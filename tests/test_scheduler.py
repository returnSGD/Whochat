"""调度任务的回归测试。

最要紧的一条：快通道**每个周期都要 flush**，不管本轮有没有产生新告警。
发送失败的告警按设计保持 pending 等重试，如果只在"有新告警"时才 flush，
那次失败的告警就永远等不到重试，6 小时后被 expire_stale_alerts 标记 failed
—— 企微限流（45009）或一次网络抖动就足以永久丢警。
"""

from __future__ import annotations


class TestFastAlertAlwaysFlushes:
    def test_flush_called_even_without_new_alerts(self, repo, monkeypatch):
        import wochat.alert.notifier as noti_mod
        import wochat.alert.rules_engine as rules_mod

        calls: list[str] = []

        class FakeEngine:
            def __init__(self, repo=None):
                pass

            def run_and_record(self, **kwargs):
                return []  # 本轮没有新告警

        class FakeNotifier:
            def __init__(self, repo=None):
                pass

            def flush(self, **kwargs):
                calls.append("flush")
                return noti_mod.PushResult(True, "skipped", "没有待推送的告警", [])

        monkeypatch.setattr(rules_mod, "RuleEngine", FakeEngine)
        monkeypatch.setattr(noti_mod, "WeComNotifier", FakeNotifier)

        from wochat.scheduler.jobs import job_fast_alert

        job_fast_alert()

        assert calls == ["flush"], "没有新告警时也必须 flush，否则 pending 告警无法重试"
