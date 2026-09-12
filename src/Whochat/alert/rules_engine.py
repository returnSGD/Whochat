"""快通道预警引擎 —— 纯规则，无模型，秒级响应。

**为什么必须有快通道**（方案文档 §6.1）：

    你的链路里最慢的是本地模型清洗。1 万条评论逐条跑 LLM 要几十分钟到几小时，
    而舆情预警的价值全在时效（行业标准 30 秒~分钟级）。
    等清洗完再告警，事情已经过去了。

快通道在采集落库后**立刻**跑，不依赖 LLM、不依赖 BERTopic，
只用关键词表 + 情绪词密度 + 增速突变检测。三条规则：

    keyword      敏感词命中
    velocity     单位时间内命中量超过阈值（增速突变）
    negativity   负面情绪占比超过阈值
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from Whochat.analysis.sentiment import LexiconSentiment
from Whochat.config import settings
from Whochat.pipeline.rules import clean, negative_words, sensitive_words
from Whochat.store.models import utcnow
from Whochat.store.repository import Repository


@dataclass
class RuleHit:
    rule_id: str
    rule_name: str
    level: str
    matched: list[dict] = field(default_factory=list)
    reason: str = ""
    count: int = 0


class RuleEngine:
    """快通道规则引擎。"""

    def __init__(self, repo: Repository | None = None, sentiment=None):
        self.repo = repo or Repository()
        # 词典后端是毫秒级的，可以放在快通道里；模型后端不行
        self.sentiment = sentiment or LexiconSentiment()

    # ------------------------------------------------------------

    def evaluate(
        self,
        now=None,
        *,
        since=None,
        until=None,
        skip_cooldown: bool = False,
    ) -> list[RuleHit]:
        """跑一遍所有启用的规则，返回命中的（已过冷却检查）。

        时间窗语义：

            默认（实时）    只看 [now - window_seconds, now]
                            —— 预警关心"正在发生什么"，不是"过去发生过什么"
            since/until     显式指定窗口，用于**回放和补数**

        **为什么需要回放**：如果爬虫断了几个小时，恢复后补抓的数据
        publish_time 在过去，默认窗口根本扫不到，那批舆情就静默漏掉了。
        补数时必须能指定时间窗重跑。

        Args:
            skip_cooldown: 回放历史时跳过冷却检查，否则第一次命中后
                           整个窗口的其余告警都会被冷却吞掉。
        """
        now = now or utcnow()
        hits: list[RuleHit] = []

        for rule in self.repo.enabled_rules():
            # 冷却检查：同一规则在冷却期内不重复告警
            # 没有冷却会导致舆情爆发时同一条规则每分钟刷屏，触发企微限流
            if not skip_cooldown:
                recent = self.repo.recent_alert_for_rule(rule.rule_id, rule.cooldown_seconds)
                if recent is not None:
                    continue

            hit = self._evaluate_rule(rule, now, since=since, until=until)
            if hit is not None:
                hits.append(hit)

        return hits

    def _evaluate_rule(self, rule, now, *, since=None, until=None) -> RuleHit | None:
        cond = rule.conditions or {}
        window = int(cond.get("window_seconds", 300))
        # 不设默认值：没配 threshold 的规则**不应该**走数量触发分支。
        # 否则一条只定义了 negative_ratio 的规则会因为默认阈值而过早触发，
        # 且命中的是窗口内全部评论（含正面/中性），严重误导严重程度判断。
        threshold = cond.get("threshold")
        threshold = int(threshold) if threshold is not None else None

        explicit_window = since is not None
        if since is None:
            since = now - timedelta(seconds=window)
        # until 必须**有上界**，实时模式也不例外。
        # 之前实时模式 until=None，查询只有下界 publish_time >= since，
        # 未来时间戳会被算进来。而 parse_time 对无时区字符串按 UTC 解析，
        # 平台时间多是本地时间（+8h）—— 于是每条评论都会"提前 8 小时"进入
        # 窗口，冷却期一到就反复误报同一批数据，直到 since 越过它。
        until = until or now
        comments = self.repo.comments_in_window(since, until=until)
        if not comments:
            return None

        keywords = list(cond.get("keywords") or [])
        # use_sensitive_words：把 dicts/sensitive_words.txt 当作关键词表。
        # 在**评估时**读取而不是写进规则里，这样改词表立刻生效，
        # 不用重新 seed 规则。（这个函数以前是死代码，没人调用。）
        if cond.get("use_sensitive_words"):
            keywords.extend(sensitive_words())
        sentiments = cond.get("sentiments") or []
        negative_ratio_threshold = cond.get("negative_ratio")

        # 需要算情感的场景：配了 sentiments 过滤，或者要算负面占比
        need_sentiment = bool(sentiments) or negative_ratio_threshold is not None
        neg_words = negative_words()

        matched: list[dict] = []
        negative_items: list[dict] = []

        for c in comments:
            text = c.text or ""
            cleaned = clean(text)
            if not cleaned:
                continue

            # 关键词过滤：配了关键词就必须命中
            if keywords and not any(k in cleaned for k in keywords):
                continue

            row = {
                "comment_id": c.comment_id,
                "platform": c.platform,
                "text": cleaned[:200],
                "publish_time": c.publish_time.isoformat() if c.publish_time else None,
                "ip_location": c.ip_location,
                "like_count": c.like_count,
            }

            # 情感：快通道用词典法，毫秒级，不依赖模型
            if need_sentiment:
                result = self.sentiment.analyze(cleaned)
                row["sentiment"] = result.label
                row["sentiment_score"] = result.score
                if result.label == "negative":
                    row["negative_word_hits"] = sum(1 for w in neg_words if w in cleaned) if neg_words else 0
                    negative_items.append(row)

            # sentiments 过滤：只保留指定情感
            if sentiments and row.get("sentiment") not in sentiments:
                continue

            matched.append(row)

        if not matched and not negative_items:
            return None

        # ---------- 触发判定 ----------

        reason = None
        final_matched = matched

        # 窗口描述：回放模式下 window_seconds 不是真实跨度，别拿它糊弄人
        if explicit_window:
            span = (until - since).total_seconds()
            span_desc = f"{span / 3600:.1f} 小时窗口"
        else:
            span_desc = f"{window} 秒"

        if negative_ratio_threshold is not None:
            # 分母是窗口内全部评论，不是 matched —— 否则比例恒为 1
            ratio = len(negative_items) / len(comments) if comments else 0
            if ratio >= float(negative_ratio_threshold):
                reason = f"负面占比 {ratio:.1%} ≥ 阈值 {float(negative_ratio_threshold):.1%}"
                # 占比规则触发时，命中列表应当是**负面评论**。
                # 否则会把中性评论也列进告警，让人误判严重程度。
                final_matched = negative_items

        if reason is None and threshold is not None and len(matched) >= threshold:
            reason = f"{span_desc}内命中 {len(matched)} 条 ≥ 阈值 {threshold} 条"

        if reason is None:
            return None

        return RuleHit(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            level=rule.level,
            matched=final_matched[:50],  # 只存前 50 条，避免单条告警体积过大
            reason=reason,
            count=len(final_matched),
        )

    # ------------------------------------------------------------

    def run_and_record(self, *, since=None, until=None, skip_cooldown: bool = False) -> list[str]:
        """跑一遍引擎并把命中的告警写入库（状态 pending，等推送）。"""
        hits = self.evaluate(since=since, until=until, skip_cooldown=skip_cooldown)
        alert_ids = []

        for hit in hits:
            alert = self.repo.add_alert(
                rule_id=hit.rule_id,
                level=hit.level,
                title=f"[{hit.level.upper()}] {hit.rule_name}",
                reason=hit.reason,
                matched_items=hit.matched,
                match_count=hit.count,
                agg_window=None,
                push_status="pending",
            )
            alert_ids.append(alert.alert_id)

        return alert_ids


# ---------------------------------------------------------------- 默认规则


def seed_default_rules(repo: Repository | None = None) -> int:
    """写入一组默认规则 —— 让用户第一次跑就有东西可看。

    参数是保守的：宁可漏报也不要天天狼来了。
    告警疲劳会让预警功能彻底失效（用户开始无视通知），这比漏报更糟。
    """
    repo = repo or Repository()

    defaults = [
        {
            "rule_id": "neg_surge",
            "name": "负面情绪激增",
            "level": "red",
            "conditions": {"sentiments": ["negative"], "threshold": 30, "window_seconds": 3600},
            "cooldown_seconds": 600,
            "channels": ["wecom"],
        },
        {
            "rule_id": "neg_ratio",
            "name": "负面占比异常",
            "level": "orange",
            "conditions": {"negative_ratio": 0.6, "window_seconds": 3600},
            "cooldown_seconds": 1800,
            "channels": ["wecom"],
        },
        {
            "rule_id": "volume_spike",
            "name": "声量突增",
            "level": "yellow",
            "conditions": {"threshold": 100, "window_seconds": 1800},
            "cooldown_seconds": 900,
            "channels": ["wecom"],
        },
        {
            # 风险词和情绪词是两条独立的线：一条冷静的"已向12315投诉并准备起诉"
            # 情感分很低，但对业务的风险等级是最高的。
            # 阈值压得比情绪规则低（3 条）—— 监管/法律信号出现一两条就该有人看。
            "rule_id": "sensitive_hit",
            "name": "敏感词命中",
            "level": "orange",
            "conditions": {
                "use_sensitive_words": True,
                "threshold": 3,
                "window_seconds": 3600,
            },
            "cooldown_seconds": 1800,
            "channels": ["wecom"],
        },
    ]

    for d in defaults:
        repo.upsert_rule(
            d["rule_id"],
            d["name"],
            d["conditions"],
            level=d["level"],
            cooldown_seconds=d["cooldown_seconds"],
            channels=d["channels"],
            enabled=True,
        )
    return len(defaults)
