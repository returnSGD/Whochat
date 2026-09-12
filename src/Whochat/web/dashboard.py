"""看板端 —— 只读可视化（双入口之一，见 web/app.py）。

**关于"数据中台"的说明（方案文档 §5.1）**：
这个是**可视化看板**，不是数据中台。真正的中台是数据资产管理 + 服务层。
单人本地工具去建真中台是过度工程化 —— 那是这类项目最大的死因。

但保留了存储层 / 展示层的边界：**这个页面只通过 Repository 读数据，
不直接碰数据库文件、不读爬虫原始 JSON**。所以将来换 UI、加缓存、
迁 PostgreSQL，都不需要动数据层。

⚠️ **措辞约束**：评论区只代表"愿意评论的人"，天然偏向极端情绪。
   页面上不能写"公众情绪偏负面"，只能写"讨论区情绪分布"。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Streamlit 直接 `streamlit run app.py` 时不会走包安装的路径，手动补上
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402

from datetime import timedelta  # noqa: E402

from Whochat.analysis import propagation as prop  # noqa: E402
from Whochat.analysis import timeseries as ts  # noqa: E402
from Whochat.config import EXPORT_DIR, settings  # noqa: E402
from Whochat.store.models import utcnow  # noqa: E402
from Whochat.store.repository import Repository, init_db  # noqa: E402

_SENTIMENT_COLORS = {"positive": "#2e9e5b", "neutral": "#9aa0a6", "negative": "#d93f3f"}
_SENTIMENT_CN = {"positive": "正面", "neutral": "中性", "negative": "负面"}


@st.cache_resource
def _init():
    init_db()
    return True


# ⚠️ 每个 Repository() 都持有一个 SQLAlchemy Session（即一条池化连接）。
#    必须用 `with` 关闭，否则每次缓存过期都会漏一条连接：连接池只有
#    5 + 10 溢出，看板刷新几轮就会 TimeoutError（连接池耗尽）并卡住。
@st.cache_data(ttl=30)
def _stats():
    with Repository() as repo:
        return repo.stats()


@st.cache_data(ttl=30)
def _distribution(version: str, platform: str | None = None, hours: int = 0):
    # 必须和趋势图用同一个平台/时间范围 —— 否则选平台=A 时，
    # 趋势图是 A 的、旁边的情绪分布和负面占比却是全平台的，两处数字打架。
    since = utcnow() - timedelta(hours=hours) if hours else None
    with Repository() as repo:
        return repo.sentiment_distribution(version, platform=platform, since=since)


@st.cache_data(ttl=30)
def _platform_dist(version: str):
    with Repository() as repo:
        return repo.platform_distribution(version)


@st.cache_data(ttl=30)
def _trend(version: str, hours: int, platform: str | None):
    with Repository() as repo:
        return repo.trend_by_hour(version, platform=platform, hours=hours)


@st.cache_data(ttl=30)
def _top_negative(version: str, limit: int = 30, platform: str | None = None):
    with Repository() as repo:
        rows = repo.top_negative(version, limit=limit, platform=platform)
    return [
        {
            "平台": c.platform,
            "评论": (c.text or "")[:120],
            "情感分": a.sentiment_score,
            "点赞": c.like_count,
            "发布时间": c.publish_time.strftime("%m-%d %H:%M") if c.publish_time else "",
            "IP属地": c.ip_location or "",
        }
        for c, a in rows
    ]


@st.cache_data(ttl=60)
def _versions() -> list[str]:
    from sqlalchemy import distinct, select

    from Whochat.store.models import AnalysisResult

    with Repository() as repo:
        return sorted(
            v for (v,) in repo.session.execute(select(distinct(AnalysisResult.analysis_version)))
        )


@st.cache_data(ttl=60)
def _alerts(limit: int = 50):
    from sqlalchemy import select

    from Whochat.store.models import Alert

    with Repository() as repo:
        rows = repo.session.scalars(
            select(Alert).order_by(Alert.trigger_time.desc()).limit(limit)
        )
        return [
            {
                "等级": a.level,
                "标题": a.title,
                "命中数": a.match_count,
                "触发时间": a.trigger_time.strftime("%m-%d %H:%M") if a.trigger_time else "",
                "推送状态": a.push_status or "-",
            }
            for a in rows
        ]


@st.cache_data(ttl=60)
def _snapshot_contents():
    with Repository() as repo:
        return repo.contents_with_snapshots()


@st.cache_data(ttl=60)
def _snapshot_series(content_id: str):
    with Repository() as repo:
        return repo.snapshot_series(content_id)


@st.cache_data(ttl=60)
def _topics():
    from sqlalchemy import select

    from Whochat.store.models import Topic

    with Repository() as repo:
        rows = repo.session.scalars(select(Topic).order_by(Topic.doc_count.desc()).limit(30))
        return [
            {"主题": t.label, "关键词": " / ".join(t.keywords or []), "文档数": t.doc_count}
            for t in rows
        ]


# ================================================================ 侧边栏


def sidebar() -> tuple[str, int, str | None]:
    st.sidebar.title("📡 舆情分析")

    versions = _versions()
    if not versions:
        st.sidebar.warning("还没有分析结果")
        st.sidebar.code("python -m Whochat.cli demo", language="bash")
        return "", 72, None

    version = st.sidebar.selectbox("分析版本", versions, index=len(versions) - 1)
    hours = st.sidebar.select_slider("时间范围（小时）", [24, 72, 168, 720], value=72)

    platforms = list(_platform_dist(version).keys())
    platform = st.sidebar.selectbox("平台", ["全部"] + sorted(platforms))
    platform = None if platform == "全部" else platform

    st.sidebar.divider()
    st.sidebar.caption(
        "⚠️ 数据来自评论区，只代表**愿意评论的人**，天然偏向极端情绪。\n\n"
        "结论请表述为「讨论区情绪分布」，不要外推为「公众意见」。"
    )

    st.sidebar.divider()
    if st.sidebar.button("🔄 刷新数据"):
        st.cache_data.clear()
        st.rerun()

    return version, hours, platform


# ================================================================ 页面


def page_overview(version: str, hours: int, platform: str | None) -> None:
    stats = _stats()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("内容", f"{stats['contents']:,}")
    c2.metric("评论", f"{stats['comments']:,}")
    c3.metric("已分析", f"{stats['analyses']:,}")
    c4.metric("预警", f"{stats['alerts']:,}")

    st.divider()

    dist = _distribution(version, platform, hours)
    total = sum(dist.values())

    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader(f"声量与情感趋势（近 {hours} 小时）")
        points = ts.to_trend_points(_trend(version, hours, platform))
        if points:
            import pandas as pd

            df = pd.DataFrame(
                [
                    {
                        "时间": p.time,
                        "总数": p.total,
                        "正面": p.positive,
                        "中性": p.neutral,
                        "负面": p.negative,
                    }
                    for p in points
                ]
            ).set_index("时间")
            st.line_chart(df, color=["#4285f4", "#2e9e5b", "#9aa0a6", "#d93f3f"])

            # 爆发点检测 —— 快通道的核心信号
            bursts = ts.detect_bursts(points)
            if bursts:
                stage = ts.classify_stage(points)
                drift = ts.sentiment_drift(points)
                b1, b2, b3 = st.columns(3)
                b1.metric("事件阶段", stage)
                b2.metric("检测到突变点", f"{len(bursts)} 个")
                b3.metric(
                    "情感漂移",
                    f"{drift:+.3f}",
                    delta="恶化" if drift < -0.05 else ("好转" if drift > 0.05 else "平稳"),
                    delta_color="inverse" if drift < -0.05 else "normal",
                )
        else:
            st.info("该时间范围内没有数据")

    with col2:
        st.subheader("讨论区情绪分布")
        st.caption(f"近 {hours} 小时" + (f" · {platform}" if platform else " · 全部平台"))
        if total:
            import pandas as pd

            df = pd.DataFrame(
                {
                    "情感": [_SENTIMENT_CN.get(k, k) for k in dist],
                    "数量": list(dist.values()),
                }
            ).set_index("情感")
            st.bar_chart(df, color="#4285f4")
            neg_ratio = dist.get("negative", 0) / total
            st.metric("负面占比", f"{neg_ratio:.1%}")
        else:
            st.info("暂无数据")

    st.divider()
    st.subheader("平台分布")
    pdist = _platform_dist(version)
    if pdist:
        import pandas as pd

        st.bar_chart(
            pd.DataFrame({"平台": list(pdist), "数量": list(pdist.values())}).set_index("平台"),
            color="#7c4dff",
        )


def page_negative(version: str, platform: str | None = None) -> None:
    st.subheader("负面内容 TOP")
    st.caption(
        "情感分越低越负面。**这里是最该优先人工复核的清单。**"
        + (f"（平台：{platform}）" if platform else "")
    )

    rows = _top_negative(version, platform=platform)
    if not rows:
        st.info("暂无负面数据")
        return

    import pandas as pd

    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def page_topics() -> None:
    st.subheader("主题分布")
    st.caption("BERTopic 建模产出。话题聚类，不是人工分类。")

    rows = _topics()
    if not rows:
        st.info("还没有主题数据。执行：`python -m Whochat.cli topics`")
        return

    import pandas as pd

    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def page_wordcloud(version: str) -> None:
    st.subheader("词云")

    freq_path = Path(EXPORT_DIR) / "word_frequencies.json"
    img_path = Path(EXPORT_DIR) / "wordcloud.png"

    col1, col2 = st.columns([2, 1])

    with col1:
        if img_path.exists():
            st.image(str(img_path), width="stretch")
        elif freq_path.exists():
            st.info("词云图未生成（可能缺中文字体或未装 wordcloud），下面是词频。")
        else:
            st.info("还没有词云。执行：`python -m Whochat.cli wordcloud`")

    with col2:
        if freq_path.exists():
            import json

            freqs = json.loads(freq_path.read_text(encoding="utf-8"))
            st.caption("TOP 30 词频")
            for i, (word, cnt) in enumerate(list(freqs.items())[:30], 1):
                st.text(f"{i:>2}. {word}  ({cnt})")


def page_alerts() -> None:
    st.subheader("预警记录")
    st.caption(
        "**快通道**产出：纯规则，不依赖模型，采集后立刻触发。\n\n"
        "推送状态为 `skipped` 通常是等级未达实时阈值（留在日报），"
        "`failed` 多半是企微限流。"
    )

    rows = _alerts()
    if not rows:
        st.info("暂无预警。执行：`python -m Whochat.cli alert --seed-rules`")
        return

    import pandas as pd

    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)

    counts = df["等级"].value_counts()
    cols = st.columns(len(counts)) if len(counts) else []
    for col, (level, cnt) in zip(cols, counts.items()):
        col.metric(level.upper(), cnt)


def _page_propagation_curve() -> None:
    """传播曲线 —— metric_snapshots 的展示层。"""
    st.markdown("#### 传播曲线")
    candidates = _snapshot_contents()
    if not candidates:
        st.info(
            "还没有多个时间点的指标快照。真实采集**每次运行都会记一个快照**，"
            "同一内容多次采集才有曲线；demo 会造 3 个时间点。"
        )
        return

    labels = {
        f"{c['title'][:36]} · {c['platform']} · {c['points']} 个快照": c["content_id"]
        for c in candidates
    }
    cid = labels[st.selectbox("选择内容", list(labels))]

    points = prop.build_curve(_snapshot_series(cid))
    metrics = prop.analyze_growth(points)

    if points:
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "时间": p.time,
                    "点赞": p.like_count,
                    "评论": p.comment_count,
                    "转发": p.share_count,
                }
                for p in points
            ]
        ).set_index("时间")
        st.line_chart(df, color=["#4285f4", "#2e9e5b", "#d93f3f"])

    if not metrics.available:
        st.warning(metrics.reason)
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "起爆点",
        f"{metrics.max_growth_time:%m-%d %H:%M}" if metrics.max_growth_time else "-",
        delta=f"{metrics.max_growth_per_hour:,.0f} 互动/小时",
    )
    c2.metric(
        "峰值互动",
        f"{metrics.peak_value:,}",
        delta=f"{metrics.peak_time:%m-%d %H:%M}" if metrics.peak_time else None,
    )
    c3.metric("平均增速", f"{metrics.avg_growth_per_hour:,.0f} 互动/小时")
    c4.metric(
        "增速拐点",
        f"{metrics.inflection_time:%m-%d %H:%M}" if metrics.inflection_time else "未出现",
    )
    st.caption(metrics.summary)


def _page_propagation_path(contents: dict) -> None:
    """传播路径（parent → child）与 KOL。"""
    st.markdown("#### 传播路径 / KOL")
    from Whochat.analysis.timeseries import propagation_metrics

    metrics = propagation_metrics([], contents)
    if not metrics.get("available"):
        st.warning(metrics.get("reason", "数据不足"))
        st.markdown(
            """
            **这不是 bug，是采集层的设计取舍。**

            传播路径和 KOL 识别依赖采集时抓到的：
            - `parent_content_id` —— 转发/引用上游
            - `author_follower_count` —— 作者粉丝数

            如果采集层漏抓了这两列，事后**无法补** ——
            社媒历史数据重爬成本极高甚至不可能（内容已删）。

            这就是"采集是全量的、不可逆的；分析是增量、可重跑的"这条原则的由来。
            """
        )
        return

    st.caption(
        f"父子关系覆盖率 {metrics['has_parent_ratio']:.1%} · "
        f"粉丝数覆盖率 {metrics['has_follower_ratio']:.1%}"
    )
    if metrics.get("missing"):
        st.info(
            "采集层缺失字段：" + "、".join(metrics["missing"]) + "。相关分析已跳过 —— "
            "**不会用全 0 的数据凑一个看起来合理的榜单出来。**"
        )

    graph = prop.build_graph(list(contents.values()))
    if graph["available"]:
        st.caption(
            f"传播边 {graph['edge_count']} 条 · 根节点 {graph['root_count']} 个"
            + (f" · 悬空父引用 {graph['dangling_parents']} 条（父内容不在本批数据里）" if graph["dangling_parents"] else "")
        )
        try:
            st.graphviz_chart(prop.to_dot(graph), width="stretch")
        except Exception as e:  # 前端渲染失败不能拖垮整页
            st.caption(f"关系图渲染失败（{type(e).__name__}），退化为边列表。")
            import pandas as pd

            st.dataframe(pd.DataFrame(graph["edges"]), width="stretch", hide_index=True)
    else:
        st.caption(graph["reason"])

    import pandas as pd

    if metrics.get("kol_available"):
        st.markdown("**影响力账号 TOP**")
        st.dataframe(pd.DataFrame(metrics["kols"]), width="stretch", hide_index=True)
    else:
        st.caption("未抓到作者粉丝数，KOL 榜跳过。")


def page_propagation(version: str) -> None:
    st.subheader("传播分析")
    st.caption(
        "传播曲线来自 `metric_snapshots`（同一内容多次采集的指标时序），"
        "传播路径来自 `parent_content_id`（转发/引用）。两者都是采集层字段，**事后补不回来**。"
    )

    from sqlalchemy import select

    from Whochat.store.models import RawContent

    with Repository() as repo:
        contents = {c.content_id: c for c in repo.session.scalars(select(RawContent).limit(2000))}

    _page_propagation_curve()
    st.divider()
    _page_propagation_path(contents)


# ================================================================ 主流程


def main() -> None:
    _init()
    version, hours, platform = sidebar()

    st.title("📡 舆情分析看板")

    if not version:
        st.warning("数据库里还没有分析结果。")
        st.markdown(
            """
            先跑一遍 demo 验证链路（不需要爬虫、不需要模型）：

            ```bash
            python -m Whochat.cli demo
            ```

            或者走真实数据：

            ```bash
            python -m Whochat.cli crawl --platform xhs --keyword "你的监控词"
            python -m Whochat.cli analyze
            ```
            """
        )
        return

    tabs = st.tabs(["总览", "负面内容", "主题", "词云", "传播", "预警"])

    with tabs[0]:
        page_overview(version, hours, platform)
    with tabs[1]:
        page_negative(version, platform)
    with tabs[2]:
        page_topics()
    with tabs[3]:
        page_wordcloud(version)
    with tabs[4]:
        page_propagation(version)
    with tabs[5]:
        page_alerts()


main()
