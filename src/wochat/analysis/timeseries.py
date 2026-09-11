"""时序与传播分析。

**时序标签是分析层的产物，传播结构依赖采集层的原始字段**（方案文档 §4.4）。

这里做的是「从时序标签里能榨出来的东西」：
    声量趋势、爆发点检测、拐点、情感随时间的漂移、事件阶段划分

⚠️ 传播路径 / KOL 识别**不能**在这里凭空造出来 —— 它依赖采集时抓到的
   `parent_content_id` 和 `author_follower_count`。如果采集层漏抓了，
   这里的函数再聪明也补不回来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Sequence


@dataclass
class TrendPoint:
    time: datetime
    total: int
    positive: int
    neutral: int
    negative: int

    @property
    def negative_ratio(self) -> float:
        return self.negative / self.total if self.total else 0.0

    @property
    def sentiment_index(self) -> float:
        """情感净值 = (正面 - 负面) / 总量，范围 [-1, 1]。"""
        return (self.positive - self.negative) / self.total if self.total else 0.0


@dataclass
class Burst:
    """爆发点。快通道预警的核心信号。"""

    time: datetime
    volume: int
    zscore: float
    direction: str  # surge | drop


def to_trend_points(rows: Sequence[dict]) -> list[TrendPoint]:
    """把 repository.trend_by_hour() 的产出转成 TrendPoint。

    repository 返回的是 dict（为了 JSON 序列化方便），这里转成有计算能力的对象。
    """
    points = []
    for r in rows:
        t = r.get("time")
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if t is None:
            continue
        points.append(
            TrendPoint(
                time=t,
                total=r.get("total", 0),
                positive=r.get("positive", 0),
                neutral=r.get("neutral", 0),
                negative=r.get("negative", 0),
            )
        )
    return sorted(points, key=lambda p: p.time)


def detect_bursts(points: Sequence[TrendPoint], window: int = 6, z_threshold: float = 2.5) -> list[Burst]:
    """基于滑动窗口的突变检测。

    Args:
        window: 用前多少个点作为基线
        z_threshold: 超过基线多少个标准差算爆发。2.5 比 2.0 保守，
                     舆情场景下宁可漏报也不要天天狼来了（告警疲劳会让预警失效）。

    这是**快通道**的核心算法 —— 不需要模型，毫秒级，可以在采集后立刻跑。
    """
    if len(points) < window + 1:
        return []

    bursts = []
    for i in range(window, len(points)):
        baseline = [p.total for p in points[i - window : i]]
        recent = points[i].total

        mu = mean(baseline) if baseline else 0
        sigma = pstdev(baseline) if len(baseline) > 1 else 0

        # 基线太平（std=0）时 z-score 无意义，改用「相对基线」的规则。
        #
        # surge 沿用最小绝对量门槛 max(10, mu*3)，否则 1 条涨到 3 条也会告警。
        #
        # drop 是补上的：爬虫挂掉或事件自然结束时，声量会从稳定基线断崖式
        # 跌到 0。只报 surge 会把这个「这一波结束了 / 我们丢数据了」的信号
        # 静默丢掉。规则：基线本身要有量（mu ≥ 10）且最近点跌到基线一半
        # （≤ 50%）以下。取 50% 是因为退潮通常是断崖而非微跌，一半的降幅
        # 足够显著；同时 recent == mu 不触发，纯平稳序列依旧零告警。
        # zscore 用 ±inf 标记「基线无波动」，与 surge 分支保持一致。
        if sigma == 0:
            if recent >= max(10, mu * 3):
                bursts.append(Burst(points[i].time, recent, float("inf"), "surge"))
            elif mu >= 10 and recent <= mu * 0.5:
                bursts.append(Burst(points[i].time, recent, float("-inf"), "drop"))
            continue

        z = (recent - mu) / sigma
        if z >= z_threshold:
            bursts.append(Burst(points[i].time, recent, round(z, 2), "surge"))
        elif z <= -z_threshold:
            bursts.append(Burst(points[i].time, recent, round(z, 2), "drop"))

    return bursts


def sentiment_drift(points: Sequence[TrendPoint], window: int = 6) -> float:
    """情感漂移：最近窗口 vs 之前窗口的情感净值变化。

    负值表示舆情在恶化 —— 这个指标比单纯的负面数量更能反映趋势，
    因为它对整体声量变化不敏感。
    """
    if len(points) < window * 2:
        return 0.0
    recent = mean(p.sentiment_index for p in points[-window:])
    previous = mean(p.sentiment_index for p in points[-window * 2 : -window])
    return round(recent - previous, 4)


def classify_stage(points: Sequence[TrendPoint]) -> str:
    """事件阶段划分 —— 萌芽 / 爆发 / 平台 / 衰退。

    用声量的相对变化率判断，不依赖绝对阈值（不同事件的量级差几个数量级）。
    """
    if len(points) < 4:
        return "数据不足"

    half = len(points) // 2
    first = sum(p.total for p in points[:half])
    second = sum(p.total for p in points[half:])
    recent = [p.total for p in points[-3:]]
    recent_avg = mean(recent) if recent else 0
    peak = max(p.total for p in points)

    if second < first * 0.4:
        return "衰退期"
    elif recent_avg >= peak * 0.85:
        # 高位横盘
        if pstdev(recent) < recent_avg * 0.2 if len(recent) > 1 else True:
            return "平台期"
        return "爆发期"
    elif second > first * 2:
        return "爆发期"
    elif peak < 10:
        return "萌芽期"
    return "平台期"


def propagation_metrics(comments: Sequence, contents: dict, top_n: int = 10) -> dict:
    """传播结构指标 —— **前提是采集层抓了 parent_content_id 和 follower_count**。

    如果采集层漏抓了这两个字段，这个函数会诚实地说"数据不足"，
    而不是编一个看起来合理的数字出来。
    """
    if not contents:
        return {"available": False, "reason": "没有内容数据"}

    has_parent = sum(1 for c in contents.values() if getattr(c, "parent_content_id", None))
    has_followers = sum(1 for c in contents.values() if getattr(c, "author_follower_count", None))

    if has_parent == 0 and has_followers == 0:
        return {
            "available": False,
            "reason": (
                "采集层未抓到 parent_content_id / author_follower_count，"
                "无法做传播路径与 KOL 识别。"
                "注意：这两个字段事后无法补 —— 社媒历史数据重爬成本极高甚至不可能（内容已删）。"
            ),
        }

    # KOL 识别：按粉丝数排序的头部账号
    ranked = sorted(
        contents.values(),
        key=lambda c: getattr(c, "author_follower_count", 0) or 0,
        reverse=True,
    )[:top_n]

    kols = [
        {
            "content_id": c.content_id,
            "author_name": getattr(c, "author_name", None),
            "followers": getattr(c, "author_follower_count", 0),
            "like_count": getattr(c, "like_count", 0),
            "platform": getattr(c, "platform", None),
        }
        for c in ranked
    ]

    # 传播路径：parent → child 的边
    edges = [
        {"from": c.parent_content_id, "to": c.content_id}
        for c in contents.values()
        if getattr(c, "parent_content_id", None)
    ]

    return {
        "available": True,
        "kol_count": len(kols),
        "kols": kols,
        "edge_count": len(edges),
        "edges": edges[:200],
        "has_parent_ratio": round(has_parent / len(contents), 3),
        "has_follower_ratio": round(has_followers / len(contents), 3),
    }
