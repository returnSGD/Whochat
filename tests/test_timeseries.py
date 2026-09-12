"""时序 / 传播分析的回归测试。

传播分析的红线是**诚实**：采集层没抓到的字段就明说做不了，
绝不能拿全 0/None 的数据凑一个"看起来有模有样"的榜单出来。
"""

from __future__ import annotations

from types import SimpleNamespace

from wochat.analysis.timeseries import propagation_metrics


def _content(cid: str, *, parent=None, followers=None):
    return SimpleNamespace(
        content_id=cid,
        parent_content_id=parent,
        author_follower_count=followers,
        author_name=f"作者{cid}",
        like_count=1,
        platform="xhs",
    )


class TestPropagationHonesty:
    def test_no_fields_at_all_reports_unavailable(self):
        contents = {"a": _content("a")}
        m = propagation_metrics([], contents)
        assert m["available"] is False
        assert "parent_content_id" in m["reason"]

    def test_no_followers_does_not_fabricate_kol_list(self):
        """回归：只抓了 parent_content_id、没抓粉丝数时，
        以前会因为 has_parent > 0 而输出一个按全 0 粉丝数排序的"影响力账号 TOP"。"""
        contents = {
            "a": _content("a", parent="root"),
            "b": _content("b", parent="root"),
        }
        m = propagation_metrics([], contents)

        assert m["available"] is True, "有 parent 就能做传播路径"
        assert m["kol_available"] is False
        assert m["kols"] == [], "没有粉丝数就不该产出 KOL 榜"
        assert m["edge_count"] == 2
        assert any("author_follower_count" in x for x in m["missing"])

    def test_no_parent_does_not_fabricate_edges(self):
        contents = {"a": _content("a", followers=1000)}
        m = propagation_metrics([], contents)

        assert m["available"] is True
        assert m["kol_available"] is True
        assert len(m["kols"]) == 1
        assert m["edge_count"] == 0
        assert any("parent_content_id" in x for x in m["missing"])

    def test_full_fields_yield_both(self):
        contents = {
            "a": _content("a", parent="root", followers=1000),
            "b": _content("b", parent="root", followers=50),
        }
        m = propagation_metrics([], contents)

        assert m["kol_available"] is True
        assert [k["followers"] for k in m["kols"]] == [1000, 50]
        assert m["edge_count"] == 2
        assert m["missing"] == []
