"""企业微信推送。

**企微机器人的硬限制（方案文档 §6.2，全部来自官方文档/社区实测）**：

    ┌─────────────────┬──────────────────────────────────────────────┐
    │ 频率            │ 20 条/分钟/机器人（按 webhook 计，不是按群）    │
    │ 实测可用        │ 第 6~8 条就可能丢，建议压在 15 条/分钟以内      │
    │ 单条 body       │ ≤ 2048 字节，UTF-8                            │
    │ @人             │ Markdown 消息**不支持 @**；@人额外消耗配额       │
    │ 超频错误        │ `api freq out of limit`                       │
    └─────────────────┴──────────────────────────────────────────────┘

**反直觉但重要的推论**：舆情爆发时负面评论是**成批来的**。如果不做聚合，
按"每条都推"设计，会在爆发最需要被告知的那一刻恰好被限流打爆 ——
这是这类系统最典型的翻车方式。

所以推送必须做四件事：窗口聚合、分级路由、冷却去重、超量降级。
前三件在这里，第四件（换自建应用 API）留接口。
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from wochat.config import settings
from wochat.store.models import utcnow
from wochat.store.repository import Repository

# 企微 markdown 支持的颜色（有限，只有三种）
_LEVEL_COLOR = {
    "red": "warning",     # 橙红
    "orange": "warning",
    "yellow": "comment",  # 灰色
    "blue": "comment",
    "info": "comment",
}

_LEVEL_EMOJI = {
    "red": "🔴",
    "orange": "🟠",
    "yellow": "🟡",
    "blue": "🔵",
}


@dataclass
class PushResult:
    ok: bool
    status: str  # sent | skipped | failed | dry_run
    message: str = ""
    alert_ids: list[str] | None = None


class RateLimiter:
    """滑动窗口限流器。

    企微限制是 20 条/分钟，我们压在 15 条留安全余量 ——
    因为官方社区里有用户报告"1 分钟只发了 1~2 条"却仍收到 `api freq out of limit`，
    实际是其它模块共享了同一个机器人的配额。留余量是廉价的保险。
    """

    def __init__(self, max_per_minute: int | None = None):
        self.max_per_minute = max_per_minute or settings.alert.max_per_minute
        self._sent: deque[float] = deque()

    def can_send(self) -> bool:
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        return len(self._sent) < self.max_per_minute

    def record(self) -> None:
        self._sent.append(time.monotonic())

    def wait_seconds(self) -> float:
        """还要等多久才能发下一条。"""
        if self.can_send() or not self._sent:
            return 0.0
        return max(0.0, 60 - (time.monotonic() - self._sent[0]))

    @property
    def used(self) -> int:
        return len(self._sent)


class WeComNotifier:
    """企业微信机器人推送器。"""

    def __init__(self, webhook: str | None = None, repo: Repository | None = None):
        self.webhook = (webhook or settings.alert.wecom_webhook).strip()
        self.repo = repo or Repository()
        self.limiter = RateLimiter()
        self.max_bytes = settings.alert.max_body_bytes

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    # ------------------------------------------------------------ 组装

    @staticmethod
    def _truncate_bytes(text: str, limit: int) -> str:
        """按字节截断，不切断多字节字符。

        ⚠️ 预留长度必须**按后缀的真实字节数**算。企微单条硬上限 2048 字节
        （UTF-8），一个汉字占 3 字节；后缀"\n…（消息过长已截断）"是 31 字节，
        之前写死预留 20，截断后反而变成 2059 字节 —— 超限被企微拒收，
        再叠加下面的推送失败处理就会丢警。
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text

        suffix = "\n…（消息过长已截断）"
        budget = limit - len(suffix.encode("utf-8"))
        if budget <= 0:
            return "（内容过长）"

        # 逐步回退，保证不会把一个汉字切成半个
        cut = encoded[:budget]
        while cut:
            try:
                return cut.decode("utf-8") + suffix
            except UnicodeDecodeError:
                cut = cut[:-1]
        return "（内容过长）"

    def build_markdown(self, alerts: list) -> str:
        """把一批告警组装成一条 markdown。

        聚合而不是逐条推送 —— 这是避免被限流打爆的关键。
        """
        if not alerts:
            return ""

        # 取最高等级作为这条消息的等级
        order = {"red": 0, "orange": 1, "yellow": 2, "blue": 3}
        top = min(alerts, key=lambda a: order.get(a.level, 9))
        emoji = _LEVEL_EMOJI.get(top.level, "⚪")
        color = _LEVEL_COLOR.get(top.level, "comment")

        total = sum(a.match_count or 0 for a in alerts)

        lines = [
            f"### {emoji} <font color=\"{color}\">舆情预警 · {len(alerts)} 条规则命中</font>",
            f"> 触发时间：{utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            f"> 命中总量：**{total}** 条",
            "",
        ]

        for alert in alerts[:7]:  # 最多列 7 条，再多就超 2048 字节了
            e = _LEVEL_EMOJI.get(alert.level, "⚪")
            lines.append(f"**{e} {alert.title or alert.rule_id}**")
            # 先说触发原因，再说命中数 —— 原因才决定要不要立刻处理
            if alert.reason:
                lines.append(f"> 触发原因：{alert.reason}")
            lines.append(f"> 命中 {alert.match_count} 条")

            for item in (alert.matched_items or [])[:3]:
                text = (item.get("text") or "")[:60]
                platform = item.get("platform", "?")
                lines.append(f"> · [{platform}] {text}")
            lines.append("")

        if len(alerts) > 7:
            lines.append(f"…另有 {len(alerts) - 7} 条规则命中，详见看板")
            lines.append("")

        lines.append("[打开看板](http://localhost:6666)")

        return self._truncate_bytes("\n".join(lines), self.max_bytes)

    # ------------------------------------------------------------ 发送

    def send_markdown(self, content: str) -> tuple[bool, str]:
        """发一条 markdown 到企微。返回 (成功, 说明)。"""
        import requests

        if not self.enabled:
            return False, "未配置 WOCHAT_WECOM_WEBHOOK"

        if not self.limiter.can_send():
            wait = self.limiter.wait_seconds()
            return False, f"触发本地限流（已用 {self.limiter.used}/{self.limiter.max_per_minute}），需等待 {wait:.0f}s"

        try:
            resp = requests.post(
                self.webhook,
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=10,
                # 企微 webhook 是外网域名，这里的代理设置跟随环境变量；
                # 如果被代理拦了，取消下面这行的注释
                # proxies={"http": None, "https": None},
            )
            data = resp.json()
        except Exception as e:
            return False, f"请求失败: {type(e).__name__}: {e}"

        if data.get("errcode") == 0:
            self.limiter.record()
            return True, "ok"

        errcode = data.get("errcode")
        errmsg = data.get("errmsg", "")
        if errcode == 45009:  # api freq out of limit
            return False, f"企微侧限流: {errmsg}（考虑合并告警或切换自建应用 API）"
        return False, f"企微返回错误 {errcode}: {errmsg}"

    # ------------------------------------------------------------ 主流程

    def flush(self, digest: bool = False) -> PushResult:
        """把 pending 的告警推送出去。

        Args:
            digest: True 时把所有 pending 合并成一条（日报模式）；
                    False 时按等级路由 —— 红色实时推，其余暂不推（等 digest）。
        """
        # 先把重试太久仍失败的告警收殓掉，否则配错的 webhook 会无限重试
        expired = self.repo.expire_stale_alerts(settings.alert.retry_max_age_seconds)
        if expired:
            print(f"[notifier] {expired} 条告警重试超时，已标记 failed")

        pending = self.repo.pending_alerts()
        if not pending:
            return PushResult(True, "skipped", "没有待推送的告警", [])

        realtime_levels = set(settings.alert.realtime_levels)

        if digest:
            batch = pending
        else:
            # 分级路由：只有配置为实时的等级立刻推，其余留在 pending 等日报
            batch = [a for a in pending if a.level in realtime_levels]

        if not batch:
            return PushResult(
                True,
                "skipped",
                f"{len(pending)} 条告警等级未达实时推送阈值（{sorted(realtime_levels)}），留待日报",
                [],
            )

        content = self.build_markdown(batch)
        ids = [a.alert_id for a in batch]

        if not self.enabled:
            # dry-run：没配 webhook 时把内容打到控制台，方便本地调试
            print("\n" + "=" * 60)
            print("[notifier] DRY-RUN（未配置 WOCHAT_WECOM_WEBHOOK，仅打印）")
            print("=" * 60)
            print(content)
            print("=" * 60 + "\n")
            for aid in ids:
                self.repo.mark_alert_pushed(aid, "skipped", "dry_run", "未配置 webhook")
            return PushResult(True, "dry_run", f"{len(batch)} 条告警（dry-run）", ids)

        ok, msg = self.send_markdown(content)
        if ok:
            for aid in ids:
                self.repo.mark_alert_pushed(aid, "sent", "wecom")
            return PushResult(True, "sent", msg, ids)

        # 发送失败**绝不能**把状态改成 failed —— pending_alerts() 只查 pending，
        # 一旦标记就再也不会被 flush 到，告警永久消失（丢警是预警系统最严重的
        # 故障，比重复推送糟糕得多）。这里保持 pending 并记录失败原因，
        # 下个调度周期自动重试；重试次数上限由 expire_stale_alerts() 兜底。
        for aid in ids:
            self.repo.mark_alert_pushed(aid, "pending", "wecom", msg)
        return PushResult(False, "failed", msg, ids)

    def flush_with_retry(self, max_wait: float = 65.0, digest: bool = False) -> PushResult:
        """限流时等一下再试一次。

        比"直接丢弃"好 —— 舆情告警丢了就真丢了。但也不能无限等，
        超过 max_wait 就放弃，让它在 pending 里留着。
        """
        result = self.flush(digest=digest)
        if result.ok or "限流" not in result.message:
            return result

        wait = self.limiter.wait_seconds()
        if wait > max_wait:
            return result

        print(f"[notifier] 限流中，等待 {wait:.0f}s 后重试")
        time.sleep(wait + 1)
        return self.flush(digest=digest)


# ---------------------------------------------------------------- 日报

def build_daily_digest(repo: Repository | None = None, version: str = "v1", hours: int = 24) -> str:
    """组装日报 —— 低等级告警、统计摘要合并成一条。"""
    repo = repo or Repository()

    since = utcnow() - timedelta(hours=hours)
    dist = repo.sentiment_distribution(version, since=since)
    total = sum(dist.values())
    stats = repo.stats()

    lines = [
        f"### 📊 舆情日报（近 {hours} 小时）",
        f"> 生成时间：{utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
        "",
        f"**声量**：{total} 条评论",
        f"**情感分布**：正面 {dist.get('positive', 0)} · 中性 {dist.get('neutral', 0)} · 负面 {dist.get('negative', 0)}",
    ]

    if total:
        neg_ratio = dist.get("negative", 0) / total
        lines.append(f"**负面占比**：{neg_ratio:.1%}")

    lines += [
        "",
        f"**累计数据**：内容 {stats['contents']} · 评论 {stats['comments']} · 分析 {stats['analyses']}",
        "",
        "[打开看板](http://localhost:6666)",
    ]
    return "\n".join(lines)
