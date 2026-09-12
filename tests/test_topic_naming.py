"""LLM 主题命名测试。

聚类出来的是 c-TF-IDF 关键词碎片，"发热 / 续航 / 掉电" 这种标签只是线索，
不是人能直接用的主题名。这个功能就是把碎片归纳成一句人话。

钉住的约束：**命名失败绝不能把标签弄丢**。主题名难看总好过看板上一片空白。
"""

from __future__ import annotations

import json

from Whochat.analysis.topics import (
    NAME_SYSTEM_PROMPT,
    TopicInfo,
    TopicResult,
    build_name_prompt,
    name_topics,
)
from Whochat.pipeline.llm_client import LLMClient


def _client(stub):
    return LLMClient(base_url=stub.base_url, api_key="k", model="m")


def _result(n: int = 3) -> TopicResult:
    return TopicResult(
        ok=True,
        topics=[
            TopicInfo(
                topic_id=i,
                label=f"关键词{i} / 词{i}b",
                keywords=[f"kw{i}", f"kw{i}b", f"kw{i}c"],
                doc_count=10 - i,
                rep_docs=[f"第{i}个主题的代表评论内容"],
            )
            for i in range(n)
        ],
    )


def _labels(*names: str) -> str:
    return json.dumps(
        {"labels": [{"i": i, "name": n} for i, n in enumerate(names)]}
    )


class TestNaming:
    def test_replaces_labels(self, llm_stub):
        llm_stub.push_chat(_labels("售后维修进度慢", "屏幕发热与续航", "物流太慢"))
        result = _result()
        ok, note = name_topics(result, client=_client(llm_stub))

        assert ok is True
        assert [t.label for t in result.topics] == [
            "售后维修进度慢",
            "屏幕发热与续航",
            "物流太慢",
        ]
        assert "3/3" in note

    def test_keywords_are_preserved(self, llm_stub):
        """只换 label，keywords 原样保留 —— 所以重命名不丢信息，
        "关键词拼标签"随时能重新拼出来。"""
        llm_stub.push_chat(_labels("甲", "乙", "丙"))
        result = _result()
        before = [list(t.keywords) for t in result.topics]

        name_topics(result, client=_client(llm_stub))

        assert [list(t.keywords) for t in result.topics] == before

    def test_partial_naming_keeps_original_labels(self, llm_stub):
        """模型只给了 2 个名字（3 个主题）—— 第 3 个必须保留原标签，不能留空。"""
        llm_stub.push_chat(_labels("甲", "乙"))
        result = _result()
        original_third = result.topics[2].label

        ok, note = name_topics(result, client=_client(llm_stub))

        assert ok is True
        assert result.topics[0].label == "甲"
        assert result.topics[2].label == original_third
        assert "2/3" in note

    def test_empty_name_is_ignored(self, llm_stub):
        """模型返回空字符串时保留原标签，别把好标签换成空的。"""
        llm_stub.push_chat(_labels("", "乙", ""))
        result = _result()
        first, third = result.topics[0].label, result.topics[2].label

        name_topics(result, client=_client(llm_stub))

        assert result.topics[0].label == first
        assert result.topics[1].label == "乙"
        assert result.topics[2].label == third

    def test_out_of_range_index_falls_back_to_position(self, llm_stub):
        """模型从 1 开始数序号时不能整体错位。"""
        llm_stub.push_chat(
            json.dumps(
                {
                    "labels": [
                        {"i": 1, "name": "第一"},
                        {"i": 2, "name": "第二"},
                        {"i": 3, "name": "第三"},
                    ]
                }
            )
        )
        result = _result()
        name_topics(result, client=_client(llm_stub))
        assert [t.label for t in result.topics] == ["第一", "第二", "第三"]

    def test_display_order_is_untouched(self, llm_stub):
        """命名不能改变主题排序 —— 排序是按讨论量来的，分析结论依赖它。"""
        llm_stub.push_chat(_labels("甲", "乙", "丙"))
        result = _result()
        before = [t.topic_id for t in result.topics]
        name_topics(result, client=_client(llm_stub))
        assert [t.topic_id for t in result.topics] == before


class TestFailureKeepsLabels:
    def test_no_llm_keeps_labels(self, llm_stub):
        result = _result()
        before = [t.label for t in result.topics]
        ok, note = name_topics(result, client=None)
        assert ok is False
        assert [t.label for t in result.topics] == before
        assert "未配置" in note

    def test_api_failure_keeps_labels(self, llm_stub):
        llm_stub.default = (401, {"error": {"message": "bad key"}})
        result = _result()
        before = [t.label for t in result.topics]
        ok, note = name_topics(result, client=_client(llm_stub))
        assert ok is False
        assert [t.label for t in result.topics] == before
        assert "bad key" in note

    def test_garbage_response_keeps_labels(self, llm_stub):
        llm_stub.push_chat("完全不是 JSON")
        result = _result()
        before = [t.label for t in result.topics]
        ok, _ = name_topics(result, client=_client(llm_stub))
        assert ok is False
        assert [t.label for t in result.topics] == before

    def test_missing_labels_key_keeps_labels(self, llm_stub):
        llm_stub.push_chat(json.dumps({"result": []}))
        result = _result()
        before = [t.label for t in result.topics]
        ok, note = name_topics(result, client=_client(llm_stub))
        assert ok is False
        assert [t.label for t in result.topics] == before
        assert "labels" in note

    def test_no_topics_is_a_no_op(self, llm_stub):
        ok, note = name_topics(TopicResult(ok=True, topics=[]), client=_client(llm_stub))
        assert ok is False
        assert llm_stub.calls == []  # 没主题就别浪费一次请求


class TestPrompt:
    def test_prompt_includes_keywords_and_reps(self):
        p = build_name_prompt(_result(1).topics)
        assert "kw0" in p
        assert "第0个主题的代表评论内容" in p
        assert "[0]" in p

    def test_prompt_forbids_low_information_names(self):
        """「其他」「主题一」这类名字没有信息量，必须在指令里明确禁止。"""
        assert "其他" in NAME_SYSTEM_PROMPT
        assert "杂项" in NAME_SYSTEM_PROMPT

    def test_prompt_requires_one_name_per_item(self):
        assert "一条都不能少" in NAME_SYSTEM_PROMPT
