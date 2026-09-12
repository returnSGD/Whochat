"""LLM 打标 + 流水线接线测试。

这里钉的核心是**契约**，不是"模型答得准不准"（那要靠真实数据评估）：

1. `clean_batch` 返回的列表必须与输入**等长且顺序一致** —— 调用方按
   `zip(comments, results)` 消费，长度对不上就会把 A 的标签贴到 B 身上，且完全静默。
2. **模型漏项/调用失败时，评论必须继续被分析**，只是不带 LLM 标签。
   丢数据比少打一个标严重得多，这是本项目一贯的口径。
3. 用了 LLM 的分析必须落在**独立版本号**里，不能和历史结果混在一起 ——
   否则事后无法区分"这批到底经没经过模型"，demo 的幂等基线也就废了。
"""

from __future__ import annotations

import json

from Whochat.pipeline.llm_clean import SYSTEM_PROMPT, LLMCleaner, build_user_prompt
from Whochat.pipeline.llm_client import LLMClient
from Whochat.pipeline.runner import Pipeline
from Whochat.store.models import AnalysisResult

# ⚠️ 样本必须用**像评论的文字**，不能用 "a"/"b"/"c" 这类单字母：
# 规则层会把它们判成垃圾直接短路，请求根本到不了模型 —— 于是测试测的是
# 别的东西，全绿但毫无意义。（这个坑真踩过一次。）
T1 = "物流很快，包装完好"
T2 = "客服态度不错，就是发货有点慢"
T3 = "用了三天就出问题了，太失望"
TEXTS = [T1, T2, T3]


def _cleaner(stub, model: str = "m"):
    return LLMCleaner(LLMClient(base_url=stub.base_url, api_key="k", model=model))


def _results(*items) -> str:
    return json.dumps({"results": list(items)})


def _item(i: int, **kw) -> dict:
    base = {
        "i": i,
        "is_ad": False,
        "is_valid": True,
        "subject": None,
        "sentiment_hint": "neutral",
        "keywords": [],
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 契约：对齐


class TestBatchContract:
    def test_result_length_always_matches_input(self, llm_stub):
        llm_stub.push_chat(_results(_item(0), _item(1), _item(2)))
        out = _cleaner(llm_stub).clean_batch(TEXTS, verbose=False)
        assert len(out) == 3

    def test_aligns_by_index_not_position(self, llm_stub):
        """模型把顺序打乱返回是常事，必须按 i 对齐而不是按出现顺序。"""
        llm_stub.push_chat(
            _results(
                _item(2, subject="第三条"),
                _item(0, subject="第一条"),
                _item(1, subject="第二条"),
            )
        )
        out = _cleaner(llm_stub).clean_batch(TEXTS, verbose=False)
        assert [r.subject for r in out] == ["第一条", "第二条", "第三条"]

    def test_omitted_item_is_flagged_not_faked(self, llm_stub):
        """模型漏了第 1 条 —— 那一条必须是"未打标"，**绝不能**伪造成
        "模型判定它不是广告"。后者是最坏的静默错误：看不出有东西丢了。"""
        llm_stub.push_chat(_results(_item(0, subject="A"), _item(2, subject="C")))
        out = _cleaner(llm_stub).clean_batch(TEXTS, verbose=False)

        assert out[1].from_llm is False
        assert out[1].error == "模型漏项"
        # 关键：漏项那条既不能说"模型说它有效"，也不能说"模型说是广告"
        assert out[1].is_ad is None
        assert out[1].is_valid is None
        assert out[0].subject == "A" and out[2].subject == "C"

    def test_out_of_range_index_falls_back_to_position(self, llm_stub):
        """模型把序号写飞了（比如从 1 开始数）时不能整批丢掉。"""
        llm_stub.push_chat(_results(_item(1, subject="X"), _item(2, subject="Y")))
        out = _cleaner(llm_stub).clean_batch(TEXTS[:2], verbose=False)
        assert out[0].subject == "X"
        assert out[1].subject == "Y"

    def test_flat_single_object_without_results_key(self, llm_stub):
        """批量=1 时有的模型会把结果直接平铺在最外层。"""
        llm_stub.push_chat(json.dumps(_item(0, subject="裸的")))
        out = _cleaner(llm_stub).clean_batch([T1], verbose=False)
        assert out[0].subject == "裸的"

    def test_all_items_flagged_when_response_is_garbage(self, llm_stub):
        llm_stub.push_chat("完全不是 JSON")
        out = _cleaner(llm_stub).clean_batch(TEXTS[:2], verbose=False)
        assert len(out) == 2
        assert all(not r.from_llm and r.error for r in out)

    def test_all_items_flagged_when_api_fails(self, llm_stub):
        llm_stub.default = (401, {"error": {"message": "bad key"}})
        out = _cleaner(llm_stub).clean_batch(TEXTS[:2], verbose=False)
        assert len(out) == 2
        assert all("bad key" in (r.error or "") for r in out)


# ---------------------------------------------------------------- 省钱的前置过滤


class TestPreFilter:
    def test_empty_and_spam_never_reach_the_model(self, llm_stub):
        """空白和规则已判定为广告的，不该花 token 送模型。"""
        llm_stub.push_chat(_results(_item(0, is_ad=False, is_valid=True)))
        out = _cleaner(llm_stub).clean_batch(
            ["", "   ", "加V信 abc12345", T1], verbose=False
        )

        assert out[0].error == "empty"
        assert out[1].error == "empty"
        assert out[2].is_ad is True
        # 只有最后一条真的发了请求（没配默认响应的话会退避重试 8 次）
        assert len(llm_stub.calls) == 1
        sent = json.dumps(llm_stub.last_body, ensure_ascii=False)
        assert T1 in sent
        assert "abc12345" not in sent

    def test_batches_by_size(self, llm_stub):
        """10 条文本、batch=4 → 3 次请求（4+4+2），而不是 10 次。"""
        for _ in range(3):
            llm_stub.push_chat(_results())
        texts = [f"{T1}第{i}条" for i in range(10)]
        _cleaner(llm_stub).clean_batch(texts, batch_size=4, verbose=False)
        assert len(llm_stub.calls) == 3


class TestPrompt:
    def test_prompt_carries_index_and_text(self):
        p = build_user_prompt(["甲甲甲", "乙乙乙"])
        assert "[0] 甲甲甲" in p
        assert "[1] 乙乙乙" in p

    def test_system_prompt_requires_one_result_per_item(self):
        """必须明确要求"每条都要有结果"，否则漏项率会明显上升。"""
        assert "每一个序号" in SYSTEM_PROMPT or "一条都不能少" in SYSTEM_PROMPT

    def test_system_prompt_warns_about_intent_vs_keyword(self):
        """回归：规则法就栽在"看关键词不看意图"上 —— 用户在**批评**刷单，
        不是在发广告。这条指令必须留着。"""
        assert "意图" in SYSTEM_PROMPT


# ---------------------------------------------------------------- 流水线接线


def _seed(repo, texts):
    repo.upsert_comments(
        [
            dict(
                comment_id=f"c{i}",
                content_id="p1",
                platform="xhs",
                text=t,
                publish_time=None,
            )
            for i, t in enumerate(texts)
        ]
    )


def _wire(monkeypatch, stub):
    monkeypatch.setattr("Whochat.pipeline.runner.get_cleaner", lambda: _cleaner(stub))


class TestPipelineIntegration:
    def test_version_is_isolated_when_llm_is_used(self, repo, llm_stub, monkeypatch):
        """LLM 结果必须落在独立版本号里，否则事后分不清哪批经过模型。"""
        _seed(repo, [T1])
        llm_stub.push_chat(_results(_item(0, is_ad=False, is_valid=True)))
        _wire(monkeypatch, llm_stub)

        p = Pipeline(repo, use_llm=True)
        assert p.version == "lexicon-v1-llm"
        p.analyze()

        assert repo.session.get(AnalysisResult, ("c0", "comment", "lexicon-v1-llm"))

    def test_version_unchanged_without_llm(self, repo, monkeypatch):
        """没配 LLM 时版本号必须和以前一模一样 —— demo 的幂等基线靠它。"""
        monkeypatch.setattr("Whochat.pipeline.runner.get_cleaner", lambda: None)
        _seed(repo, [T1])
        p = Pipeline(repo, use_llm=True)
        assert p.version == "lexicon-v1"
        p.analyze()
        assert repo.session.get(AnalysisResult, ("c0", "comment", "lexicon-v1"))

    def test_subject_and_keywords_are_persisted(self, repo, llm_stub, monkeypatch):
        _seed(repo, ["这个型号发热太严重了"])
        llm_stub.push_chat(
            _results(_item(0, subject="某品牌X型号手机", keywords=["发热", "续航"]))
        )
        _wire(monkeypatch, llm_stub)

        stats = Pipeline(repo, use_llm=True).analyze()

        row = repo.session.get(AnalysisResult, ("c0", "comment", "lexicon-v1-llm"))
        assert row.subject == "某品牌X型号手机"
        assert row.keywords == ["发热", "续航"]
        assert stats.llm_tagged == 1

    def test_sentiment_still_comes_from_analyzer_not_llm(
        self, repo, llm_stub, monkeypatch
    ):
        """ADR#5：LLM 只打标，**不判情感**。模型给的 sentiment_hint 不许进库。"""
        _seed(repo, ["发热严重，售后也联系不上，太失望了"])
        llm_stub.push_chat(_results(_item(0, sentiment_hint="positive")))
        _wire(monkeypatch, llm_stub)

        Pipeline(repo, use_llm=True).analyze()

        row = repo.session.get(AnalysisResult, ("c0", "comment", "lexicon-v1-llm"))
        assert row.sentiment_label == "negative"  # 词典法的判断，不是模型说的 positive

    def test_llm_ad_is_rejected_and_counted(self, repo, llm_stub, monkeypatch):
        """规则正则漏掉的广告由模型补上，且要计入"过滤广告"的统计。"""
        _seed(repo, ["正常的一条评论内容", "点击主页链接领取福利哦"])
        llm_stub.push_chat(
            _results(_item(0, is_ad=False), _item(1, is_ad=True, is_valid=False))
        )
        _wire(monkeypatch, llm_stub)

        stats = Pipeline(repo, use_llm=True).analyze()

        assert stats.dropped_spam == 1
        assert stats.analyzed == 1
        # 被判广告的也要落一行 is_valid=False，否则下一轮又会被当成待分析
        row = repo.session.get(AnalysisResult, ("c1", "comment", "lexicon-v1-llm"))
        assert row is not None and row.is_valid is False

    def test_omitted_item_is_still_analyzed(self, repo, llm_stub, monkeypatch):
        """模型漏了第 1 条 —— 那条仍要被正常分析，不能丢。

        这是本文件最重要的一条：LLM 是可选增强，不是数据通路的守门人。
        """
        _seed(repo, ["第一条正常的评论内容", "第二条正常的评论内容"])
        llm_stub.push_chat(_results(_item(0, subject="只有第一条")))
        _wire(monkeypatch, llm_stub)

        stats = Pipeline(repo, use_llm=True).analyze()

        assert stats.analyzed == 2  # 两条都在
        missed = repo.session.get(AnalysisResult, ("c1", "comment", "lexicon-v1-llm"))
        assert missed is not None
        assert missed.subject is None  # 没打上标
        assert missed.is_valid is True  # 但也没被误判成广告

    def test_llm_failure_does_not_break_the_pipeline(self, repo, llm_stub, monkeypatch):
        """API 挂掉时整条链路照常跑完 —— 只是没有 LLM 标签。"""
        _seed(repo, ["评论甲内容比较长", "评论乙内容也比较长"])
        llm_stub.default = (500, {"error": {"message": "server on fire"}})
        _wire(monkeypatch, llm_stub)
        monkeypatch.setattr("Whochat.pipeline.llm_client.time.sleep", lambda s: None)

        stats = Pipeline(repo, use_llm=True).analyze()

        assert stats.analyzed == 2
        assert stats.llm_tagged == 0

    def test_keywords_fall_back_to_rules_when_llm_returns_none(
        self, repo, llm_stub, monkeypatch
    ):
        """模型给了空关键词时回落到 jieba 抽取，而不是留空。"""
        _seed(repo, ["发热严重售后太差"])
        llm_stub.push_chat(_results(_item(0, keywords=[])))
        _wire(monkeypatch, llm_stub)

        Pipeline(repo, use_llm=True).analyze()

        row = repo.session.get(AnalysisResult, ("c0", "comment", "lexicon-v1-llm"))
        assert row.keywords  # 非空
