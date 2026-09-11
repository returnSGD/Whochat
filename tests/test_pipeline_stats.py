"""流水线幂等性与统计口径的测试。

这一块曾经有个很隐蔽的 bug：被过滤的评论不落任何记录 → 下一轮又被当成
"待分析"取出来 → 而它的重复孪生已经入库、不在去重池里 → 这次反而被
当正常评论分析掉。**每重跑一次，统计就更脏一点。**
"""

from __future__ import annotations

from wochat.pipeline.runner import Pipeline
from wochat.store.models import utcnow

DUP = "这个产品真的很好用，强烈推荐购买"
NEG = "发热严重，售后也联系不上，太失望了"
POS = "物流很快，包装完好，客服态度也不错"
SPAM = "加V信 abc12345 领取优惠券"


def _seed(repo):
    rows = [
        dict(comment_id="d1", content_id="c1", platform="xhs", text=DUP, publish_time=utcnow()),
        dict(comment_id="d2", content_id="c1", platform="xhs", text=DUP, publish_time=utcnow()),
        dict(comment_id="n1", content_id="c1", platform="xhs", text=NEG, publish_time=utcnow()),
        dict(comment_id="p1", content_id="c1", platform="xhs", text=POS, publish_time=utcnow()),
        dict(comment_id="s1", content_id="c1", platform="xhs", text=SPAM, publish_time=utcnow()),
    ]
    repo.upsert_comments(rows)


class TestIdempotency:
    def test_second_run_analyzes_nothing(self, repo):
        """重跑必须没有新工作 —— 否则每跑一次都会多放一批本该被过滤的评论进来。"""
        _seed(repo)
        pipe = Pipeline(repo)

        first = pipe.analyze()
        assert first.analyzed == 3, "d1/d2 留一条、n1、p1 有效；s1 是广告"
        assert first.dropped_dup == 1
        assert first.dropped_spam == 1

        second = pipe.analyze()
        assert second.analyzed == 0, "重跑不应再分析出任何东西"
        assert second.dropped_dup == 0
        assert second.dropped_spam == 0

    def test_third_run_still_stable(self, repo):
        _seed(repo)
        pipe = Pipeline(repo)
        for _ in range(3):
            pipe.analyze()
        s = repo.stats()
        assert s["comments"] == 5
        assert s["analyses"] == 3, "有效分析数不应随重跑增长"


class TestStatsInvariant:
    def test_valid_plus_rejected_equals_comments(self, repo):
        """每条评论最终都要有一个归宿：要么有效分析，要么被淘汰标记。

        这个不变式是幂等性的保证 —— 一旦有评论两边都不沾，
        它下一轮就会被重新取出来分析。
        """
        _seed(repo)
        Pipeline(repo).analyze()

        s = repo.stats()
        assert s["analyses"] + s["rejected"] == s["comments"]
        assert s["analyses"] == 3
        assert s["rejected"] == 2  # d2 重复 + s1 广告

    def test_rejected_rows_do_not_pollute_distribution(self, repo):
        """淘汰行只是"已处理"的标记，不能计入情感分布。"""
        _seed(repo)
        pipe = Pipeline(repo)
        pipe.analyze()

        dist = repo.sentiment_distribution(pipe.version)
        assert sum(dist.values()) == 3
        assert set(dist) <= {"positive", "neutral", "negative"}

    def test_rejected_rows_do_not_pollute_keywords(self, repo):
        repo.upsert_comments(
            [
                dict(comment_id="k1", content_id="c1", platform="xhs", text=POS, publish_time=utcnow()),
                dict(comment_id="k2", content_id="c1", platform="xhs", text=SPAM, publish_time=utcnow()),
            ]
        )
        pipe = Pipeline(repo)
        pipe.analyze()

        kws = repo.keyword_frequencies(pipe.version)
        assert all("abc12345" not in k for k in kws), "广告词不应出现在高频词里"


class TestAnalyzedTexts:
    def test_rejected_rows_excluded(self, repo):
        """主题建模/词云的语料也必须排除淘汰行。"""
        _seed(repo)
        pipe = Pipeline(repo)
        pipe.analyze()

        texts = repo.analyzed_texts(pipe.version)
        assert len(texts) == 3
        assert not any("abc12345" in t for t in texts)
