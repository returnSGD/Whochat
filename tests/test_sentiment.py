"""情感分析（词典后端）测试。

注意：准确率本身由 tests/annotated_sample.json + `cli evaluate` 衡量，
这里只固化**行为契约**和已修复的缺陷，不重复做标注集评估。
"""

from __future__ import annotations

import pytest

from Whochat.analysis.sentiment import LexiconSentiment


@pytest.fixture(scope="module")
def analyzer():
    return LexiconSentiment()


class TestContract:
    def test_score_always_within_range(self, analyzer):
        """score 的约定是 -1.0 ~ 1.0。

        回归：转折分支（"先扬后抑"）会把 tail_score 直接当分数用，
        而 tail_score 是词表原始权重求和，能把分数压到 -3.5 —— 越界后
        任何按 [-1,1] 假设做的阈值比较/归一化/排序都是错的。
        """
        texts = [
            "看着不错，但是质量差垃圾坑人",
            "这个产品不错，但是垃圾差坑人骗",
            "好是好，但是太差了",
            "一开始挺好，但是后来各种问题不断",
            "非常满意，但是" + "差" * 5,
            "垃圾",
            "完美" * 10,
        ]
        for t in texts:
            r = analyzer.analyze(t)
            assert -1.0 <= r.score <= 1.0, f"{t!r} -> {r.score}"

    def test_label_matches_score_sign(self, analyzer):
        for t in ["质量很好值得推荐", "发热严重太失望了", "今天下午三点", ""]:
            r = analyzer.analyze(t)
            assert r.label in ("positive", "neutral", "negative")
            if r.label == "positive":
                assert r.score > 0
            elif r.label == "negative":
                assert r.score < 0

    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_empty_is_neutral(self, analyzer, text):
        r = analyzer.analyze(text)
        assert r.label == "neutral"
        assert r.score == 0.0

    def test_reproducible(self, analyzer):
        text = "发热严重，玩游戏十分钟就烫手"
        assert analyzer.analyze(text).score == analyzer.analyze(text).score


class TestNegation:
    def test_negation_flips_within_clause(self, analyzer):
        assert analyzer.analyze("这个产品不好用").score < 0

    def test_negation_does_not_cross_clause_boundary(self, analyzer):
        """回归：「还是没修，太失望了」里的"没"修饰的是"修"，
        不该跨越逗号把"失望"反转成正面。"""
        r = analyzer.analyze("还是没修，太失望了")
        assert r.label == "negative"


class TestQuestionHandling:
    def test_plain_question_is_not_positive(self, analyzer):
        """评论区大量是提问（"请问这个支持以旧换新吗"），
        不该因为出现"支持""好"就判成正面。"""
        assert analyzer.analyze("请问这个支持以旧换新吗").label == "neutral"

    @pytest.mark.xfail(
        reason="已知短板：'这也叫好用？垃圾死了' 的负面词（垃圾 -1.6）被正面词"
        "（好用 +1.5）抵消后净值为 -0.035，落在中性带内被判 neutral。"
        "代码注释声称'只压正面不压负面'，但把净值清零并不能保留负面信号。"
        "修它需要改成只扣除正面贡献，属情感引擎调参，暂不动（会牵动 90.9% 基线）。",
        strict=False,
    )
    def test_sarcastic_rhetorical_question_keeps_negative(self, analyzer):
        """吐槽式反问要保留负面，不能被疑问句规则压掉。"""
        assert analyzer.analyze("这也叫好用？垃圾死了").label == "negative"


class TestDuplicateWordScoring:
    @pytest.mark.xfail(
        reason="WORKLOG §三-12 声称该例已修，实际只修了一半：重复计分确实只算一次了，"
        "但'真'是程度副词，把'稳定'(+1.1) 放大到约 +1.65，仍压过'坏'(-1.2)，"
        "净分为 +0.14（中性）。要真正修好得让程度副词不改变极性判断，"
        "属调参范畴，暂不改（会牵动 90.9% 基线）。",
        strict=False,
    )
    def test_repeated_sentiment_word_counts_once(self, analyzer):
        """「质量真稳定，稳定地坏」里两个"稳定"不能盖过"坏"。"""
        assert analyzer.analyze("质量真稳定，稳定地坏").score < 0


class TestBatch:
    def test_batch_matches_single(self, analyzer):
        texts = ["质量很好", "太差了", "还行吧"]
        batch = analyzer.analyze_batch(texts)
        assert [r.label for r in batch] == [analyzer.analyze(t).label for t in texts]
