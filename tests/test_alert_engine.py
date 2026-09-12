"""快通道预警引擎测试。

预警出错的代价是不对称的：漏报会错过处置窗口，误报会让人无视通知
（告警疲劳比漏报更致命）。所以两个方向都要钉死。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from Whochat.alert.rules_engine import RuleEngine, seed_default_rules
from Whochat.store.models import utcnow


def _comment(cid: str, text: str, *, hours_ago: float = 0.0, platform: str = "xhs") -> dict:
    return {
        "comment_id": cid,
        "content_id": "c1",
        "platform": platform,
        "text": text,
        "publish_time": utcnow() - timedelta(hours=hours_ago),
    }


@pytest.fixture
def engine(repo):
    return RuleEngine(repo)


class TestSensitiveWordRule:
    """风险词是独立于情绪词的一条线。"""

    def test_triggers_on_sensitive_words(self, engine, repo):
        seed_default_rules(repo)
        repo.upsert_comments(
            [
                _comment("s1", "已经向12315投诉了，准备走法律程序起诉"),
                _comment("s2", "准备发律师函，要求退货赔偿"),
                _comment("s3", "已向市场监管部门实名举报"),
            ]
        )
        hits = engine.evaluate()
        assert any(h.rule_id == "sensitive_hit" for h in hits), [h.rule_id for h in hits]

    def test_does_not_trigger_on_normal_chatter(self, engine, repo):
        seed_default_rules(repo)
        repo.upsert_comments([_comment(f"n{i}", "这个产品用着还不错，推荐给大家") for i in range(5)])
        hits = engine.evaluate()
        assert not any(h.rule_id == "sensitive_hit" for h in hits)

    def test_sensitive_words_are_live_from_dict_file(self, engine, repo):
        """词表是在评估时读取的，改文件立刻生效，不需要重新 seed 规则。"""
        from Whochat.pipeline.rules import sensitive_words

        assert len(sensitive_words()) > 0, "dicts/sensitive_words.txt 应当存在且非空"


class TestNegativeRatioRule:
    def test_matched_list_contains_only_negative_comments(self, engine, repo):
        """回归：占比规则触发时，命中列表曾经混入中性/正面评论，
        "命中 N 条"里有一大半是好评，严重误导严重程度判断。"""
        seed_default_rules(repo)
        repo.upsert_comments(
            [
                _comment("p1", "质量很好，非常满意，推荐购买"),
                _comment("p2", "东西不错，物流也快"),
                _comment("n1", "发热严重，售后也联系不上，太失望了"),
                _comment("n2", "垃圾产品，做工粗糙，劝退"),
                _comment("n3", "卡顿闪退，质量太差，后悔买了"),
            ]
        )
        hits = {h.rule_id: h for h in engine.evaluate()}
        assert "neg_ratio" in hits, f"触发规则: {list(hits)}"
        for item in hits["neg_ratio"].matched:
            assert item.get("sentiment") == "negative"


class TestCountRule:
    def test_no_threshold_means_no_count_trigger(self, engine, repo):
        """回归：只配了 negative_ratio 没配 threshold 的规则，
        曾经因为 threshold 有默认值 10 而被数量分支误触发。"""
        repo.upsert_rule(
            "ratio_only",
            "只配占比",
            {"negative_ratio": 0.9, "window_seconds": 3600},  # 故意不给 threshold
            level="orange",
            cooldown_seconds=0,
            channels=["wecom"],
            enabled=True,
        )
        repo.upsert_comments([_comment(f"c{i}", "还行吧，就那样") for i in range(50)])
        hits = engine.evaluate()
        assert not hits, "没有 threshold 就不该走数量触发分支"


class TestReplayWindow:
    def test_replay_finds_historical_comments(self, engine, repo):
        """爬虫断线补数时，数据 publish_time 在过去，默认实时窗口扫不到 ——
        必须能用 since/until 回放，否则整批舆情静默漏警。"""
        seed_default_rules(repo)
        repo.upsert_comments(
            [_comment(f"h{i}", "垃圾骗人的东西，太失望了", hours_ago=48) for i in range(40)]
        )

        # 实时窗口（默认 1 小时）看不到 48 小时前的数据
        assert not engine.evaluate(), "实时模式不应命中历史数据"

        # 回放模式能扫到
        since = utcnow() - timedelta(hours=72)
        hits = engine.evaluate(since=since, until=utcnow(), skip_cooldown=True)
        assert any(h.rule_id == "neg_surge" for h in hits), [h.rule_id for h in hits]


class TestRealtimeWindowUpperBound:
    def test_future_timestamps_are_not_counted(self, engine, repo):
        """回归：实时窗口曾经只有下界，没有 <= now 的上界。

        平台时间多为本地时间（+8h），而 parse_time 对无时区字符串按 UTC 解析，
        于是评论的 publish_time 会"落在未来 8 小时"。没有上界时它们会被计入，
        冷却期一到就反复误报同一批数据。"""
        seed_default_rules(repo)
        repo.upsert_comments(
            [
                {
                    "comment_id": f"f{i}",
                    "content_id": "c1",
                    "platform": "xhs",
                    "text": "垃圾东西，太失望了",
                    "publish_time": utcnow() + timedelta(hours=8),
                }
                for i in range(40)
            ]
        )
        assert engine.evaluate() == [], "未来时间戳不应进入实时窗口"


class TestCooldown:
    def test_cooldown_suppresses_second_alert(self, engine, repo):
        seed_default_rules(repo)
        repo.upsert_comments([_comment(f"c{i}", "太差了，垃圾", hours_ago=48) for i in range(40)])
        since = utcnow() - timedelta(hours=72)

        first = engine.run_and_record(since=since, skip_cooldown=True)
        assert first, "第一次应当触发并落库"

        # 冷却期内再跑，不应重复触发
        assert engine.evaluate(since=since) == []

    def test_skip_cooldown_forces_replay(self, engine, repo):
        seed_default_rules(repo)
        repo.upsert_comments([_comment(f"c{i}", "太差了，垃圾", hours_ago=48) for i in range(40)])
        since = utcnow() - timedelta(hours=72)

        engine.run_and_record(since=since, skip_cooldown=True)
        replay = engine.evaluate(since=since, skip_cooldown=True)
        assert replay, "回放模式必须跳过冷却，否则整个窗口的告警会被第一条吞掉"
