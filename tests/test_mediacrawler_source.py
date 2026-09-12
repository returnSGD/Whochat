"""MediaCrawler 适配器的回归测试。

重点：**没有新产出时不能把历史文件重读一遍**。MediaCrawler 按天复用同一个
文件名，同一天第二次跑是往已存在的文件里追加；而适配器以前用"文件名集合差"
判断新文件，差集为空就回退读取本平台**所有日期**的文件 —— 于是每次运行都把
全量历史重新灌一遍：旧内容的 search_keyword 被当前关键词改写，快照表被灌水。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from Whochat.crawler.base import CrawlTask
from Whochat.crawler.mediacrawler_source import MediaCrawlerSource


@pytest.fixture
def mc(tmp_path, monkeypatch):
    """造一个最小的 MediaCrawler 目录 + 输出目录。"""
    crawler_dir = tmp_path / "mc"
    crawler_dir.mkdir()
    (crawler_dir / "main.py").write_text("", encoding="utf-8")

    output_dir = tmp_path / "out"
    jsonl_dir = output_dir / "xhs" / "jsonl"
    jsonl_dir.mkdir(parents=True)

    src = MediaCrawlerSource(crawler_dir=crawler_dir, output_dir=output_dir)

    import Whochat.crawler.mediacrawler_source as mod

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    return src, jsonl_dir


def _task() -> CrawlTask:
    return CrawlTask(platform="xhs", mode="keyword", target="监控词")


def _write(path, record):
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def test_no_new_output_yields_nothing(mc):
    """本次跑没有写入任何文件 → 不应把历史文件当成本次产出。"""
    src, jsonl_dir = mc
    _write(jsonl_dir / "search_contents_2026-01-01.jsonl", {"note_id": "OLD", "title": "旧内容"})

    assert list(src.crawl(_task())) == []


def test_old_days_not_reread_when_new_file_appears(mc, monkeypatch):
    """有新文件时，只读新文件，不能把别的日期的历史一并重读。"""
    src, jsonl_dir = mc
    _write(jsonl_dir / "search_contents_2026-01-01.jsonl", {"note_id": "OLD", "title": "旧内容"})

    import Whochat.crawler.mediacrawler_source as mod

    def fake_run(*a, **k):
        _write(jsonl_dir / "search_contents_2026-09-12.jsonl", {"note_id": "NEW", "title": "新内容"})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    ids = {rec["content_id"] for rec in src.crawl(_task())}
    assert ids == {"NEW"}, "只应产出本次真正写入的文件里的记录"


def test_appended_same_day_file_is_detected(mc, monkeypatch):
    """同一天追加到已存在的文件（文件名不变，只有 mtime 变）也要能被读到。"""
    src, jsonl_dir = mc
    path = jsonl_dir / "search_contents_2026-09-12.jsonl"
    _write(path, {"note_id": "FIRST", "title": "第一条"})

    import Whochat.crawler.mediacrawler_source as mod

    def fake_run(*a, **k):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"note_id": "SECOND", "title": "第二条"}, ensure_ascii=False) + "\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    ids = {rec["content_id"] for rec in src.crawl(_task())}
    assert ids == {"FIRST", "SECOND"}


def test_timeout_is_bounded_and_reads_partial(mc, monkeypatch):
    """子进程必须带硬超时 —— 否则卡死的 MediaCrawler 会把整轮采集拖死。

    超时不重试：卡住的任务重跑一次大概率还是卡住，代价是又一个 timeout。
    """
    src, jsonl_dir = mc
    import subprocess

    import Whochat.crawler.mediacrawler_source as mod

    timeouts: list[int | None] = []

    def fake_run(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert list(src.crawl(_task())) == []
    assert timeouts == [src.timeout], "应传入配置的超时且超时后不再重试"


def test_retries_only_when_nothing_was_produced(mc, monkeypatch):
    """零产出（秒退/抖动）才重试；有产出就直接读，不重复跑。"""
    src, jsonl_dir = mc
    import Whochat.crawler.mediacrawler_source as mod
    from Whochat.config import settings

    monkeypatch.setattr(settings.crawl, "max_retries", 3)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)  # 测试里不等退避

    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(returncode=1)  # 第一次零产出
        _write(
            jsonl_dir / "search_contents_2026-09-13.jsonl",
            {"note_id": "NEW", "title": "第二次才抓到"},
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    ids = {rec["content_id"] for rec in src.crawl(_task())}
    assert ids == {"NEW"}
    assert calls["n"] == 2, "第一次零产出后重试一次即可"


def test_no_retry_when_output_exists_even_with_nonzero_exit(mc, monkeypatch):
    """有部分产出时即使退出码非 0 也不重试（落库幂等，重跑只会更慢）。"""
    src, jsonl_dir = mc
    import Whochat.crawler.mediacrawler_source as mod
    from Whochat.config import settings

    monkeypatch.setattr(settings.crawl, "max_retries", 3)
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        _write(
            jsonl_dir / "search_contents_2026-09-13.jsonl",
            {"note_id": "PARTIAL", "title": "被风控中断但已有一半"},
        )
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    ids = {rec["content_id"] for rec in src.crawl(_task())}
    assert ids == {"PARTIAL"}
    assert calls["n"] == 1
