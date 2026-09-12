"""Repository 写入语义的回归测试。

重点是 upsert 的"只增不改"承诺：后续采集缺字段时，**不能**用 None 把库里
已有的有效值清空。最典型的是 publish_time 被清成 NULL —— 该评论从此掉出
所有按时间窗的查询（`comments_in_window` 要求 publish_time IS NOT NULL），
快通道漏警、趋势图失真，且再也不会恢复。
"""

from __future__ import annotations

from datetime import datetime, timezone

from Whochat.store.models import Comment, RawContent


def _comment(**over) -> dict:
    row = dict(
        comment_id="c1",
        content_id="x1",
        platform="xhs",
        text="还行",
        publish_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        like_count=10,
    )
    row.update(over)
    return row


class TestUpsertDoesNotClobberWithNone:
    def test_comment_publish_time_survives_partial_recrawl(self, repo):
        repo.upsert_comments([_comment()])
        # 第二次采集该字段缺失（别名没命中 / 平台没返回）→ publish_time 为 None
        repo.upsert_comments([_comment(publish_time=None, like_count=None)])

        c = repo.session.get(Comment, "c1")
        assert c.publish_time is not None, "缺字段的重复采集不应清空已有时间戳"
        assert c.like_count == 10, "缺字段也不应清空已有指标"

    def test_comment_non_none_values_still_update(self, repo):
        repo.upsert_comments([_comment()])
        repo.upsert_comments([_comment(text="改过的文本", like_count=99)])

        c = repo.session.get(Comment, "c1")
        assert c.text == "改过的文本"
        assert c.like_count == 99

    def test_content_publish_time_survives_partial_recrawl(self, repo):
        repo.upsert_contents(
            [
                dict(
                    content_id="x1",
                    platform="xhs",
                    title="原标题",
                    publish_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    author_follower_count=500,
                )
            ]
        )
        repo.upsert_contents(
            [dict(content_id="x1", platform="xhs", title="新标题", publish_time=None, author_follower_count=None)]
        )

        c = repo.session.get(RawContent, "x1")
        assert c.publish_time is not None
        assert c.author_follower_count == 500
        assert c.title == "新标题"


class TestSearchKeywordAttribution:
    def test_later_keyword_does_not_rewrite_attribution(self, repo):
        """同一条内容可能被多个监控词命中，后来者覆盖前者会让
        "这条舆情是被哪个词发现的"永久错乱 —— 归因一经确立不再改写。"""
        repo.upsert_contents([dict(content_id="x1", platform="xhs", search_keyword="关键词A")])
        repo.upsert_contents([dict(content_id="x1", platform="xhs", search_keyword="关键词B")])

        assert repo.session.get(RawContent, "x1").search_keyword == "关键词A"

    def test_attribution_filled_in_when_previously_missing(self, repo):
        repo.upsert_contents([dict(content_id="x1", platform="xhs")])
        repo.upsert_contents([dict(content_id="x1", platform="xhs", search_keyword="关键词B")])

        assert repo.session.get(RawContent, "x1").search_keyword == "关键词B"


class TestLatestAnalysisVersion:
    def test_none_when_empty(self, repo):
        assert repo.latest_analysis_version() is None
