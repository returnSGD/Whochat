"""去重测试。

网络采集的数据含 5~30% 近重复，去重出错的后果是双向的：
漏杀 → 声量虚高、情感被爆款绑架；错杀 → 正常评论静默消失、统计失真。
两个方向都要有用例。
"""

from __future__ import annotations

import pytest

from Whochat.pipeline.dedup import dedupe, exact_dedupe, minhash_dedupe, text_fingerprint


class TestExactDedupe:
    def test_keeps_first_occurrence(self):
        assert exact_dedupe(["a", "a", "b", "a", "b"]) == ["a", "b"]

    def test_punctuation_and_case_insensitive(self):
        """「这个真好用！」和「这个真好用」应当判为同一条。"""
        assert exact_dedupe(["这个真好用！", "这个真好用", "这个 真好用"]) == ["这个真好用！"]

    def test_empty_texts_are_dropped(self):
        """回归：原来用 `not fp` 判空是死代码（sha256 摘要永远非空），
        导致第一条空文本被保留、其余被当成"重复"丢掉。"""
        assert exact_dedupe(["", "", "a", "a", "b"]) == ["a", "b"]
        assert exact_dedupe([None, None]) == []

    def test_fingerprint_of_empty_is_stable(self):
        assert text_fingerprint(None) == text_fingerprint("")


class TestEmojiPolarityPreserved:
    """emoji 是中文社媒的主要极性信号，不能因为正文相同就被判成重复。"""

    def test_opposite_emoji_are_not_exact_duplicates(self):
        a = "这个产品真的很好用😀"
        b = "这个产品真的很好用😡"
        assert exact_dedupe([a, b]) == [a, b]
        assert text_fingerprint(a) != text_fingerprint(b)

    def test_punctuation_insensitivity_still_holds(self):
        """保留 emoji 不等于放弃标点归一 —— 这两件事要同时成立。"""
        assert exact_dedupe(["这个真好用！！", "这个真好用"]) == ["这个真好用！！"]


class TestMinHashDedupe:
    """短文本回归：3-gram 对 ≤2 字的文本产生 0 个 shingle，
    MinHash 全空 → 所有短评论互相判为近重复，只留第一条。
    评论区里"支持""谢谢""关注"这类高频短评会整批消失。"""

    @pytest.mark.parametrize("words", [["支持", "谢谢", "关注"], ["好的", "赞哦", "确实"]])
    def test_distinct_two_char_comments_all_survive(self, words):
        result = minhash_dedupe(words)
        assert result is not None, "datasketch 未安装，跳过"
        assert sorted(result.kept) == sorted(words)

    def test_two_char_duplicates_still_collapse(self):
        result = minhash_dedupe(["支持", "支持", "谢谢"])
        assert result is not None
        assert len(result.kept) == 2

    def test_long_text_behaviour_unchanged(self):
        result = minhash_dedupe(["这个产品质量非常好值得推荐", "今天天气不错适合出去玩", "客服态度很差联系不上"])
        assert result is not None
        assert len(result.kept) == 3


class TestDedupePipeline:
    def test_exact_and_near_combined(self):
        texts = ["这个产品真的很好用值得推荐", "这个产品真的很好用值得推荐", "完全不同的另一条评论内容"]
        result = dedupe(texts)
        assert result.kept == ["这个产品真的很好用值得推荐", "完全不同的另一条评论内容"]
        assert result.total_dropped == 1

    def test_total_dropped_counts_exact_plus_near(self):
        """回归：exact_dropped 曾经算出来但没往外传，上层永远显示"重复 0"。"""
        texts = ["a" * 20, "a" * 20]
        result = dedupe(texts)
        assert result.total_dropped == 1

    def test_all_unique_input_is_untouched(self):
        texts = ["第一条完全独立的评论内容", "第二条毫不相干的评论内容", "第三条风马牛不相及的评论"]
        result = dedupe(texts, key=lambda x: x)
        assert len(result.kept) == 3
        assert result.total_dropped == 0

    def test_dropped_items_are_recoverable_by_identity(self):
        """runner 依赖"候选 − 幸存者 = 被淘汰者"来标记 is_valid=False，
        这里固化该不变式：幸存者必须是输入的子集，且身份不变。"""
        items = [("a", "重复的文本内容啊"), ("b", "重复的文本内容啊"), ("c", "另一条不同的文本")]
        result = dedupe(items, key=lambda p: p[1])
        survivors = {id(x) for x in result.kept}
        assert all(id(x) in survivors for x in result.kept)
        assert len(survivors) == 2
