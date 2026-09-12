"""人工导入的测试。

README 里演示的就是 `Whochat import xxx.jsonl --platform xhs`（不带 --keyword），
所以这条路径必须有回归保护 —— 它曾经在数据已落库之后崩掉。
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from Whochat.crawler.base import CrawlTask
from Whochat.crawler.manual_source import ManualImportSource
from Whochat.pipeline.runner import Pipeline


def _crawl(path, platform="xhs", keyword=None):
    task = CrawlTask(platform=platform, mode="keyword", target=keyword)
    return list(ManualImportSource(path).crawl(task))


class TestClassification:
    def test_comment_aliases_are_recognised(self, tmp_path):
        """回归：只认字面量 "comment_id" 会让 cid/rpid/tid 的记录被误判成内容
        或被静默丢弃 —— 别名表声称支持它们，导入路径却到不了。"""
        p = tmp_path / "a.jsonl"
        p.write_text(
            "\n".join(
                [
                    json.dumps({"cid": "cm1", "text": "换平台字段名了", "note_id": "n1"}),
                    json.dumps({"rpid": "rp1", "content": "B站的字段名", "note_id": "n1"}),
                ]
            ),
            encoding="utf-8",
        )
        recs = _crawl(p)
        assert {r["comment_id"] for r in recs} == {"cm1", "rp1"}

    def test_content_with_title_is_content(self, tmp_path):
        p = tmp_path / "b.jsonl"
        p.write_text(
            json.dumps({"id": "x1", "title": "标题", "content": "正文", "note_id": "n9"}) + "\n",
            encoding="utf-8",
        )
        recs = _crawl(p)
        assert len(recs) == 1
        assert "comment_id" not in recs[0], "带 title 的记录不该被当成评论"
        # content_id 取的是别名表里优先级更高的 note_id，而不是 id
        assert recs[0]["content_id"] == "n9"
        assert recs[0]["title"] == "标题"

    def test_unparseable_record_is_skipped_not_crashed(self, tmp_path, capsys):
        p = tmp_path / "c.jsonl"
        p.write_text(json.dumps({"foo": "bar"}) + "\n", encoding="utf-8")
        recs = _crawl(p)
        assert recs == []
        assert "跳过" in capsys.readouterr().out


class TestEncoding:
    def test_gbk_csv_does_not_crash(self, tmp_path):
        """中文 Windows 上 Excel 默认导出 GBK/ANSI CSV，
        写死 utf-8-sig 会抛 UnicodeDecodeError 中断整条导入。"""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerows(
            [
                ["comment_id", "content", "note_id"],
                ["g1", "这个产品很好用，推荐", "n1"],
                ["g2", "发热严重，太失望了", "n1"],
            ]
        )
        p = tmp_path / "gbk.csv"
        p.write_bytes(buf.getvalue().encode("gbk"))

        recs = _crawl(p)
        assert len(recs) == 2
        assert "很好用" in recs[0]["text"]

    def test_gbk_jsonl_does_not_crash(self, tmp_path):
        p = tmp_path / "gbk.jsonl"
        p.write_bytes(
            json.dumps({"cid": "c9", "text": "国标编码评论", "note_id": "n1"}, ensure_ascii=False).encode("gbk")
        )
        recs = _crawl(p)
        assert len(recs) == 1


class TestDumpRaw:
    def test_import_without_keyword_does_not_crash(self, repo, tmp_path):
        """回归：README 的用法不带 --keyword，task.target 为 None，
        旧代码迭代 None 直接 TypeError，导入在数据落库后崩掉。"""
        from Whochat.crawler.manual_source import register_manual

        p = tmp_path / "d.jsonl"
        p.write_text(
            json.dumps({"cid": "k1", "text": "没有关键词也要能导入", "note_id": "n1"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        src = register_manual(p, "xhs")
        task = CrawlTask(platform="xhs", mode="keyword", target=None)
        stats = Pipeline(repo).crawl(task, source_name=src.name)
        assert stats.stored_comments == 1
