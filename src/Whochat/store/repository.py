"""统一读写接口 —— 事实上的"数据中台"边界（方案文档 §5.1）。

**预警逻辑、分析脚本、看板一律通过这里读写，不直接碰数据库文件、不读原始 JSON。**
这样将来换 PostgreSQL、换 UI、加缓存时，上层代码一行不用改。

MVP 用 SQLite，DDL 按 PostgreSQL 写，迁移只需改 `WHOCHAT_DB_URL`。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from Whochat.config import settings
from Whochat.store.models import (
    Alert,
    AlertRule,
    AnalysisResult,
    Base,
    Comment,
    CrawlTask,
    MetricSnapshot,
    RawContent,
    Topic,
    utcnow,
)

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        kwargs: dict[str, Any] = {"echo": settings.store.echo, "future": True}
        if settings.store.url.startswith("sqlite"):
            # SQLite 多线程访问（Streamlit / APScheduler 并发）
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(settings.store.url, **kwargs)
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory()


def init_db() -> None:
    """建表。幂等，可重复调用。"""
    Base.metadata.create_all(get_engine())


# ============================================================ 时区工具


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite 不保存时区，读回来是 naive。统一按 UTC 补上，避免比较时报错。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_time(value: Any) -> datetime | None:
    """把各平台五花八门的时间格式统一成 aware datetime。

    支持：datetime 对象 / 秒级或毫秒级时间戳 / ISO 字符串 / "YYYY-MM-DD HH:MM:SS"。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_time(int(s))
        s = s.replace("/", "-").replace("Z", "+00:00")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        try:
            return _ensure_aware(datetime.fromisoformat(s))
        except ValueError:
            return None
    return None


# ============================================================ 写入


def _comment_analysis(stmt, *, valid_only: bool = True):
    """给聚合查询加上"只统计评论、且只统计有效结果"的约束。

    两个约束都不能省：

    * `item_type == "comment"` —— AnalysisResult 的主键是
      (item_id, item_type, analysis_version)，content 和 comment 的 ID
      命名空间不同却可能撞号，不区分会把内容级结果混进评论统计。
    * `is_valid IS NOT FALSE` —— 被规则清洗判为广告/近重复的评论现在也会
      写一行 is_valid=False（否则它们下一轮又会被当成"待分析"重新处理，
      见 runner.clean_comments）。这些行只用于标记"已处理"，
      **不能**计入情感分布、趋势、高频词等统计，不然广告会污染结论。
      用 IS NOT FALSE 而不是 == True，是为了兼容历史上 NULL 的旧行。
    """
    stmt = stmt.where(AnalysisResult.item_type == "comment")
    if valid_only:
        stmt = stmt.where(AnalysisResult.is_valid.isnot(False))
    return stmt


class Repository:
    """所有读写都走这里。方法名保持业务语义，不暴露 SQL。"""

    def __init__(self, session: Session | None = None):
        self.session = session or get_session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------- 采集数据落库

    def upsert_contents(self, rows: Iterable[dict]) -> int:
        """写入内容。已存在则更新（指标会随时间增长，但 publish_time 等不变）。"""
        count = 0
        for row in rows:
            cid = row.get("content_id")
            if not cid:
                continue
            existing = self.session.get(RawContent, cid)
            payload = dict(row)
            payload["content_id"] = cid
            if payload.get("publish_time") is not None:
                payload["publish_time"] = parse_time(payload["publish_time"])
            if existing is None:
                self.session.add(RawContent(**payload))
            else:
                for k, v in payload.items():
                    # 不要用 None 覆盖已有值：后续采集某字段缺失（别名没命中、
                    # 平台没返回）时，None 会把库里原本有效的值清空。最典型的是
                    # publish_time 被清成 NULL —— 该内容从此掉出所有按时间窗的
                    # 查询（快通道漏警、趋势图失真），且再也回不来。
                    if k == "content_id" or v is None:
                        continue
                    # 归因（search_keyword）一经确立就不再改写：同一条内容可能被
                    # 多个监控词命中，后来者覆盖前者会让"这条舆情是被哪个词发现的"
                    # 永久错乱，历史归因统计随之失真。首次带值时仍会补上。
                    if k == "search_keyword" and existing.search_keyword and existing.search_keyword != v:
                        continue
                    setattr(existing, k, v)
            count += 1
        self.session.commit()
        return count

    def upsert_comments(self, rows: Iterable[dict]) -> int:
        count = 0
        for row in rows:
            cid = row.get("comment_id")
            if not cid:
                continue
            payload = dict(row)
            payload["comment_id"] = cid
            if payload.get("publish_time") is not None:
                payload["publish_time"] = parse_time(payload["publish_time"])
            existing = self.session.get(Comment, cid)
            if existing is None:
                self.session.add(Comment(**payload))
            else:
                for k, v in payload.items():
                    # 同 upsert_contents：None 不覆盖已有值，否则一次缺字段的
                    # 重复采集会把 publish_time 清成 NULL，评论静默掉出时间窗。
                    if k != "comment_id" and v is not None:
                        setattr(existing, k, v)
            count += 1
        self.session.commit()
        return count

    def add_snapshots(self, rows: Iterable[dict]) -> int:
        """记录指标快照。同一 (content_id, snapshot_time) 幂等。"""
        count = 0
        for row in rows:
            cid, ts = row.get("content_id"), parse_time(row.get("snapshot_time") or utcnow())
            if not cid or ts is None:
                continue
            if self.session.get(MetricSnapshot, (cid, ts)) is not None:
                continue
            self.session.add(
                MetricSnapshot(
                    content_id=cid,
                    snapshot_time=ts,
                    like_count=row.get("like_count"),
                    comment_count=row.get("comment_count"),
                    share_count=row.get("share_count"),
                )
            )
            count += 1
        self.session.commit()
        return count

    # -------------------------------------------------- 分析结果

    def save_analysis(self, rows: Iterable[dict]) -> int:
        """写入分析结果。按 (item_id, item_type, analysis_version) 幂等覆盖。"""
        count = 0
        for row in rows:
            key = (row["item_id"], row["item_type"], row["analysis_version"])
            existing = self.session.get(AnalysisResult, key)
            if existing is None:
                self.session.add(AnalysisResult(**row))
            else:
                for k, v in row.items():
                    if k not in ("item_id", "item_type", "analysis_version"):
                        setattr(existing, k, v)
            count += 1
        self.session.commit()
        return count

    def latest_analysis_version(self) -> str | None:
        """最近一次写入的分析版本号。

        日报/周报这类"没带版本号"的调用方必须用它来解析版本 —— 版本号是
        `{后端}-v1`（默认 `lexicon-v1`），写死 "v1" 会查不到任何结果，
        日报里情感分布永远是空的。
        """
        return self.session.scalar(
            select(AnalysisResult.analysis_version)
            .order_by(AnalysisResult.processed_at.desc())
            .limit(1)
        )

    def save_topics(self, run_id: str, topics: Sequence[dict]) -> int:
        self.session.execute(delete(Topic).where(Topic.run_id == run_id))
        for t in topics:
            self.session.add(
                Topic(
                    run_id=run_id,
                    topic_id=int(t["topic_id"]),
                    label=t.get("label"),
                    keywords=t.get("keywords"),
                    doc_count=t.get("doc_count"),
                    rep_docs=t.get("rep_docs"),
                )
            )
        self.session.commit()
        return len(topics)

    # -------------------------------------------------- 读取

    def comments_for_analysis(self, version: str, limit: int = 100000) -> list[Comment]:
        """取还没分析过的评论（该版本下）。"""
        done = select(AnalysisResult.item_id).where(
            AnalysisResult.item_type == "comment",
            AnalysisResult.analysis_version == version,
        )
        stmt = (
            select(Comment)
            .where(Comment.text.isnot(None), Comment.text != "")
            .where(Comment.comment_id.notin_(done))
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def comments_in_window(
        self,
        since: datetime,
        until: datetime | None = None,
        platform: str | None = None,
    ) -> list[Comment]:
        """取某个时间窗内的评论 —— 快通道预警的输入。

        按 `publish_time` 过滤（内容实际发布时刻），不是 `crawl_time`。
        预警关心的是"什么时候发生的"，不是"什么时候抓到的"。
        """
        stmt = select(Comment).where(
            Comment.text.isnot(None),
            Comment.text != "",
            Comment.publish_time.isnot(None),
            Comment.publish_time >= since,
        )
        if until:
            stmt = stmt.where(Comment.publish_time < until)
        if platform:
            stmt = stmt.where(Comment.platform == platform)
        return list(self.session.scalars(stmt))

    def all_comments(self, platform: str | None = None, keyword: str | None = None) -> list[Comment]:
        stmt = select(Comment).where(Comment.text.isnot(None), Comment.text != "")
        if platform:
            stmt = stmt.where(Comment.platform == platform)
        if keyword:
            sub = select(RawContent.content_id).where(RawContent.search_keyword == keyword)
            stmt = stmt.where(Comment.content_id.in_(sub))
        return list(self.session.scalars(stmt))

    def content_map(self, content_ids: Sequence[str]) -> dict[str, RawContent]:
        if not content_ids:
            return {}
        rows = self.session.scalars(
            select(RawContent).where(RawContent.content_id.in_(list(content_ids)))
        )
        return {r.content_id: r for r in rows}

    # -------------------------------------------------- 看板聚合查询

    def sentiment_distribution(
        self, version: str, platform: str | None = None, since: datetime | None = None
    ) -> dict[str, int]:
        stmt = (
            select(AnalysisResult.sentiment_label, func.count())
            .where(AnalysisResult.analysis_version == version)
            .group_by(AnalysisResult.sentiment_label)
        )
        stmt = _comment_analysis(stmt)
        if platform:
            stmt = stmt.where(
                AnalysisResult.item_id.in_(
                    select(Comment.comment_id).where(Comment.platform == platform)
                )
            )
        if since:
            stmt = stmt.where(
                AnalysisResult.item_id.in_(
                    select(Comment.comment_id).where(Comment.publish_time >= since)
                )
            )
        return {label or "unknown": cnt for label, cnt in self.session.execute(stmt)}

    def trend_by_hour(
        self,
        version: str,
        platform: str | None = None,
        hours: int = 72,
        bucket_hours: int = 1,
    ) -> list[dict]:
        """按时间桶聚合声量与情感 —— 看板趋势图 + 拐点识别的基础。"""
        since = utcnow() - timedelta(hours=hours)
        stmt = (
            select(
                Comment.publish_time,
                AnalysisResult.sentiment_label,
            )
            .join(AnalysisResult, AnalysisResult.item_id == Comment.comment_id)
            .where(
                AnalysisResult.analysis_version == version,
                Comment.publish_time.isnot(None),
                Comment.publish_time >= since,
            )
        )
        stmt = _comment_analysis(stmt)
        if platform:
            stmt = stmt.where(Comment.platform == platform)

        buckets: dict[str, dict[str, Any]] = {}
        for pub_time, label in self.session.execute(stmt):
            pub_time = _ensure_aware(pub_time)
            if pub_time is None:
                continue
            # 向下取整到 bucket_hours
            floored = pub_time.replace(minute=0, second=0, microsecond=0)
            if bucket_hours > 1:
                floored = floored.replace(hour=(floored.hour // bucket_hours) * bucket_hours)
            key = floored.isoformat()
            b = buckets.setdefault(
                key,
                {"time": key, "total": 0, "positive": 0, "neutral": 0, "negative": 0},
            )
            b["total"] += 1
            if label in ("positive", "neutral", "negative"):
                b[label] += 1
        return sorted(buckets.values(), key=lambda x: x["time"])

    def top_negative(
        self,
        version: str,
        limit: int = 20,
        since: datetime | None = None,
        platform: str | None = None,
    ) -> list[tuple[Comment, AnalysisResult]]:
        stmt = (
            select(Comment, AnalysisResult)
            .join(AnalysisResult, AnalysisResult.item_id == Comment.comment_id)
            .where(
                AnalysisResult.analysis_version == version,
                AnalysisResult.sentiment_label == "negative",
            )
            .order_by(AnalysisResult.sentiment_score.asc())
            .limit(limit)
        )
        stmt = _comment_analysis(stmt)
        if since:
            stmt = stmt.where(Comment.publish_time >= since)
        if platform:
            stmt = stmt.where(Comment.platform == platform)
        return [(c, a) for c, a in self.session.execute(stmt)]

    def snapshot_series(self, content_id: str) -> list[dict]:
        """某条内容的指标时序 —— 传播曲线的原始数据。

        时间升序。只有 >=2 个点才谈得上"传播"，单点只是快照。
        """
        rows = self.session.scalars(
            select(MetricSnapshot)
            .where(MetricSnapshot.content_id == content_id)
            .order_by(MetricSnapshot.snapshot_time)
        )
        return [
            {
                "time": _ensure_aware(r.snapshot_time),
                "like_count": r.like_count or 0,
                "comment_count": r.comment_count or 0,
                "share_count": r.share_count or 0,
            }
            for r in rows
        ]

    def contents_with_snapshots(self, limit: int = 200, min_points: int = 2) -> list[dict]:
        """有多个快照点的内容 —— 看板里传播曲线的可选对象。

        按最新快照时间倒序（最近还在涨的排前面）。title/platform 取自
        raw_content，方便用户认出是哪条。
        """
        rows = self.session.execute(
            select(
                MetricSnapshot.content_id,
                func.count().label("points"),
                func.min(MetricSnapshot.snapshot_time),
                func.max(MetricSnapshot.snapshot_time),
            )
            .group_by(MetricSnapshot.content_id)
            .having(func.count() >= min_points)
            .order_by(func.max(MetricSnapshot.snapshot_time).desc())
            .limit(limit)
        ).all()
        if not rows:
            return []

        contents = self.content_map([r[0] for r in rows])
        out = []
        for cid, points, first_t, last_t in rows:
            c = contents.get(cid)
            out.append(
                {
                    "content_id": cid,
                    "title": (getattr(c, "title", None) or cid) if c else cid,
                    "platform": (getattr(c, "platform", "") or "") if c else "",
                    "points": points,
                    "first_time": _ensure_aware(first_t),
                    "last_time": _ensure_aware(last_t),
                }
            )
        return out

    def platform_distribution(self, version: str) -> dict[str, int]:
        stmt = (
            select(Comment.platform, func.count())
            .join(AnalysisResult, AnalysisResult.item_id == Comment.comment_id)
            .where(AnalysisResult.analysis_version == version)
            .group_by(Comment.platform)
        )
        stmt = _comment_analysis(stmt)
        return {p or "unknown": c for p, c in self.session.execute(stmt)}

    def analyzed_texts(self, version: str, platform: str | None = None) -> list[str]:
        """取清洗后的文本，给词云和主题建模用。"""
        stmt = select(AnalysisResult.cleaned_text).where(
            AnalysisResult.analysis_version == version,
            AnalysisResult.cleaned_text.isnot(None),
        )
        stmt = _comment_analysis(stmt)
        if platform:
            stmt = stmt.where(
                AnalysisResult.item_id.in_(
                    select(Comment.comment_id).where(Comment.platform == platform)
                )
            )
        return [t for (t,) in self.session.execute(stmt) if t]

    def keyword_frequencies(self, version: str, top_n: int = 150) -> dict[str, int]:
        """直接按 analysis_results.keywords 统计词频，比重新分词快。"""
        counter: Counter = Counter()
        stmt = _comment_analysis(
            select(AnalysisResult.keywords).where(AnalysisResult.analysis_version == version)
        )
        for (kws,) in self.session.execute(stmt):
            if isinstance(kws, list):
                counter.update(kws)
        return dict(counter.most_common(top_n))

    # -------------------------------------------------- 统计

    def stats(self) -> dict[str, int]:
        # analyses 只统计**有效**结果。被规则清洗淘汰的评论现在也会占一行
        # （is_valid=False，见 runner.clean_comments），它们只是"已处理"的标记；
        # 若一并计入，"已分析"会等于评论总数，看板上的数字就骗人了。
        valid = _comment_analysis(select(func.count()).select_from(AnalysisResult))
        rejected = (
            select(func.count())
            .select_from(AnalysisResult)
            .where(
                AnalysisResult.item_type == "comment",
                AnalysisResult.is_valid.is_(False),
            )
        )
        return {
            "contents": self.session.scalar(select(func.count()).select_from(RawContent)) or 0,
            "comments": self.session.scalar(select(func.count()).select_from(Comment)) or 0,
            "snapshots": self.session.scalar(select(func.count()).select_from(MetricSnapshot)) or 0,
            "analyses": self.session.scalar(valid) or 0,
            "rejected": self.session.scalar(rejected) or 0,
            "alerts": self.session.scalar(select(func.count()).select_from(Alert)) or 0,
        }

    # -------------------------------------------------- 预警规则

    def upsert_rule(self, rule_id: str, name: str, conditions: dict, **kwargs) -> AlertRule:
        rule = self.session.get(AlertRule, rule_id)
        if rule is None:
            rule = AlertRule(rule_id=rule_id, name=name, conditions=conditions, **kwargs)
            self.session.add(rule)
        else:
            rule.name = name
            rule.conditions = conditions
            for k, v in kwargs.items():
                setattr(rule, k, v)
        self.session.commit()
        return rule

    def enabled_rules(self) -> list[AlertRule]:
        return list(self.session.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))

    def all_rules(self) -> list[AlertRule]:
        """含禁用规则 —— 操作端的管理列表要用。"""
        return list(self.session.scalars(select(AlertRule).order_by(AlertRule.rule_id)))

    def delete_rule(self, rule_id: str) -> bool:
        rule = self.session.get(AlertRule, rule_id)
        if rule is None:
            return False
        self.session.delete(rule)
        self.session.commit()
        return True

    # -------------------------------------------------- 预警记录

    def recent_alert_for_rule(self, rule_id: str, within_seconds: int) -> Alert | None:
        """冷却检查用。"""
        since = utcnow() - timedelta(seconds=within_seconds)
        return self.session.scalars(
            select(Alert)
            .where(Alert.rule_id == rule_id, Alert.trigger_time >= since)
            .order_by(Alert.trigger_time.desc())
            .limit(1)
        ).first()

    def add_alert(self, **kwargs) -> Alert:
        alert = Alert(**kwargs)
        self.session.add(alert)
        self.session.commit()
        return alert

    def mark_alert_pushed(self, alert_id: str, status: str, channel: str | None = None, error: str | None = None) -> None:
        alert = self.session.get(Alert, alert_id)
        if alert:
            alert.push_status = status
            alert.push_channel = channel
            alert.push_error = error
            if status == "sent":
                alert.pushed_at = utcnow()
            self.session.commit()

    def pending_alerts(self) -> list[Alert]:
        return list(
            self.session.scalars(
                select(Alert).where(Alert.push_status == "pending").order_by(Alert.trigger_time)
            )
        )

    def expire_stale_alerts(self, max_age_seconds: int) -> int:
        """把重试太久仍未成功的告警标记为 failed，避免永久重试。

        只清理**失败过**（push_error 非空）的 pending 告警：从未尝试推送的
        告警不受影响，age 再大也留着。这是"发送失败保持 pending 自动重试"
        的配套兜底 —— 没有它，一个配错的 webhook 会让告警无限重试。
        """
        cutoff = utcnow() - timedelta(seconds=max_age_seconds)
        rows = list(
            self.session.scalars(
                select(Alert).where(
                    Alert.push_status == "pending",
                    Alert.trigger_time < cutoff,
                    Alert.push_error.is_not(None),
                )
            )
        )
        for a in rows:
            a.push_status = "failed"
        if rows:
            self.session.commit()
        return len(rows)

    # -------------------------------------------------- 采集任务

    def upsert_task(self, platform: str, mode: str, target: str, **kwargs) -> CrawlTask:
        task = self.session.scalars(
            select(CrawlTask).where(
                CrawlTask.platform == platform, CrawlTask.mode == mode, CrawlTask.target == target
            )
        ).first()
        if task is None:
            task = CrawlTask(platform=platform, mode=mode, target=target, **kwargs)
            self.session.add(task)
        else:
            for k, v in kwargs.items():
                setattr(task, k, v)
            task.updated_at = utcnow()
        self.session.commit()
        return task
