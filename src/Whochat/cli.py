"""命令行入口。

典型用法：

    # 1. 初始化（建库、默认规则、自检）
    python -m Whochat.cli init

    # 2. 零依赖验证整条链路（不需要爬虫、不需要模型）
    python -m Whochat.cli demo

    # 3. 看板
    python -m Whochat.cli dashboard

    # 4. 真实采集（需要 MediaCrawler 环境）
    python -m Whochat.cli crawl --platform xhs --keyword "某品牌"

    # 5. 导入已有的 MediaCrawler 输出
    python -m Whochat.cli import vendor/MediaCrawler/data/xhs/jsonl/search_comments_2026-09-11.jsonl --platform xhs

    # 6. 分析 / 主题 / 词云 / 预警
    python -m Whochat.cli analyze
    python -m Whochat.cli topics
    python -m Whochat.cli wordcloud
    python -m Whochat.cli alert
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from Whochat.config import EXPORT_DIR, ROOT, settings
from Whochat.console import configure_console


# ---------------------------------------------------------------- 工具


def _banner(text: str) -> None:
    print("\n" + "=" * 66)
    print(f"  {text}")
    print("=" * 66)


def _repo():
    from Whochat.store.repository import Repository, init_db

    init_db()
    return Repository()


# ================================================================ 命令


def cmd_init(args) -> int:
    _banner("初始化")
    from Whochat.alert.rules_engine import seed_default_rules

    repo = _repo()
    print(f"数据库: {settings.store.url}")

    n = seed_default_rules(repo)
    print(f"已写入 {n} 条默认预警规则")

    # 环境自检 —— 把"能不能跑"一次性说清楚，省得后面一个个试
    print("\n组件状态：")

    from Whochat.analysis.sentiment import TransformerSentiment, get_analyzer

    a = get_analyzer()
    print(f"  情感分析    : {a.name}", end="")
    if a.name == "lexicon":
        print("（词典法，零依赖可跑）")
        if not TransformerSentiment.is_available():
            print("               → 想要更高准确率: pip install -e .[sentiment]")
    else:
        print()

    from Whochat.pipeline.llm_client import LLMClient

    cfg = settings.llm
    if not cfg.configured:
        print("  LLM 分析    : 未配置（可选）")
        print("               → 填 WHOCHAT_LLM_BASE_URL 与 WHOCHAT_LLM_API_KEY 即可启用")
    elif not cfg.is_enabled:
        print("  LLM 分析    : 已配置但被关闭（WHOCHAT_LLM_ENABLED=false）")
    else:
        ok, msg = LLMClient().available()
        print(f"  LLM 分析    : {'就绪' if ok else '不可用'} — {msg}")

    from Whochat.analysis import topics

    ok, msg = topics.is_available()
    print(f"  主题建模    : {'就绪' if ok else '未就绪'} — {msg}")

    from Whochat.analysis.wordcloud_gen import find_cjk_font

    font = find_cjk_font()
    print(f"  中文字体    : {font or '未找到（词云会渲染成方框）'}")

    from Whochat.crawler.mediacrawler_source import MediaCrawlerSource

    ok, msg = MediaCrawlerSource().available()
    print(f"  MediaCrawler: {'就绪' if ok else '未就绪'} — {msg}")
    print(f"  企微推送    : {'已配置' if settings.alert.wecom_webhook else '未配置（dry-run 模式，只打印）'}")

    print(f"\n词典目录: {ROOT / 'dicts'}")
    return 0


def cmd_demo(args) -> int:
    """零依赖跑通全链路 —— 这条命令能跑通，说明业务逻辑没问题。"""
    _banner("Demo：用造数据跑通全链路")
    print("这一步不需要爬虫、不需要模型，用来验证清洗/分析/看板/预警是否正常。\n")

    from Whochat.alert.notifier import WeComNotifier
    from Whochat.alert.rules_engine import RuleEngine, seed_default_rules
    from Whochat.crawler.base import CrawlTask
    from Whochat.crawler.mock_source import MockSource
    from Whochat.pipeline.runner import Pipeline

    repo = _repo()
    seed_default_rules(repo)

    source = MockSource()
    pipeline = Pipeline(repo)

    task = CrawlTask(
        platform=args.platform or "mock",
        mode="keyword",
        target=args.keyword,
        max_items=600,
        include_sub_comments=True,
        extra={"span_hours": 72, "burst_hour": 30},
    )

    # 采集
    contents, comments = [], []
    for rec in source.crawl(task):
        (comments if "comment_id" in rec else contents).append(rec)

    print(f"造数: 内容 {len(contents)} 条 / 评论 {len(comments)} 条")
    repo.upsert_contents(contents)
    repo.upsert_comments(comments)

    # 指标快照：造 3 个时间点，让传播曲线有形状
    from Whochat.store.models import utcnow
    from datetime import timedelta

    snaps = []
    for c in contents:
        for h in (48, 24, 0):
            snaps.append(
                {
                    "content_id": c["content_id"],
                    "snapshot_time": utcnow() - timedelta(hours=h),
                    # 越早的快照数值越小 —— 模拟传播增长
                    "like_count": int((c.get("like_count") or 0) * (0.3 if h == 48 else 0.7 if h == 24 else 1.0)),
                    "comment_count": int((c.get("comment_count") or 0) * (0.3 if h == 48 else 0.7 if h == 24 else 1.0)),
                    "share_count": c.get("share_count"),
                }
            )
    repo.add_snapshots(snaps)

    # 分析 —— 用同一个 stats 对象累计，否则 report() 里采集/落库数字会是 0
    from Whochat.pipeline.runner import PipelineStats

    stats = PipelineStats(
        crawled_contents=len(contents),
        crawled_comments=len(comments),
        stored_contents=len(contents),
        stored_comments=len(comments),
        snapshots=len(snaps),
    )

    print()
    analysis = pipeline.analyze()
    stats.analyzed = analysis.analyzed
    stats.dropped_spam = analysis.dropped_spam
    stats.dropped_dup = analysis.dropped_dup

    # 预警（快通道）
    #
    # 这里用**回放模式**而不是实时模式：造的数据 publish_time 都在过去，
    # 实时模式只看"最近 N 分钟"，扫不到 42 小时前的那次爆发。
    # 真实场景同理 —— 爬虫断线后补抓的数据，必须能指定时间窗重跑，
    # 否则那批舆情会被静默漏掉。
    print()
    engine = RuleEngine(repo)
    # 爆发点被 mock 造在 42 小时前（span 72h / burst_hour 30），取 ±6h 作为回放窗口
    since = utcnow() - timedelta(hours=48)
    until = utcnow() - timedelta(hours=36)
    print(f"回放窗口: {since:%m-%d %H:%M} ~ {until:%m-%d %H:%M}（模拟的爆发时段）")

    alert_ids = engine.run_and_record(since=since, until=until, skip_cooldown=True)
    print(f"快通道预警: 触发 {len(alert_ids)} 条规则")

    notifier = WeComNotifier(repo=repo)

    # 实时通道：只有 red 级会立刻推（分级路由，避免爆发时被企微限流打爆）
    result = notifier.flush()
    print(f"实时通道: {result.status} — {result.message}")

    # 日报通道：把所有 pending 合并成一条。demo 顺便展示真实的消息格式
    result2 = notifier.flush(digest=True)
    print(f"日报通道: {result2.status} — {result2.message}")

    # 词云
    print()
    from Whochat.analysis import wordcloud_gen

    texts = repo.analyzed_texts(pipeline.version)
    wc = wordcloud_gen.generate(texts)
    print(f"词云: {wc.message}")

    _banner("完成")
    print(stats.report())
    print(f"\n下一步: python -m Whochat.cli dashboard")
    print(f"分析版本: {pipeline.version}")
    return 0


def cmd_crawl(args) -> int:
    _banner(f"采集 {args.platform} / {args.keyword}")
    from Whochat.crawler.base import CrawlTask, resolve_source

    repo = _repo()

    if args.source == "mediacrawler":
        from Whochat.crawler.mediacrawler_source import MediaCrawlerSource

        src = MediaCrawlerSource(login_type=args.login, headless=args.headless)
    elif args.source == "mock":
        from Whochat.crawler.mock_source import MockSource

        src = MockSource()
    else:
        print(f"未知采集后端: {args.source}")
        return 1

    from Whochat.crawler.base import register

    register(src)

    task = CrawlTask(
        platform=args.platform,
        mode=args.mode,
        target=args.keyword,
        max_items=args.max_items,
        include_sub_comments=not args.no_sub_comments,
    )

    from Whochat.pipeline.runner import Pipeline

    pipeline = Pipeline(repo)
    stats = pipeline.run_all(task, source_name=src.name)

    _banner("采集完成")
    print(stats.report())
    return 0


def _collect_keywords(args) -> list[str] | None:
    """从 --keyword / --file 收集关键词，去重保序。

    文件一行一个，UTF-8 BOM 用 utf-8-sig 吃掉（Excel 导出的 txt 常带 BOM，
    否则第一个关键词会凭空多出 \\ufeff 前缀，采集命不中）。
    """
    words = list(getattr(args, "keyword", None) or [])
    for path_str in getattr(args, "file", None) or []:
        path = Path(path_str)
        if not path.exists():
            print(f"关键词文件不存在: {path}")
            return None
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                words.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def cmd_keywords(args) -> int:
    """监控关键词的批量管理 —— 支撑"长期监测 100 个关键词"。"""
    repo = _repo()
    action = args.action

    if action == "add":
        words = _collect_keywords(args)
        if words is None:
            return 1
        platforms = list(args.platform or [])
        if not words:
            print("没有可加入的关键词。用 --keyword 或 --file 提供。")
            return 1
        if not platforms:
            print("必须指定 --platform（可重复）")
            return 1
        _banner(f"加入监控：{len(words)} 个关键词 × {len(platforms)} 个平台")
        created = updated = 0
        for platform in platforms:
            c, u = repo.add_watch_tasks(
                platform,
                words,
                interval_seconds=args.interval,
                enabled=not args.disabled,
            )
            created += c
            updated += u
            print(f"  {platform:<10} 新建 {c} / 更新 {u}")
        print(f"\n合计：新建 {created}，更新 {updated}")
        print(f"数据库任务总数：{repo.watch_summary()['total']}")
        if args.disabled:
            print("已加入但未启用（`keywords enable` 可启用）")
        else:
            print("调度器下一轮会开始采集。查看: python -m Whochat.cli keywords status")
        return 0

    if action == "list":
        rows = repo.list_crawl_tasks(platform=args.platform)
        if not rows:
            print("没有监控任务。用 `keywords add` 添加。")
            return 0
        print(f"{'平台':<10}{'目标':<24}{'状态':<9}{'启用':<5}{'间隔s':<8}{'失败':<5}{'下次':<20}{'错误'}")
        print("-" * 100)
        for t in rows:
            nxt = t.next_run_at.strftime("%m-%d %H:%M") if t.next_run_at else "立即"
            print(
                f"{t.platform:<10}{t.target[:22]:<24}{t.status:<9}"
                f"{'是' if t.enabled else '否':<5}{t.interval_seconds:<8}"
                f"{t.consecutive_failures:<5}{nxt:<20}{(t.error or '')[:20]}"
            )
        return 0

    if action == "status":
        s = repo.watch_summary()
        _banner("监控状态")
        print(f"  任务总数    : {s['total']}")
        print(f"  启用中      : {s['enabled']}")
        print(f"  待跑（到期）: {s['due']}")
        print(f"  运行中      : {s['running']}")
        print(f"  连续失败    : {s['failing']}")
        print(f"\n  采集并发    : {settings.crawl.concurrency}（WHOCHAT_CRAWL_CONCURRENCY）")
        print(f"  单任务超时  : {settings.crawl.timeout_seconds}s（WHOCHAT_CRAWL_TIMEOUT）")
        if s["due"] > 0:
            print(
                "\n  ⚠️ 有到期未跑的任务。到期数长期 >0 说明监测频率跟不上："
                "\n     提高并发，或调大各关键词的采集间隔。"
            )
        return 0

    if action in ("enable", "disable"):
        enable = action == "enable"
        ids = list(args.task_id or [])
        if args.platform:
            ids += [t.task_id for t in repo.list_crawl_tasks(platform=args.platform)]
        if not ids:
            print("用 --task-id 或 --platform 指定要操作的任务。")
            return 1
        changed = sum(1 for tid in ids if repo.set_task_enabled(tid, enable))
        print(f"已{'启用' if enable else '停用'} {changed} 个任务")
        return 0

    if action == "rm":
        ids = list(args.task_id or [])
        if args.platform:
            if not args.all:
                print("按平台批量删除需同时加 --all（避免误删）。")
                return 1
            if not args.yes:
                print("批量删除不可撤销，确认请加 --yes。")
                return 1
        if not ids and not args.platform:
            print("用 --task-id 或 --platform --all 指定要删除的任务。")
            return 1
        n = repo.delete_crawl_tasks(
            task_ids=ids or None, platform=args.platform if args.all else None
        )
        print(f"已删除 {n} 个任务")
        return 0

    print(f"未知操作: {action}")
    return 1


def cmd_import(args) -> int:
    _banner(f"导入 {args.path}")
    from Whochat.crawler.base import CrawlTask, register
    from Whochat.crawler.manual_source import ManualImportSource, register_manual
    from Whochat.pipeline.runner import Pipeline

    path = Path(args.path)
    if not path.exists():
        print(f"文件不存在: {path}")
        return 1

    repo = _repo()
    src = register_manual(path, args.platform, llm_map=args.llm_map)

    task = CrawlTask(platform=args.platform, mode="keyword", target=args.keyword)
    stats = Pipeline(repo).crawl(task, source_name=src.name)

    if args.llm_map:
        print(f"字段映射: {src.field_map_note}")

    _banner("导入完成")
    print(stats.report())
    print("\n下一步: python -m Whochat.cli analyze")
    return 0


def cmd_analyze(args) -> int:
    _banner("分析")
    from Whochat.pipeline.runner import Pipeline

    repo = _repo()
    pipeline = Pipeline(repo, version=args.version)
    print(f"分析版本: {pipeline.version}（后端: {pipeline.analyzer.name}）\n")

    stats = pipeline.analyze(limit=args.limit)

    _banner("完成")
    print(stats.report())
    return 0


def cmd_topics(args) -> int:
    _banner("主题建模")
    from Whochat.analysis import topics
    from Whochat.pipeline.runner import Pipeline

    ok, msg = topics.is_available()
    if not ok:
        print(msg)
        print("\n主题建模是可选组件，不影响链路其它部分。")
        return 1

    repo = _repo()
    version = args.version or Pipeline(repo).version

    docs, doc_ids = topics.aggregate_by_content(repo, version, keyword=args.keyword)
    print(f"聚合出 {len(docs)} 篇文档（按 content_id 聚合，避免短文本碎片化）")

    result = topics.model_topics(docs, min_topic_size=args.min_topic_size)
    print(result.message)

    if not result.ok:
        return 1

    # LLM 命名（可选）。聚类出来的是 c-TF-IDF 关键词碎片，"发热 / 续航 / 掉电"
    # 这种标签只能算线索，不是人能直接用的主题名。
    if not args.no_llm:
        from Whochat.pipeline.llm_client import get_client

        named, note = topics.name_topics(result, client=get_client())
        print(f"LLM 主题命名: {note}" if named else f"跳过 LLM 主题命名: {note}")

    for t in result.topics[:20]:
        print(f"  [{t.doc_count:>4}] {t.label}")

    repo.save_topics("default", [
        {
            "topic_id": t.topic_id,
            "label": t.label,
            "keywords": t.keywords,
            "doc_count": t.doc_count,
            "rep_docs": t.rep_docs,
        }
        for t in result.topics
    ])
    path = topics.export_result(result)
    if path:
        print(f"\n已导出: {path}")
    return 0


def cmd_wordcloud(args) -> int:
    _banner("词云")
    from Whochat.analysis import wordcloud_gen
    from Whochat.pipeline.runner import Pipeline

    repo = _repo()
    version = args.version or Pipeline(repo).version
    texts = repo.analyzed_texts(version)

    if not texts:
        print(f"没有找到版本 {version} 的分析结果。先跑: python -m Whochat.cli analyze")
        return 1

    print(f"文本 {len(texts)} 条，版本 {version}")
    result = wordcloud_gen.generate(texts, top_n=args.top_n)
    print(result.message)

    if result.frequencies:
        freq_path = wordcloud_gen.export_frequencies(result.frequencies)
        print(f"词频已导出: {freq_path}")
        print("\nTOP 20 词：")
        for i, (word, cnt) in enumerate(list(result.frequencies.items())[:20], 1):
            print(f"  {i:>2}. {word} ({cnt})")

    return 0 if result.ok else 1


def cmd_alert(args) -> int:
    _banner("预警（快通道）")
    from Whochat.alert.notifier import WeComNotifier
    from Whochat.alert.rules_engine import RuleEngine, seed_default_rules

    repo = _repo()
    if args.seed_rules:
        n = seed_default_rules(repo)
        print(f"已写入 {n} 条默认规则")

    engine = RuleEngine(repo)

    if args.since_hours is not None:
        # 回放模式：补数、复盘、调阈值时用
        from datetime import timedelta

        from Whochat.store.models import utcnow

        since = utcnow() - timedelta(hours=args.since_hours)
        until = utcnow() - timedelta(hours=args.until_hours) if args.until_hours else None
        print(f"回放模式: {since:%m-%d %H:%M} ~ {until:%m-%d %H:%M}" if until else f"回放模式: {since:%m-%d %H:%M} 起")
        print("（跳过冷却检查 —— 否则第一次命中会把整个窗口的其余告警吞掉）")
        alert_ids = engine.run_and_record(since=since, until=until, skip_cooldown=True)
    else:
        print("规则引擎跑一遍（实时模式，只看各规则的 window_seconds 窗口）…")
        print("提示：如果爬虫刚补抓了历史数据，用 --since-hours 回放，否则会漏警")
        alert_ids = engine.run_and_record()

    print(f"触发 {len(alert_ids)} 条告警")

    notifier = WeComNotifier(repo=repo)
    result = notifier.flush(digest=args.digest)
    print(f"推送: {result.status} — {result.message}")
    if result.alert_ids:
        print(f"涉及告警: {', '.join(result.alert_ids)}")
    return 0


def cmd_status(args) -> int:
    _banner("状态")
    from Whochat.pipeline.runner import Pipeline

    repo = _repo()
    stats = repo.stats()
    for k, v in stats.items():
        print(f"  {k:>10}: {v}")

    version = Pipeline(repo).version
    dist = repo.sentiment_distribution(version)
    if dist:
        print(f"\n情感分布（{version}）:")
        for label, cnt in sorted(dist.items()):
            print(f"  {label:>10}: {cnt}")

    platform_dist = repo.platform_distribution(version)
    if platform_dist:
        print("\n平台分布:")
        for p, cnt in sorted(platform_dist.items(), key=lambda x: -x[1]):
            print(f"  {p:>10}: {cnt}")

    print(f"\n数据库: {settings.store.url}")
    print(f"看板:   python -m Whochat.cli dashboard")
    return 0


def cmd_maintenance(args) -> int:
    """数据保留 —— 长期运营时控制磁盘/查询膨胀。

    默认**试运行**（只报告将删除什么），确认后加 `--yes` 才真删。
    原始 JSONL 是"采集不可逆"的兜底，删了就无法从磁盘恢复 —— 谨慎使用。
    """
    from Whochat.config import RAW_DIR

    _banner("数据维护")
    repo = _repo()
    planned = False

    if args.prune_snapshots_days:
        n = repo.prune_snapshots(args.prune_snapshots_days, dry_run=not args.yes)
        print(
            f"{'已删除' if args.yes else '将删除'} {n} 条早于 "
            f"{args.prune_snapshots_days} 天的指标快照"
        )
        planned = planned or not args.yes

    if args.prune_raw_days:
        cutoff = time.time() - args.prune_raw_days * 86400
        files = [p for p in Path(RAW_DIR).glob("*.jsonl") if p.stat().st_mtime < cutoff]
        size_mb = sum(p.stat().st_size for p in files) / 1e6
        print(
            f"{'已删除' if args.yes else '将删除'} {len(files)} 个原始 JSONL"
            f"（{size_mb:.1f} MB，早于 {args.prune_raw_days} 天）"
        )
        if args.yes:
            for p in files:
                try:
                    p.unlink()
                except OSError as e:
                    print(f"  删除失败 {p}: {e}")
        else:
            planned = True

    if not (args.prune_snapshots_days or args.prune_raw_days):
        print("没有指定清理项。")
        print("示例：")
        print("  # 试运行（只报告）")
        print("  python -m Whochat.cli maintenance --prune-snapshots-days 90 --prune-raw-days 180")
        print("  # 确认执行")
        print("  python -m Whochat.cli maintenance --prune-snapshots-days 90 --prune-raw-days 180 --yes")
        return 0

    if planned:
        print("\n以上为试运行。确认执行请加 --yes。")
    return 0


def cmd_dashboard(args) -> int:
    """启动 Streamlit 看板。"""
    import subprocess

    app_path = Path(__file__).parent / "web" / "app.py"
    port = args.port

    print(f"启动看板: http://localhost:{port}")
    print("按 Ctrl+C 停止\n")

    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("未安装 streamlit。执行: pip install -e .[web]")
        return 1


def cmd_evaluate(args) -> int:
    """在标注集上评估情感分析 —— 全项目最该早做的一件事。"""
    _banner("情感分析评估")
    import json

    from Whochat.analysis.sentiment import evaluate, get_analyzer

    path = Path(args.file)
    if not path.exists():
        print(f"标注集不存在: {path}")
        print("\n格式要求（JSON 数组）:")
        print('  [["这个真好用", "positive"], ["用了三天就坏了", "negative"]]')
        print("\n建议自建 300~500 条业务标注集。没有基线，")
        print("后面所有的情感统计、预警阈值、主题分析都建立在流沙上。")
        return 1

    pairs = [tuple(p) for p in json.loads(path.read_text(encoding="utf-8"))]
    analyzer = get_analyzer(args.backend)
    print(f"标注样本 {len(pairs)} 条，后端 {analyzer.name}\n")

    result = evaluate(pairs, analyzer)
    print(f"准确率: {result['accuracy']:.2%}")
    print("\n混淆矩阵（行=真实，列=预测）:")
    for gold, preds in result["confusion"].items():
        print(f"  {gold:>10}: {preds}")

    if result["errors"]:
        print(f"\n错误样例（前 10 / 共 {len(result['errors'])}）:")
        for text, gold, pred in result["errors"][:10]:
            print(f"  [{gold}→{pred}] {text[:60]}")
    return 0


# ================================================================ 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="Whochat",
        description="舆情分析 Agent —— 采集 / 清洗 / 分析 / 看板 / 预警",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库、默认规则，并做环境自检").set_defaults(func=cmd_init)

    d = sub.add_parser("demo", help="用造数据零依赖跑通全链路（推荐第一步）")
    d.add_argument("--platform", default="mock", help="平台，默认 mock（全部平台）")
    d.add_argument("--keyword", default="某品牌", help="模拟的监控关键词")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("crawl", help="真实采集")
    c.add_argument("--platform", required=True, help="douyin/xhs/kuaishou/bilibili/weibo/tieba/zhihu")
    c.add_argument("--keyword", required=True, help="关键词 / 内容ID / 主页ID")
    c.add_argument("--mode", default="keyword", choices=["keyword", "content_id", "creator"])
    c.add_argument("--source", default="mediacrawler", choices=["mediacrawler", "mock"])
    c.add_argument("--login", default="qrcode", choices=["qrcode", "phone", "cookie"])
    c.add_argument("--headless", action="store_true", help="无头模式（注意：首次登录必须关掉）")
    c.add_argument("--max-items", type=int, default=500)
    c.add_argument("--no-sub-comments", action="store_true", help="不抓二级评论")
    c.set_defaults(func=cmd_crawl)

    # ---- 监控关键词批量管理（长期监测 100+ 词靠它）----
    kw = sub.add_parser("keywords", help="监控关键词批量管理（加入/启停/查看）")
    ks = kw.add_subparsers(dest="action", required=True)

    ka = ks.add_parser("add", help="批量加入监控（一行一个关键词）")
    ka.add_argument("--platform", action="append", default=[], help="可重复；每个平台各建一套任务")
    ka.add_argument("--keyword", action="append", default=[], help="单个关键词，可重复")
    ka.add_argument("--file", action="append", default=[], help="关键词文件，一行一个（# 为注释）")
    ka.add_argument(
        "--interval", type=int, default=settings.crawl.default_interval_seconds,
        help=f"采集间隔（秒），默认 {settings.crawl.default_interval_seconds}",
    )
    ka.add_argument("--disabled", action="store_true", help="只加入不启用")
    ka.set_defaults(func=cmd_keywords)

    kl = ks.add_parser("list", help="列出监控任务")
    kl.add_argument("--platform", default=None)
    kl.set_defaults(func=cmd_keywords)

    ks.add_parser("status", help="监控规模与健康度").set_defaults(func=cmd_keywords)

    for _name, _help in (("enable", "启用监控任务"), ("disable", "停用监控任务")):
        sp = ks.add_parser(_name, help=_help)
        sp.add_argument("--task-id", action="append", default=[], help="可重复")
        sp.add_argument("--platform", default=None, help="该平台下全部任务")
        sp.set_defaults(func=cmd_keywords)

    kr = ks.add_parser("rm", help="删除监控任务")
    kr.add_argument("--task-id", action="append", default=[], help="可重复")
    kr.add_argument("--platform", default=None)
    kr.add_argument("--all", action="store_true", help="配合 --platform 删除该平台全部任务")
    kr.add_argument("--yes", action="store_true", help="确认批量删除")
    kr.set_defaults(func=cmd_keywords)

    i = sub.add_parser("import", help="导入本地 JSONL/JSON/CSV")
    i.add_argument("path", help="文件路径")
    i.add_argument("--platform", default="unknown")
    i.add_argument("--keyword", default=None)
    i.add_argument(
        "--llm-map",
        action="store_true",
        help="字段名陌生时，先让 LLM 学一遍到我们 schema 的映射再导入",
    )
    i.set_defaults(func=cmd_import)

    a = sub.add_parser("analyze", help="清洗 + 去重 + 情感分析")
    a.add_argument("--version", default=None, help="分析版本号，默认按后端生成")
    a.add_argument("--limit", type=int, default=100000)
    a.set_defaults(func=cmd_analyze)

    t = sub.add_parser("topics", help="主题建模（BERTopic，可选组件）")
    t.add_argument("--version", default=None)
    t.add_argument("--keyword", default=None)
    t.add_argument("--min-topic-size", type=int, default=5)
    t.add_argument("--no-llm", action="store_true", help="跳过 LLM 主题命名")
    t.set_defaults(func=cmd_topics)

    w = sub.add_parser("wordcloud", help="生成词云")
    w.add_argument("--version", default=None)
    w.add_argument("--top-n", type=int, default=150)
    w.set_defaults(func=cmd_wordcloud)

    al = sub.add_parser("alert", help="跑快通道预警并推送")
    al.add_argument("--digest", action="store_true", help="日报模式：所有等级合并成一条")
    al.add_argument("--seed-rules", action="store_true", help="先写入默认规则")
    al.add_argument("--since-hours", type=int, default=None,
                    help="回放模式：从 N 小时前开始扫描。补数/复盘时必用，否则历史数据会漏警")
    al.add_argument("--until-hours", type=int, default=None, help="回放模式的结束点（N 小时前），默认到现在")
    al.set_defaults(func=cmd_alert)

    sub.add_parser("status", help="查看数据统计").set_defaults(func=cmd_status)

    m = sub.add_parser("maintenance", help="数据保留：清理老快照/原始文件（默认试运行）")
    m.add_argument("--prune-snapshots-days", type=int, default=None, help="删除早于 N 天的指标快照")
    m.add_argument("--prune-raw-days", type=int, default=None, help="删除早于 N 天的原始 JSONL")
    m.add_argument("--yes", action="store_true", help="确认执行（不加则只报告将删除什么）")
    m.set_defaults(func=cmd_maintenance)

    dash = sub.add_parser("dashboard", help="启动看板")
    dash.add_argument(
        "--port", type=int, default=settings.web.port,
        help=f"端口，默认 {settings.web.port}（可用 WHOCHAT_DASHBOARD_PORT 改）",
    )
    dash.set_defaults(func=cmd_dashboard)

    e = sub.add_parser("evaluate", help="在标注集上评估情感分析准确率")
    e.add_argument("file", help="标注集 JSON 文件")
    e.add_argument("--backend", default=None, help="lexicon / transformer")
    e.set_defaults(func=cmd_evaluate)

    return p


def main(argv: list[str] | None = None) -> int:
    # 必须在任何打印之前 —— 否则 dry-run 文案里的 emoji 会在 GBK 控制台上崩
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
