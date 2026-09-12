"""周期关键词监控的回归测试。

核心承诺：**任务跑完会重新排期**，不是跑完即止。早期实现把它置成 done 就
再也不过问，于是每个关键词只被采集一次 —— "长期监测 100 个关键词"实际上
不成立。这里把重排、失败退避、崩溃恢复都钉死。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import inspect

from Whochat.store.models import CrawlTask, utcnow


class TestAddWatchTasks:
    def test_bulk_add_is_idempotent(self, repo):
        created, updated = repo.add_watch_tasks(
            "xhs", ["品牌A", "品牌B", "# 注释行", "  "], interval_seconds=600
        )
        assert (created, updated) == (2, 0), "注释与空行不应建任务"

        created2, updated2 = repo.add_watch_tasks(
            "xhs", ["品牌A", "品牌C"], interval_seconds=600
        )
        assert (created2, updated2) == (1, 1), "已存在的词只更新，不重复建"
        assert len(repo.list_crawl_tasks("xhs")) == 3

    def test_new_tasks_are_due_immediately(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A", "品牌B"], interval_seconds=600)
        assert len(repo.due_crawl_tasks()) == 2, "next_run_at=NULL 视为立即到期"

    def test_disabled_tasks_are_not_due(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600, enabled=False)
        assert repo.due_crawl_tasks() == []
        assert repo.watch_summary()["enabled"] == 0

    def test_same_keyword_on_two_platforms_creates_two_tasks(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600)
        repo.add_watch_tasks("douyin", ["品牌A"], interval_seconds=600)
        assert repo.watch_summary()["total"] == 2


class TestReArm:
    def test_success_rearms_for_next_cycle(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600)
        tid = repo.list_crawl_tasks()[0].task_id

        repo.mark_task_running([tid])
        assert repo.due_crawl_tasks() == [], "running 不重复派发"
        repo.mark_task_result(tid, ok=True, items=7)

        t = repo.session.get(CrawlTask, tid)
        assert t.status == "done"
        assert t.consecutive_failures == 0
        assert t.items_collected == 7
        assert t.next_run_at is not None, "成功后必须排下一次 —— 否则监测只跑一轮"
        assert repo.due_crawl_tasks() == [], "刚排期，尚未到期"

    def test_failure_keeps_monitoring_with_backoff(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600)
        tid = repo.list_crawl_tasks()[0].task_id

        for _ in range(3):
            repo.mark_task_running([tid])
            repo.mark_task_result(tid, ok=False, error="403 风控")

        t = repo.session.get(CrawlTask, tid)
        assert t.enabled is True, "失败不能自动停掉监控"
        assert t.status == "failed"
        assert t.consecutive_failures == 3
        assert t.error == "403 风控"
        delay = (
            t.next_run_at.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
        ).total_seconds()
        assert delay > 600 * 3, "连续失败应按指数退避，避免挤占正常词"

    def test_one_shot_task_disables_after_run(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=0)
        tid = repo.list_crawl_tasks()[0].task_id
        repo.mark_task_result(tid, ok=True)
        t = repo.session.get(CrawlTask, tid)
        assert t.enabled is False and t.next_run_at is None

    def test_reenable_schedules_immediately(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600, enabled=False)
        tid = repo.list_crawl_tasks()[0].task_id
        assert repo.set_task_enabled(tid, True) is True
        assert repo.due_crawl_tasks(), "重新启用后应立刻排期一次"


class TestCrashRecovery:
    def test_stale_running_is_recovered(self, repo):
        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600)
        tid = repo.list_crawl_tasks()[0].task_id
        repo.mark_task_running([tid])

        assert repo.reset_stale_running(3600) == 0, "未超时不能回收"
        t = repo.session.get(CrawlTask, tid)
        t.updated_at = utcnow() - timedelta(hours=2)
        repo.session.commit()

        assert repo.reset_stale_running(3600) == 1
        assert repo.session.get(CrawlTask, tid).status == "pending"
        assert repo.due_crawl_tasks(), "回收后必须能被重新派发"


class TestJobCrawl:
    def test_due_task_is_run_and_rearmed(self, repo, monkeypatch):
        """端到端：job_crawl 消费到期任务，跑完按 interval 重排。

        回归的是那个最要命的 bug —— 任务成功置 done 后无人重新入队。
        """
        import Whochat.crawler as crawler_mod
        import Whochat.pipeline.runner as runner_mod
        from Whochat.config import settings

        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600)

        class FakeSource:
            name = "fake"

        monkeypatch.setattr(crawler_mod, "source_for", lambda platform: FakeSource())

        class FakePipeline:
            def __init__(self, repo):
                pass

            def crawl(self, task, source_name=None):
                return SimpleNamespace(stored_contents=1, stored_comments=3)

        monkeypatch.setattr(runner_mod, "Pipeline", FakePipeline)
        monkeypatch.setattr(settings.crawl, "min_interval", 0)

        from Whochat.scheduler.jobs import job_crawl

        job_crawl()
        # job_crawl 用自己的会话写库，测试会话的 identity map 需要失效才能看到新值
        repo.session.expire_all()

        t = repo.list_crawl_tasks()[0]
        assert t.status == "done"
        assert t.items_collected == 3
        assert t.next_run_at is not None, "跑完必须排下一次，否则监测只跑一轮"
        assert repo.due_crawl_tasks() == []

    def test_no_due_task_does_nothing(self, repo, monkeypatch):
        """没有到期任务时不应误跑（禁用任务尤其不能被带上）。"""
        import Whochat.crawler as crawler_mod

        repo.add_watch_tasks("xhs", ["品牌A"], interval_seconds=600, enabled=False)

        called = []
        monkeypatch.setattr(
            crawler_mod,
            "source_for",
            lambda *a, **k: called.append("x") or SimpleNamespace(name="fake"),
        )

        from Whochat.scheduler.jobs import job_crawl

        job_crawl()
        assert called == []


class TestSchedulerSourceRegistration:
    def test_source_for_registers_backend(self, monkeypatch):
        """调度器进程的注册表默认是空的；source_for 必须把它填上。

        回归：早期 job_crawl 直接 resolve_source(platform)，注册表为空 →
        每个任务都报"未注册的采集后端"，长期采集从未真正跑起来。
        """
        import Whochat.crawler as crawler_mod
        from Whochat.config import settings
        from Whochat.crawler.base import get_source, list_sources

        monkeypatch.setattr(settings.crawl, "source", "mock")
        src = crawler_mod.source_for("xhs")

        assert src.name == "mock"
        assert "mock" in list_sources()
        assert get_source("mock") is src


class TestMigration:
    def test_old_crawl_tasks_table_gets_new_columns(self, tmp_path, monkeypatch):
        """老库升级：`create_all` 不会给已存在的表加列，必须靠增量迁移。

        没有这一步，升级后调度器会直接报 `no such column: crawl_tasks.enabled`。
        """
        import sqlite3

        import Whochat.store.repository as R
        from Whochat.config import settings

        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE crawl_tasks (
                task_id TEXT PRIMARY KEY, platform TEXT, mode TEXT, target TEXT,
                status TEXT, last_cursor TEXT, items_collected INTEGER, error TEXT,
                created_at DATETIME, updated_at DATETIME)"""
        )
        con.commit()
        con.close()

        monkeypatch.setattr(R, "_engine", None)
        monkeypatch.setattr(R, "_SessionFactory", None)
        monkeypatch.setattr(settings.store, "url", f"sqlite:///{db}")

        R.init_db()

        cols = {c["name"] for c in inspect(R.get_engine()).get_columns("crawl_tasks")}
        assert {
            "enabled",
            "interval_seconds",
            "last_run_at",
            "next_run_at",
            "consecutive_failures",
        } <= cols

        # 幂等：再跑一次不应报 duplicate column
        R.init_db()
