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

from wochat.config import settings

# ============================================================ 任务


def job_fast_alert() -> None:
    """快通道 —— 每分钟。纯规则，不碰模型，秒级完成。

    这是整个系统的时效性保证：它不等清洗、不等分析，
    只看"最近发生了什么"。
    """
    from wochat.alert.notifier import WeComNotifier
    from wochat.alert.rules_engine import RuleEngine
    from wochat.store.repository import Repository

    _log("快通道预警")
    repo = Repository()
    try:
        engine = RuleEngine(repo)
        alert_ids = engine.run_and_record()
        if alert_ids:
            _log(f"  触发 {len(alert_ids)} 条告警")
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


def job_crawl() -> None:
    """采集 —— 默认每 30 分钟。

    采集失败是**常态**（403/滑块/登录态失效），所以这里不抛异常，
    只记录，让下一次周期继续跑。断点续爬游标存在 CrawlTask.last_cursor。
    """
    from wochat.crawler.base import CrawlTask, resolve_source
    from wochat.pipeline.runner import Pipeline
    from wochat.store.repository import Repository

    _log("采集")
    repo = Repository()
    try:
        from sqlalchemy import select

        from wochat.store.models import CrawlTask as CrawlTaskModel

        rows = list(
            repo.session.scalars(
                select(CrawlTaskModel).where(CrawlTaskModel.status.in_(("pending", "running")))
            )
        )
        if not rows:
            _log("  没有待执行的采集任务")
            return

        pipeline = Pipeline(repo)
        for row in rows:
            _log(f"  {row.platform}/{row.target}")
            row.status = "running"
            repo.session.commit()
            try:
                source = resolve_source(row.platform)
                task = CrawlTask(
                    platform=row.platform,
                    mode=row.mode,
                    target=row.target,
                    max_items=settings.crawl.max_items,
                    include_sub_comments=settings.crawl.include_sub_comments,
                )
                stats = pipeline.crawl(task, source_name=source.name)
                row.status = "done"
                row.items_collected = (row.items_collected or 0) + stats.stored_comments
                _log(f"    内容 {stats.stored_contents} / 评论 {stats.stored_comments}")
            except Exception as e:
                # 单个任务失败不能影响其他任务
                row.status = "pending"  # 保持 pending，下轮重试
                row.error = f"{type(e).__name__}: {e}"[:500]
                _log(f"    失败: {row.error}")
            repo.session.commit()
    except Exception as e:
        _log(f"  失败: {type(e).__name__}: {e}")
    finally:
        repo.close()


def job_analyze() -> None:
    """慢通道 —— 默认每 30 分钟。清洗 + 去重 + 情感分析。"""
    from wochat.pipeline.runner import Pipeline
    from wochat.store.repository import Repository

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
    from wochat.alert.notifier import WeComNotifier, build_daily_digest
    from wochat.store.repository import Repository

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
    from wochat.analysis import topics
    from wochat.pipeline.runner import Pipeline
    from wochat.store.repository import Repository

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
    parser = argparse.ArgumentParser(
        prog="wochat.scheduler",
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

    _log("调度器启动")
    for job in scheduler.get_jobs():
        _log(f"  {job.id:<14} {job.trigger}")
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
