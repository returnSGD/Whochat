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
