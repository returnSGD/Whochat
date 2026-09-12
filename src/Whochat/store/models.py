"""数据表定义。

设计要点（方案文档 §2.4 / §5.3）：

1. **采集层抓全字段** —— 采集不可逆，分析可重跑。社媒历史数据重爬成本极高
   甚至不可能（内容已删），所以 `raw_json` 必须全量存档兜底。
2. **`parent_content_id` + `author_follower_count` 是传播分析的命根子** ——
   没有这两列，传播路径和 KOL 识别永远做不了，且事后补不回来。
3. **`analysis_version` 支持多版本并存** —— 换模型 / 改 prompt / 调阈值后重跑，
   旧结果不删，可以对比效果。
4. 类型统一按 PostgreSQL 写（`JSONB` 在 sqlite 下自动降级为 `JSON`），
   迁移时只改 `WHOCHAT_DB_URL` 即可。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB 用于 PostgreSQL，其他方言降级为普通 JSON
JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Base(DeclarativeBase):
    pass


# ============================================================ 采集层


class RawContent(Base):
    """内容主体（视频 / 笔记 / 帖子 / 文章 / 回答）。"""

    __tablename__ = "raw_content"

    content_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)

    content_type: Mapped[str | None] = mapped_column(String(32))  # video/note/post/article/answer
    title: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)

    # 原始时间戳，保留来源时区
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    crawl_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # 作者信息 —— KOL 识别依赖 follower_count
    author_id: Mapped[str | None] = mapped_column(String(128), index=True)
    author_name: Mapped[str | None] = mapped_column(String(256))
    author_follower_count: Mapped[int | None] = mapped_column(Integer)
    author_verified: Mapped[bool | None] = mapped_column(Boolean)

    # 抓取时刻的指标快照
    like_count: Mapped[int | None] = mapped_column(Integer)
    comment_count: Mapped[int | None] = mapped_column(Integer)
    share_count: Mapped[int | None] = mapped_column(Integer)
    collect_count: Mapped[int | None] = mapped_column(Integer)

    # 传播路径上游（转发/引用）—— 没有它就做不了传播分析
    parent_content_id: Mapped[str | None] = mapped_column(String(128), index=True)

    # 命中的监控词，用于归因
    search_keyword: Mapped[str | None] = mapped_column(String(256), index=True)

    # 原始 JSON 全量存档 —— 兜底，字段漏了就靠它
    raw_json: Mapped[dict | None] = mapped_column(JSONVariant)

    __table_args__ = (
        Index("ix_content_platform_publish", "platform", "publish_time"),
        Index("ix_content_keyword_publish", "search_keyword", "publish_time"),
    )


class Comment(Base):
    """评论。本项目的分析主力数据源 —— 评论区是情绪最集中处。"""

    __tablename__ = "comments"

    comment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_id: Mapped[str] = mapped_column(String(128), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)

    # 对话结构：一级评论归属 + 回复目标
    parent_comment_id: Mapped[str | None] = mapped_column(String(128), index=True)
    reply_to_comment_id: Mapped[str | None] = mapped_column(String(128), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)

    text: Mapped[str | None] = mapped_column(Text)

    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    crawl_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author_id: Mapped[str | None] = mapped_column(String(128), index=True)
    author_follower_count: Mapped[int | None] = mapped_column(Integer)

    like_count: Mapped[int | None] = mapped_column(Integer)
    reply_count: Mapped[int | None] = mapped_column(Integer)

    # 平台自带的 IP 属地
    ip_location: Mapped[str | None] = mapped_column(String(64), index=True)

    raw_json: Mapped[dict | None] = mapped_column(JSONVariant)

    __table_args__ = (
        Index("ix_comment_content_time", "content_id", "publish_time"),
        Index("ix_comment_platform_time", "platform", "publish_time"),
    )


class MetricSnapshot(Base):
    """指标时序快照 —— 画传播曲线的唯一数据来源。

    同一内容多次采集 = 多个快照点。不采这张表，传播分析永远做不了
    （能看到"最终有多少赞"，看不到"什么时候涨起来的"）。
    """

    __tablename__ = "metric_snapshots"

    content_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    like_count: Mapped[int | None] = mapped_column(Integer)
    comment_count: Mapped[int | None] = mapped_column(Integer)
    share_count: Mapped[int | None] = mapped_column(Integer)


# ============================================================ 分析层


class AnalysisResult(Base):
    """分析结果。`analysis_version` 让结果可重跑、可对比。"""

    __tablename__ = "analysis_results"

    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_type: Mapped[str] = mapped_column(String(16), primary_key=True)  # content | comment
    analysis_version: Mapped[str] = mapped_column(String(32), primary_key=True)

    cleaned_text: Mapped[str | None] = mapped_column(Text)

    sentiment_label: Mapped[str | None] = mapped_column(String(16), index=True)  # positive/neutral/negative
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    emotion_type: Mapped[str | None] = mapped_column(String(32))

    topic_id: Mapped[int | None] = mapped_column(Integer, index=True)
    topic_prob: Mapped[float | None] = mapped_column(Float)

    keywords: Mapped[list | None] = mapped_column(JSONVariant)

    # LLM 清洗产出
    is_ad: Mapped[bool | None] = mapped_column(Boolean)
    is_valid: Mapped[bool | None] = mapped_column(Boolean)
    subject: Mapped[str | None] = mapped_column(String(256))

    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_analysis_version_sentiment", "analysis_version", "sentiment_label"),)


class Topic(Base):
    """主题建模产出（BERTopic）。"""

    __tablename__ = "topics"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    topic_id: Mapped[int] = mapped_column(Integer, primary_key=True)

    label: Mapped[str | None] = mapped_column(String(256))
    keywords: Mapped[list | None] = mapped_column(JSONVariant)
    doc_count: Mapped[int | None] = mapped_column(Integer)
    rep_docs: Mapped[list | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================ 预警与任务


class AlertRule(Base):
    """预警规则。快通道用，纯规则无模型，保证秒级响应。"""

    __tablename__ = "alert_rules"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    level: Mapped[str] = mapped_column(String(16), default="yellow")  # red/orange/yellow/blue

    # {keywords: [...], sentiments: [...], threshold: 50, window_seconds: 300}
    conditions: Mapped[dict] = mapped_column(JSONVariant)

    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300)
    channels: Mapped[list] = mapped_column(JSONVariant, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    """预警记录。含推送状态，便于排查"为什么没收到通知"。"""

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    rule_id: Mapped[str | None] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)

    trigger_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    title: Mapped[str | None] = mapped_column(String(512))
    # 为什么触发（"负面占比 62% ≥ 阈值 60%"）——
    # 只说"命中 54 条"没法判断严重程度，人也无法据此调阈值
    reason: Mapped[str | None] = mapped_column(String(512))
    matched_items: Mapped[list | None] = mapped_column(JSONVariant)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    agg_window: Mapped[int | None] = mapped_column(Integer)

    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_status: Mapped[str | None] = mapped_column(String(32))  # pending/sent/skipped/failed
    push_channel: Mapped[str | None] = mapped_column(String(32))
    push_error: Mapped[str | None] = mapped_column(Text)


class CrawlTask(Base):
    """采集任务。`last_cursor` 支撑断点续爬 —— 反爬导致中断是常态，必须有。"""

    __tablename__ = "crawl_tasks"

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))  # keyword | content_id | creator
    target: Mapped[str] = mapped_column(String(256))

    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/done/failed
    last_cursor: Mapped[str | None] = mapped_column(Text)
    items_collected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("platform", "mode", "target", name="uq_task_target"),)
