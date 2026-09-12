"""字段归一化的测试。

这一层是纯函数，且是所有外部数据的入口 —— 出错会静默污染全链路，
所以用例优先覆盖"平台给的脏格式"。
"""

from __future__ import annotations

import pytest

from wochat.crawler.normalize import (
    normalize_comment,
    normalize_content,
    parse_bool,
    parse_count,
)


class TestParseCount:
    """各平台的计数格式五花八门，解析错了看板数字就全错。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.2万", 12_000),
            ("1.5w", 15_000),
            ("1.5W", 15_000),
            ("3,456", 3_456),
            ("1234", 1_234),
            (1234, 1_234),
            ("1.2亿", 120_000_000),
            ("2k", 2_000),
        ],
    )
    def test_parses_platform_formats(self, raw, expected):
        assert parse_count(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "暂无", "－"])
    def test_missing_values_become_none(self, raw):
        assert parse_count(raw) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("约1.2万", 12_000),
            ("超过3万", 30_000),
            # 畸形值必须返回 None，而不是静默截出错误数字
            ("1.2.3", None),
            ("1e3", None),
            ("1.2万3", None),
        ],
    )
    def test_malformed_values_are_not_misparsed(self, raw, expected):
        assert parse_count(raw) == expected


class TestParseBool:
    @pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "y", "是", "v", "verified", 1, True])
    def test_truthy(self, raw):
        assert parse_bool(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "n", "否", 0, False])
    def test_falsy(self, raw):
        assert parse_bool(raw) is False

    def test_unknown_is_none(self):
        # 未知值必须是 None 而不是 False —— 否则"没这个字段"和"明确为假"分不开
        assert parse_bool("maybe") is None


class TestNormalizeContent:
    BASE = {"note_id": "N1", "title": "标题", "desc": "正文", "user_id": "RAW_USER_123"}

    def test_returns_none_without_id(self):
        assert normalize_content({"title": "没有 id"}, "xhs") is None

    def test_content_author_id_is_anonymized(self):
        """回归：内容侧曾经直接把平台原始 user_id 落库，只有评论侧做了脱敏。

        合规要求是"不存储平台原始用户 ID"（方案文档 §12），
        内容发布者同样是自然人，不能漏。
        """
        rec = normalize_content(self.BASE, "xhs")
        assert rec["author_id"] != "RAW_USER_123"
        assert rec["author_id"] is not None

    def test_anonymization_is_stable(self):
        """同一个人要能聚合，所以哈希必须可复现。"""
        a = normalize_content(self.BASE, "xhs")
        b = normalize_content(self.BASE, "xhs")
        assert a["author_id"] == b["author_id"]

    def test_missing_author_stays_none(self):
        rec = normalize_content({"note_id": "N1", "title": "t"}, "xhs")
        assert rec["author_id"] is None

    def test_duplicate_title_and_body_drops_title(self):
        rec = normalize_content({"note_id": "N1", "title": "一样", "desc": "一样"}, "xhs")
        assert rec["title"] is None
        assert rec["body_text"] == "一样"

    def test_raw_json_is_preserved(self):
        """采集不可逆，raw_json 是唯一的后悔药，任何情况下都不能丢。"""
        rec = normalize_content(self.BASE, "xhs")
        assert rec["raw_json"] == self.BASE


class TestNormalizeComment:
    def test_returns_none_without_text(self):
        assert normalize_comment({"comment_id": "c1"}, "xhs") is None

    def test_returns_none_without_parent_content(self):
        # 评论必须挂在一条内容下，否则无法归属
        assert normalize_comment({"comment_id": "c1", "content": "你好"}, "xhs") is None

    def test_author_id_is_anonymized(self):
        rec = normalize_comment(
            {"comment_id": "c1", "content": "你好", "note_id": "N1", "user_id": "RAW_USER_9"},
            "xhs",
        )
        assert rec["author_id"] != "RAW_USER_9"

    def test_level_derived_from_parent(self):
        base = {"comment_id": "c1", "content": "你好", "note_id": "N1"}
        assert normalize_comment(base, "xhs")["level"] == 1
        assert normalize_comment({**base, "parent_comment_id": "c0"}, "xhs")["level"] == 2

    def test_alias_fallback(self):
        """字段名变了也要能命中别的候选，这是别名表存在的意义。"""
        rec = normalize_comment({"rpid": "r1", "text": "换平台字段名了", "note_id": "N1"}, "bilibili")
        assert rec["comment_id"] == "r1"

    def test_bilibili_top_level_parent_zero_is_not_a_parent(self):
        """回归：bilibili 顶层评论固定写 parent_comment_id="0"，
        之前被判为有父评论 → 全部一级评论误标 level=2 并挂到不存在的父 "0"。"""
        rec = normalize_comment(
            {"comment_id": "c1", "content": "内容", "video_id": "BV1", "parent_comment_id": "0"},
            "bilibili",
        )
        assert rec["level"] == 1
        assert rec["parent_comment_id"] is None


class TestMediaCrawlerRealFields:
    """MediaCrawler 各平台输出的真实字段名 —— 别名表必须覆盖，
    否则真实采集的作者/指标字段会静默为 NULL。"""

    def test_content_creator_hash_and_nickname(self):
        rec = normalize_content(
            {"note_id": "N1", "creator_hash": "HASH1", "nickname": "某人", "desc": "正文"}, "xhs"
        )
        assert rec["author_name"] == "某人"
        assert rec["author_id"] is not None
        assert rec["author_id"] != "HASH1", "平台哈希也要再脱敏一次，保持全库口径一致"

    def test_zhihu_content_fields(self):
        rec = normalize_content(
            {
                "content_id": "A1",
                "content_type": "answer",
                "content_text": "知乎正文",
                "created_time": 1700000000,
                "creator_hash": "H",
                "user_nickname": "知友",
                "content_url": "https://www.zhihu.com/x",
                "voteup_count": 5,
            },
            "zhihu",
        )
        assert rec["body_text"] == "知乎正文"
        assert rec["publish_time"] is not None
        assert rec["author_name"] == "知友"
        assert rec["content_type"] == "answer"
        assert rec["url"] == "https://www.zhihu.com/x"

    def test_bilibili_metric_fields(self):
        rec = normalize_content(
            {
                "video_id": "BV1",
                "title": "标题",
                "video_comment": "12",
                "video_share_count": "3",
                "video_favorite_count": "4",
                "video_type": "video",
                "creator_hash": "H",
                "nickname": "UP主",
            },
            "bilibili",
        )
        assert rec["comment_count"] == 12
        assert rec["share_count"] == 3
        assert rec["collect_count"] == 4
        assert rec["content_type"] == "video"

    def test_weibo_comment_fields(self):
        rec = normalize_comment(
            {
                "comment_id": "c1",
                "content": "内容",
                "note_id": "N1",
                "comment_like_count": "7",
                "creator_hash": "H",
                "user_nickname": "甲",
            },
            "weibo",
        )
        assert rec["like_count"] == 7
        assert rec["author_id"] is not None
