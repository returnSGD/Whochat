"""传播分析的测试 —— 曲线、增长特征、传播路径。

原则：采集层没抓到的字段就诚实报"数据不足"，绝不拿别的数据编一条曲线。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wochat.analysis.propagation import (
    CurvePoint,
    analyze_growth,
    build_curve,
    build_graph,
    to_dot,
)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


class TestBuildCurve:
    def test_sorts_and_computes_engagement(self):
        rows = [
            {"time": T0 + timedelta(hours=2), "like_count": 30, "comment_count": 3, "share_count": 2},
            {"time": T0, "like_count": 10, "comment_count": 1, "share_count": 0},
        ]
        pts = build_curve(rows)
        assert [p.time for p in pts] == [T0, T0 + timedelta(hours=2)]
        assert pts[0].engagement == 11
        assert pts[1].engagement == 35

    def test_naive_datetimes_are_treated_as_utc(self):
        """SQLite 读回来是 naive，不补时区会在相减时直接抛 TypeError。"""
        rows = [
            {"time": datetime(2026, 1, 1, 0, 0), "like_count": 1},
            {"time": datetime(2026, 1, 1, 1, 0), "like_count": 2},
        ]
        m = analyze_growth(build_curve(rows))
        assert m.available is True
        assert m.duration_hours == 1.0


class TestAnalyzeGrowth:
    def test_single_point_is_unavailable(self):
        m = analyze_growth([CurvePoint(time=T0, like_count=10)])
        assert m.available is False
        assert "快照" in m.reason

    def test_detects_peak_burst_and_inflection(self):
        pts = [
            CurvePoint(time=T0 + timedelta(hours=i), like_count=v)
            for i, v in enumerate([0, 10, 100, 110, 115])
        ]
        m = analyze_growth(pts)
        assert m.available is True
        assert m.peak_time == T0 + timedelta(hours=4), "峰值是量最大的那一刻"
        assert m.max_growth_time == T0 + timedelta(hours=2), "起爆点是增速最快的那一刻"
        assert m.max_growth_per_hour == 90.0
        assert m.inflection_time == T0 + timedelta(hours=3), "起爆后增速回落即为拐点"
        assert m.start_value == 0 and m.end_value == 115

    def test_monotonic_flat_series_has_no_inflection(self):
        pts = [
            CurvePoint(time=T0 + timedelta(hours=i), like_count=v)
            for i, v in enumerate([0, 100, 200, 300])
        ]
        m = analyze_growth(pts)
        # 增速恒定，从未回落到峰值 30% 以下 → 没有拐点
        assert m.available is True
        assert m.inflection_time is None


class TestBuildGraph:
    def _c(self, cid, parent=None, title=None, followers=0):
        from types import SimpleNamespace

        return SimpleNamespace(
            content_id=cid,
            parent_content_id=parent,
            title=title or cid,
            platform="xhs",
            author_follower_count=followers,
        )

    def test_dangling_parents_are_skipped_not_drawn(self):
        contents = [self._c("a"), self._c("b", parent="missing"), self._c("c", parent="a")]
        g = build_graph(contents)

        assert g["available"] is True
        assert g["edges"] == [{"from": "a", "to": "c"}], "父节点不在本批数据里的边不能画成孤立节点"
        assert g["dangling_parents"] == 1
        assert {n.content_id for n in g["nodes"]} == {"a", "c"}
        assert next(n for n in g["nodes"] if n.content_id == "a").is_root is True
        assert next(n for n in g["nodes"] if n.content_id == "c").is_root is False

    def test_no_parents_reports_unavailable(self):
        g = build_graph([self._c("a"), self._c("b")])
        assert g["available"] is False
        assert "parent_content_id" in g["reason"]

    def test_dot_escapes_quotes(self):
        g = build_graph([self._c("a", title='含"引号"的标题'), self._c("b", parent="a")])
        dot = to_dot(g)
        assert "digraph propagation" in dot
        assert '"a" -> "b"' in dot
        assert '含"引号"' not in dot, "标题里的双引号必须转义，否则 DOT 语法会被破坏"


class TestRepositorySnapshots:
    def test_series_is_time_sorted(self, repo):
        repo.add_snapshots(
            [
                {"content_id": "c1", "snapshot_time": T0 + timedelta(hours=1), "like_count": 20},
                {"content_id": "c1", "snapshot_time": T0, "like_count": 10},
            ]
        )
        series = repo.snapshot_series("c1")
        assert [s["like_count"] for s in series] == [10, 20]

    def test_contents_with_snapshots_filters_single_point(self, repo):
        repo.add_snapshots(
            [
                {"content_id": "c1", "snapshot_time": T0, "like_count": 1},
                {"content_id": "c1", "snapshot_time": T0 + timedelta(hours=1), "like_count": 2},
                {"content_id": "c2", "snapshot_time": T0, "like_count": 9},
            ]
        )
        rows = repo.contents_with_snapshots(min_points=2)
        assert [r["content_id"] for r in rows] == ["c1"]
        assert rows[0]["points"] == 2


class TestMockProducesPropagationChain:
    def test_mock_has_valid_parent_references(self):
        """回归：mock 曾经把 parent_content_id 恒置 None，
        传播路径这条链路在 demo 里从未被跑过，做出来也没数据可验证。"""
        from wochat.crawler.base import CrawlTask
        from wochat.crawler.mock_source import MockSource

        task = CrawlTask(platform="mock", mode="keyword", target="某品牌", max_items=600)
        contents = [r for r in MockSource().crawl(task) if "content_id" in r]

        ids = {r["content_id"] for r in contents}
        parents = [r["parent_content_id"] for r in contents if r.get("parent_content_id")]
        assert parents, "mock 应产出转发/引用关系，供传播路径分析验证"
        assert all(p in ids for p in parents), "父引用必须指向同批内容，不能悬空"
