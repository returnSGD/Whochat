"""传播分析 —— 传播曲线与传播路径。

和 `timeseries.py` 的分工：那边做**声量/情感**的时序（评论发布时间聚合），
这边做**传播结构**，依赖的是采集层的两组原始字段：

    metric_snapshots.like/comment/share + snapshot_time  → 传播曲线
    raw_content.parent_content_id                        → 传播路径 / 转发链

这两样都是"采集不可逆"的字段（方案文档 ADR#4）：内容被删、指标不再变化，
事后补不回来。采集层没抓到时，这里的函数**诚实地报数据不足**，
绝不拿别的数据编一条看起来合理的曲线出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 不保存时区，读回来是 naive —— 统一按 UTC 补上，否则相减会报错。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class CurvePoint:
    """传播曲线上的一个采样点。"""

    time: datetime
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0

    @property
    def engagement(self) -> int:
        """互动总量 = 赞 + 评 + 转。传播分析关心的是"热度的增长"，不是单项。"""
        return self.like_count + self.comment_count + self.share_count


@dataclass
class GrowthMetrics:
    """传播曲线导出的量化特征。"""

    available: bool
    reason: str = ""
    points: int = 0

    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_hours: float = 0.0

    start_value: int = 0
    end_value: int = 0
    peak_value: int = 0
    peak_time: datetime | None = None

    # 起爆点：增速最快的那一刻（不是量最大的那一刻）
    max_growth_per_hour: float = 0.0
    max_growth_time: datetime | None = None
    # 拐点：增速从高位回落（传播由加速转减速）
    inflection_time: datetime | None = None
    avg_growth_per_hour: float = 0.0
    summary: str = ""


def build_curve(rows: Sequence[dict]) -> list[CurvePoint]:
    """把 repository.snapshot_series() 的产出转成按时间升序的曲线点。"""
    points = []
    for r in rows:
        t = _aware(r.get("time"))
        if t is None:
            continue
        points.append(
            CurvePoint(
                time=t,
                like_count=int(r.get("like_count") or 0),
                comment_count=int(r.get("comment_count") or 0),
                share_count=int(r.get("share_count") or 0),
            )
        )
    return sorted(points, key=lambda p: p.time)


def analyze_growth(points: Sequence[CurvePoint]) -> GrowthMetrics:
    """从传播曲线提取起爆点 / 峰值 / 增速拐点。

    只有 1 个采样点是没有"传播"可言的 —— 那只是一张快照。
    诚实地返回不可用，而不是画一条水平线假装平稳。
    """
    if len(points) < 2:
        return GrowthMetrics(
            available=False,
            reason="该内容只有一个指标快照，画不出传播曲线（需要至少 2 次采集）",
            points=len(points),
        )

    pts = sorted(points, key=lambda p: p.time)
    start, end = pts[0], pts[-1]
    duration = (end.time - start.time).total_seconds() / 3600

    peak = max(pts, key=lambda p: p.engagement)

    # 逐段增速（互动量/小时）
    rates: list[tuple[datetime, float]] = []
    for prev, cur in zip(pts, pts[1:]):
        hours = (cur.time - prev.time).total_seconds() / 3600
        if hours <= 0:
            continue
        rates.append((cur.time, (cur.engagement - prev.engagement) / hours))

    max_rate, max_rate_time = 0.0, None
    if rates:
        max_rate_time, max_rate = max(rates, key=lambda x: x[1])

    # 增速拐点：起爆之后，增速首次回落到峰值的 30% 以下
    inflection = None
    if max_rate > 0 and max_rate_time is not None:
        after = [r for r in rates if r[0] > max_rate_time]
        for t, rate in after:
            if rate <= max_rate * 0.3:
                inflection = t
                break

    total_growth = end.engagement - start.engagement
    summary = (
        f"互动量 {start.engagement:,} → {end.engagement:,}（{total_growth:+,}），"
        f"历时 {duration:.1f} 小时"
    )
    if max_rate_time is not None:
        summary += f"；起爆点 {max_rate_time:%m-%d %H:%M}（{max_rate:,.0f}/小时）"
    if inflection is not None:
        summary += f"；{inflection:%m-%d %H:%M} 起增速回落"

    return GrowthMetrics(
        available=True,
        points=len(pts),
        start_time=start.time,
        end_time=end.time,
        duration_hours=round(duration, 2),
        start_value=start.engagement,
        end_value=end.engagement,
        peak_value=peak.engagement,
        peak_time=peak.time,
        max_growth_per_hour=round(max_rate, 1),
        max_growth_time=max_rate_time,
        inflection_time=inflection,
        avg_growth_per_hour=round(total_growth / duration, 1) if duration > 0 else 0.0,
        summary=summary,
    )


# ---------------------------------------------------------------- 传播路径


@dataclass
class GraphNode:
    content_id: str
    label: str
    platform: str
    followers: int = 0
    is_root: bool = False


def build_graph(contents: Sequence, *, max_nodes: int = 40) -> dict:
    """把内容的 parent_content_id 关系整理成可画图的节点/边。

    只保留引用了**本次数据集中存在**的父节点的边 —— 指向集外的悬空父引用
    会让图里出现无意义的孤立节点（真实采集里父内容可能没被同批抓到）。
    悬空数量一并返回，便于判断覆盖率。
    """
    by_id = {getattr(c, "content_id", None): c for c in contents}
    by_id.pop(None, None)

    edges = []
    dangling = 0
    children: set[str] = set()
    referenced: set[str] = set()

    for c in contents:
        cid = getattr(c, "content_id", None)
        parent = getattr(c, "parent_content_id", None)
        if not cid or not parent:
            continue
        if parent not in by_id:
            dangling += 1
            continue
        edges.append({"from": parent, "to": cid})
        children.add(cid)
        referenced.add(parent)

    # 节点 = 有边相连的内容；根 = 只被引用、自身不引用别人
    node_ids = list(dict.fromkeys([e["to"] for e in edges] + [e["from"] for e in edges]))
    node_ids = node_ids[:max_nodes]
    keep = set(node_ids)

    nodes = []
    for cid in node_ids:
        c = by_id[cid]
        nodes.append(
            GraphNode(
                content_id=cid,
                label=(getattr(c, "title", None) or cid)[:24],
                platform=getattr(c, "platform", "") or "",
                followers=getattr(c, "author_follower_count", 0) or 0,
                is_root=cid in referenced and cid not in children,
            )
        )

    return {
        "available": bool(edges),
        "nodes": nodes,
        "edges": [e for e in edges if e["from"] in keep and e["to"] in keep],
        "edge_count": len(edges),
        "root_count": sum(1 for n in nodes if n.is_root),
        "dangling_parents": dangling,
        "reason": "" if edges else "没有 parent_content_id 关系（转发/引用）可画",
    }


def to_dot(graph: dict) -> str:
    """生成 Graphviz DOT —— 看板直接交给 st.graphviz_chart 渲染，无需本地 graphviz。"""
    lines = [
        "digraph propagation {",
        '  rankdir="LR";',
        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei", fontsize=10];',
        '  edge [color="#9aa0a6"];',
    ]
    for n in graph["nodes"]:
        fill = "#dbeafe" if n.is_root else "#f3f4f6"
        label = n.label.replace('"', "'")
        lines.append(f'  "{n.content_id}" [label="{label}\\n({n.platform})", fillcolor="{fill}"];')
    for e in graph["edges"]:
        lines.append(f'  "{e["from"]}" -> "{e["to"]}";')
    lines.append("}")
    return "\n".join(lines)
