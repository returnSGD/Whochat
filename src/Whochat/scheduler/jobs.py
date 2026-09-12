"""L0 编排层 —— APScheduler。

**为什么不用 Airflow/Prefect**（方案文档 §8.1）：
Airflow 在 Windows 上需要 WSL/Docker，且对"几个定时任务"来说完全是杀鸡用牛刀。
本项目 MVP 阶段只有 4 个周期任务，APScheduler 单进程就能覆盖，零运维。

什么时候该换：
    - 任务间出现真实依赖（DAG）
    - 需要回填/重试/血缘追踪
    - 多机部署
    那时迁 Prefect（`@flow`/`@task` 装饰器，改动代码极少）。

**调度设计的关键点**：快通道和慢通道必须分开调度。
预警要秒级，分析可以分钟级 —— 如果把两者塞进同一个 job，
预警会被分析拖慢，快慢双通道就白设计了。
"""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import datetime, timedelta

from Whochat.config import settings
from Whochat.console import configure_console

# ============================================================ 任务


def job_fast_alert() -> None:
    """快通道 —— 每分钟。纯规则，不碰模型，秒级完成。

    这是整个系统的时效性保证：它不等清洗、不等分析，
    只看"最近发生了什么"。
    """
    from Whochat.alert.notifier import WeComNotifier
    from Whochat.alert.rules_engine import RuleEngine
    from Whochat.store.repository import Repository

    _log("快通道预警")
    repo = Repository()
    try:
        engine = RuleEngine(repo)
        alert_ids = engine.run_and_record()
        if alert_ids:
            _log(f"  触发 {len(alert_ids)} 条告警")

        # ⚠️ flush 必须**无条件**跑，不能包在 `if alert_ids:` 里。
        #    发送失败的告警会保持 pending 等下一次重试（notifier.flush 的设计），
        #    但如果本轮没有新告警就不 flush，那些 pending 根本等不到重试 ——
        #    直到 6 小时后被 expire_stale_alerts 标记 failed，永久丢警。
        #    企微限流（45009）或一次网络抖动就足以触发这条路径。
        notifier = WeComNotifier(repo=repo)
        # 实时通道：只有 red 级立刻推，其余留待日报
        result = notifier.flush()
        _log(f"  推送: {result.status} — {result.message}")
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        # 必须放 finally：异常路径上不关会话会一直占着池化连接，
        # 每分钟一次的 job 很快就把连接池耗光。
        repo.close()


def _crawl_one(
    task_id: str,
    platform: str,
    mode: str,
    target: str,
    lock=None,
) -> tuple[str, bool, str, int]:
    """在独立线程/独立会话里跑一个采集任务。

    每个任务开自己的 Repository（= 自己的连接）。SQLite 已开 WAL +
    busy_timeout，多写者会排队而不是直接报 "database is locked"。

    `lock` 是**平台级互斥锁**：同一平台的多个关键词必须串行。原因有两个，
    都不是理论风险：
      1. MediaCrawler 按天复用同一个 JSONL 文件名（search_contents_DATE），
         同平台并发写同一个文件后，适配器按 mtime 判"本次新增"会把 A 关键词
         的记录误当成 B 关键词的产出（归因错乱）；
      2. 同一平台/账号并发多个浏览器会话，更容易触发风控。
    跨平台仍然并行 —— 那才是并发的价值所在。
    """
    from contextlib import nullcontext

    from Whochat.crawler import source_for
    from Whochat.crawler.base import CrawlTask
    from Whochat.pipeline.runner import Pipeline
    from Whochat.store.repository import Repository

    repo = Repository()
    try:
        # ⚠️ 必须经 source_for 构建并注册后端：调度器进程里注册表默认是空的，
        # 直接 resolve_source 会命中 KeyError，任务永远失败。
        source = source_for(platform)
        task = CrawlTask(
            platform=platform,
            mode=mode,
            target=target,
            max_items=settings.crawl.max_items,
            include_sub_comments=settings.crawl.include_sub_comments,
        )
        with (lock if lock is not None else nullcontext()):
            stats = Pipeline(repo).crawl(task, source_name=source.name)
        _log(
            f"  ✓ {platform}/{target}"
            f" — 内容 {stats.stored_contents} / 评论 {stats.stored_comments}"
        )
        return task_id, True, "", stats.stored_comments
    except Exception as e:
        # 单个任务失败不能影响其他任务，也不能中断调度器
        msg = f"{type(e).__name__}: {e}"
        _log(f"  ✗ {platform}/{target} — {msg}")
        return task_id, False, msg, 0
    finally:
        repo.close()


def job_crawl() -> None:
    """采集 —— 默认每 30 分钟扫一次**到期**的监控任务。

    采集失败是**常态**（403/滑块/登录态失效），所以这里不抛异常，只记录，
    让任务失败退避后继续排期。断点续爬游标存在 CrawlTask.last_cursor。

    ⚠️ 与早期实现的关键区别：任务不再是"跑完 done 就再也不管"。每个任务的
    `next_run_at` 决定下一次何时到期，`mark_task_result` 在每轮结束后重新排期
    —— 这才是"长期监测 100 个关键词"能成立的原因。

    并发：每个任务会拉起一个浏览器子进程，默认 `concurrency=1`（最稳）。
    100 个词要更快产出可调高，但注意内存与平台风控。
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from Whochat.store.repository import Repository

    _log("采集")
    repo = Repository()
    try:
        # 崩溃残留的 running 若不回收，会被到期查询永久跳过，该关键词静默停采
        stale_after = max(settings.crawl.timeout_seconds * 2, 3600)
        recovered = repo.reset_stale_running(stale_after)
        if recovered:
            _log(f"  回收 {recovered} 个中断残留的 running 任务")

        summary = repo.watch_summary()
        if summary["enabled"] == 0:
            _log("  没有启用的监控任务（用 `cli keywords add` 添加）")
            return

        rows = repo.due_crawl_tasks(limit=settings.crawl.max_due_tasks)
        if not rows:
            _log(
                f"  没有到期任务（监控 {summary['enabled']} 个，"
                f"运行中 {summary['running']}）"
            )
            return

        jobs = [(r.task_id, r.platform, r.mode, r.target) for r in rows]
        workers = max(1, min(settings.crawl.concurrency, len(jobs)))
        _log(
            f"  到期 {len(jobs)} 个任务，并发 {workers}"
            + (f"，历史失败 {summary['failing']} 个" if summary["failing"] else "")
        )
        repo.mark_task_running([j[0] for j in jobs])

        # 平台级互斥锁：同平台串行（见 _crawl_one 的说明），跨平台并行
        import threading

        locks: dict[str, threading.Lock] = {}
        locks_guard = threading.Lock()

        def platform_lock(platform: str) -> threading.Lock:
            with locks_guard:
                return locks.setdefault(platform, threading.Lock())

        succeeded = failed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl") as ex:
            futures = []
            for i, (tid, platform, mode, target) in enumerate(jobs):
                # 任务**启动**之间限速。低于 3 秒会显著提高被封概率（config）。
                if i and settings.crawl.min_interval > 0:
                    time.sleep(settings.crawl.min_interval)
                _log(f"  → {platform}/{target}")
                futures.append(
                    ex.submit(_crawl_one, tid, platform, mode, target, platform_lock(platform))
                )

            for fut in as_completed(futures):
                try:
                    tid, ok, err, items = fut.result()
                except Exception as e:  # 理论上 _crawl_one 不抛，兜底
                    _log(f"  任务异常: {type(e).__name__}: {e}")
                    continue
                repo.mark_task_result(tid, ok=ok, items=items, error=err or None)
                if ok:
                    succeeded += 1
                else:
                    failed += 1

        after = repo.watch_summary()
        _log(
            f"  完成 {succeeded} / 失败 {failed}；"
            f"仍在监控 {after['enabled']} 个，待跑 {after['due']} 个"
        )
        if after["due"] > 0:
            _log(
                "  ⚠️ 仍有到期任务未处理：提高 WHOCHAT_CRAWL_CONCURRENCY "
                "或调大各关键词的采集间隔，否则监测频率会持续走低"
            )
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        # 必须放 finally：异常路径上不关会话会一直占着池化连接，
        # 每 30 分钟一次的 job 很快就把连接池耗光。
        repo.close()


def job_analyze() -> None:
    """慢通道 —— 默认每 30 分钟。清洗 + 去重 + 情感分析。"""
    from Whochat.pipeline.runner import Pipeline
    from Whochat.store.repository import Repository

    _log("分析")
    repo = Repository()
    try:
        stats = Pipeline(repo).analyze()
        if stats.analyzed:
            _log(f"  已分析 {stats.analyzed} 条")
        else:
            _log("  没有新数据")
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        repo.close()


def job_daily_report() -> None:
    """日报 —— 每天 9 点。把低等级告警和统计摘要合并成一条推送。"""
    from Whochat.alert.notifier import WeComNotifier, build_daily_digest
    from Whochat.store.repository import Repository

    _log("日报")
    repo = Repository()
    try:
        notifier = WeComNotifier(repo=repo)

        # 先把积压的告警合并推出去
        result = notifier.flush(digest=True)
        _log(f"  告警汇总: {result.status} — {result.message}")

        # 再推一条统计摘要
        if notifier.enabled:
            content = build_daily_digest(repo)
            ok, msg = notifier.send_markdown(content)
            _log(f"  统计摘要: {'已发送' if ok else msg}")
        else:
            print(build_daily_digest(repo))
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        repo.close()


def job_topics() -> None:
    """主题建模 —— 每天一次。比较耗 CPU，不要跟快通道抢资源。"""
    from Whochat.analysis import topics
    from Whochat.pipeline.runner import Pipeline
    from Whochat.store.repository import Repository

    ok, msg = topics.is_available()
    if not ok:
        _log(f"主题建模跳过: {msg}")
        return

    _log("主题建模")
    repo = Repository()
    try:
        version = Pipeline(repo).version
        docs, _ = topics.aggregate_by_content(repo, version)
        result = topics.model_topics(docs)
        if result.ok:
            _log(f"  {result.message}")
            repo.save_topics("daily", [
                {
                    "topic_id": t.topic_id,
                    "label": t.label,
                    "keywords": t.keywords,
                    "doc_count": t.doc_count,
                    "rep_docs": t.rep_docs,
                }
                for t in result.topics
            ])
        else:
            _log(f"  {result.message}")
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        repo.close()


# ============================================================ 工具


def _log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _handle_signal(scheduler) -> None:
    """返回一个信号处理器，用来优雅停止调度器。

    ⚠️ 不要只设一个 `_running = False` 的标志位就完事：自定义 SIGINT 处理器
    会**替换掉** Python 默认的 default_int_handler，KeyboardInterrupt 根本不会
    抛出，`scheduler.start()` 会一直阻塞，而那个标志位从来没人读 ——
    结果就是 Ctrl+C 完全没反应，只能杀进程。
    正解是在处理器里直接 shutdown()：BlockingScheduler.start() 阻塞在
    Event.wait() 上，shutdown() 会把它唤醒，start() 随即正常返回。
    """

    def _stop(signum, frame) -> None:
        _log("收到停止信号，正在停止（等待当前任务结束）…")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            # 调度器可能尚未启动或已停止，忽略
            pass

    return _stop


def build_scheduler():
    """组装调度器。任务间隔可通过 CLI 参数调整。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(
        timezone="Asia/Shanghai",
        job_defaults={
            # 同一任务不并发（采集和分析抢同一个库）
            "max_instances": 1,
            # 错过执行时间后合并成一次，而不是补跑十次
            "coalesce": True,
            "misfire_grace_time": 300,
        },
    )

    # 快通道必须独立且高频 —— 它不能被采集/分析拖慢
    scheduler.add_job(job_fast_alert, IntervalTrigger(minutes=1), id="fast_alert")
    scheduler.add_job(job_crawl, IntervalTrigger(minutes=30), id="crawl")
    scheduler.add_job(job_analyze, IntervalTrigger(minutes=30), id="analyze")
    scheduler.add_job(job_topics, CronTrigger(hour=3, minute=0), id="topics")
    scheduler.add_job(job_daily_report, CronTrigger(hour=9, minute=0), id="daily_report")

    return scheduler


# ============================================================ CLI


def main(argv: list[str] | None = None) -> int:
    # 日报（dry-run 分支）会打印带 emoji 的文案，GBK 控制台必须先降级
    configure_console()
    # 长期运行必须落文件日志 —— stdout 一关就没了，出了事无从回溯
    from Whochat.logging_setup import setup_logging

    log_path = setup_logging()
    parser = argparse.ArgumentParser(
        prog="Whochat.scheduler",
        description="舆情分析调度器 —— 快通道 / 采集 / 分析 / 主题 / 日报",
    )
    parser.add_argument("--once", metavar="JOB", help="只跑一次指定任务就退出（fast_alert/crawl/analyze/topics/daily_report）")
    parser.add_argument("--list", action="store_true", help="列出所有任务")
    args = parser.parse_args(argv)

    jobs = {
        "fast_alert": job_fast_alert,
        "crawl": job_crawl,
        "analyze": job_analyze,
        "topics": job_topics,
        "daily_report": job_daily_report,
    }

    if args.list:
        for name in jobs:
            print(f"  {name}")
        return 0

    # --once 用来手动触发或放进系统 cron/Task Scheduler
    if args.once:
        if args.once not in jobs:
            print(f"未知任务: {args.once}。可选: {list(jobs)}")
            return 1
        jobs[args.once]()
        return 0

    scheduler = build_scheduler()
    signal.signal(signal.SIGINT, _handle_signal(scheduler))
    if hasattr(signal, "SIGTERM"):
        # Windows 任务计划程序/服务停止走的是 SIGTERM
        signal.signal(signal.SIGTERM, _handle_signal(scheduler))

    _log(f"调度器启动（日志: {log_path}）")
    for job in scheduler.get_jobs():
        _log(f"  {job.id:<14} {job.trigger}")

    # 把监控规模打在启动日志里：长期运营第一眼要看到"我在盯多少词、健康吗"
    try:
        from Whochat.store.repository import Repository, init_db

        init_db()
        repo = Repository()
        try:
            s = repo.watch_summary()
            _log(
                f"  监控任务: 共 {s['total']} / 启用 {s['enabled']} / "
                f"待跑 {s['due']} / 连续失败 {s['failing']}"
            )
            if s["enabled"] > 0:
                _log(
                    f"  采集并发 {settings.crawl.concurrency}"
                    f"（每任务超时 {settings.crawl.timeout_seconds}s）"
                )
        finally:
            repo.close()
    except Exception as e:
        _log(f"  读取监控规模失败: {type(e).__name__}: {e}")
    _log("Ctrl+C 停止")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        # 兜底：信号处理器没能拦住时（例如非主线程启动）仍能退出
        pass
    finally:
        if getattr(scheduler, "running", False):
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
    _log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
