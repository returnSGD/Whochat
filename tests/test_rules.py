"""规则清洗测试。

这一层跑在每条数据上，是调用次数最多的一环；
同时 is_spam 的误判会静默改变下游所有统计口径，所以两个方向都要测。
"""

from __future__ import annotations

import pytest

from wochat.pipeline.rules import clean, count_matches, is_meaningful, is_spam, tokenize


class TestClean:
    def test_strips_url(self):
        assert "http" not in clean("看这里 https://example.com/x 很好")
        assert "www" not in clean("看 www.example.com 很好")

    def test_strips_mention(self):
        assert "@" not in clean("感谢 @张三 的分享，东西不错")

    def test_strips_html_and_zero_width(self):
        # 标签替换成空格（而不是删除）是有意的：否则 "<b>加</b>粗" 会粘成 "加粗"
        assert clean("<b>加粗</b>正文") == "加粗 正文"
        assert "​" not in clean("零​宽字符")

    def test_keeps_topic_text(self):
        """话题标签本身是主题信号，#iPhone17# 应当保留成 iPhone17。"""
        assert "iPhone17" in clean("#iPhone17# 用了一个月")
        assert "#" not in clean("#iPhone17# 用了一个月")

    def test_drops_topic_when_asked(self):
        assert "iPhone17" not in clean("#iPhone17# 用了一个月", keep_topic=False)

    def test_collapses_whitespace(self):
        assert clean("  a   b  \n c ") == "a b c"

    def test_empty_input(self):
        assert clean(None) == ""
        assert clean("") == ""


class TestIsSpam:
    @pytest.mark.parametrize(
        "text",
        [
            "加V信 abc12345",
            "微信:abc12345",
            "vx12345",
            "加微 13800138000",
            "QQ 123456789",
            "v:abc12345",
        ],
    )
    def test_detects_real_ads(self, text):
        assert is_spam(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "这个version真的很稳定",
            "看了video觉得不错",
            "质量很好，推荐购买",
            "用了半年没有任何问题，续航也够用",
        ],
    )
    def test_does_not_kill_normal_comments(self, text):
        """回归：_CONTACT 里的裸 v|V 会匹配英文单词首字母，
        "version" 被切成 v+ersion 后误判成广告 —— 违反"宁可漏杀，不可错杀"。"""
        assert is_spam(text) is False

    def test_empty_and_too_short_are_spam(self):
        assert is_spam("") is True
        assert is_spam(None) is True
        assert is_spam("好") is True


class TestIsMeaningful:
    def test_short_noise_is_not_meaningful(self):
        assert is_meaningful("顶") is False
        assert is_meaningful("666") is False

    def test_real_comment_is_meaningful(self):
        assert is_meaningful("这个产品的续航表现让我很满意") is True


class TestTokenize:
    def test_drops_stopwords_and_single_chars(self):
        tokens = tokenize("这个产品非常好用")
        assert "的" not in tokens
        assert "这个" not in tokens

    def test_drops_pure_digits(self):
        assert "123456" not in tokenize("订单号 123456 查询")

    def test_empty(self):
        assert tokenize(None) == []


class TestCountMatches:
    def test_counts_hits(self):
        assert count_matches("发热严重而且卡顿", {"发热", "卡顿", "掉漆"}) == 2

    def test_empty_word_set(self):
        assert count_matches("任意文本", set()) == 0
