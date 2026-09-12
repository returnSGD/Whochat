"""LLM 客户端测试。

**为什么起一个假服务器而不是 mock `requests`**：这个客户端最容易出错的地方
恰恰是 HTTP 层 —— 端点探测、状态码分支、退避重试、JSON Mode 降级。把
`requests.post` mock 掉，就等于把要测的东西测掉了（测的是"我以为会发出的请求"，
而不是"真实发生的行为"）。所以这里起一个真的 `http.server`，让 `requests`
真的走一遍 socket。

**不需要任何 API key 或外网**：全部打向 127.0.0.1 上的桩服务。
"""

from __future__ import annotations

import pytest

from Whochat.pipeline.llm_client import (
    LLMClient,
    LLMUsage,
    _loads_tolerant,
    align_by_index,
)


def _chat(content: str, **usage) -> dict:
    """构造一个成功的对话响应体。"""
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage:
        body["usage"] = usage
    return body


# ---------------------------------------------------------------- 端点探测


class TestEndpointProbing:
    def test_falls_back_to_v1_when_bare_path_404s(self, llm_stub):
        """用户常只写到域名（https://api.deepseek.com），而多数服务端在 /v1 下。"""
        llm_stub.default = (404, {"error": {"message": "no such endpoint"}})
        llm_stub.push(404, {"error": {"message": "no such endpoint"}})
        llm_stub.push(200, _chat("pong"))

        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        resp = c.chat([{"role": "user", "content": "hi"}])

        assert resp.ok is True
        assert llm_stub.paths == ["/chat/completions", "/v1/chat/completions"]

    def test_caches_endpoint_after_success(self, llm_stub):
        """探测成功后不该每次都先撞一遍错的候选 —— 那是白花一倍的请求。"""
        llm_stub.push(404, {})
        llm_stub.push(200, _chat("pong"))
        llm_stub.push(200, _chat("pong"))
        llm_stub.push(200, _chat("pong"))

        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        c.chat([{"role": "user", "content": "1"}])
        c.chat([{"role": "user", "content": "2"}])

        assert llm_stub.paths == [
            "/chat/completions",
            "/v1/chat/completions",
            "/v1/chat/completions",
        ]

    def test_explicit_full_endpoint_is_used_as_is(self, llm_stub):
        llm_stub.push(200, _chat("pong"))
        c = LLMClient(
            base_url=f"{llm_stub.base_url}/v1/chat/completions", api_key="k", model="m"
        )
        assert c.chat([{"role": "user", "content": "hi"}]).ok
        assert llm_stub.paths == ["/v1/chat/completions"]


# ---------------------------------------------------------------- 模型解析


class TestModelResolution:
    def test_explicit_model_wins_without_listing(self, llm_stub):
        llm_stub.push(200, _chat("pong"))
        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="my-model")
        c.chat([{"role": "user", "content": "hi"}])
        assert llm_stub.paths == ["/chat/completions"]  # 没去调 /models

    def test_auto_picks_chat_model_and_skips_embeddings(self, llm_stub):
        """自动挑模型时要排除 embed/rerank/tts 之类 —— 挑中它们会每条都失败。"""
        llm_stub.push(
            200,
            {
                "data": [
                    {"id": "text-embedding-3-small"},
                    {"id": "whisper-1"},
                    {"id": "bge-rerank-v2"},
                    {"id": "deepseek-chat"},
                    {"id": "deepseek-reasoner"},
                ]
            },
        )
        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="")
        model, note = c.resolve_model()
        assert model == "deepseek-chat"
        assert "自动选择" in note

    def test_auto_fails_loudly_when_nothing_listed(self, llm_stub):
        """挑不出来必须明确报错 —— 猜错的表现是"每条都失败但静默"。"""
        llm_stub.default = (401, {"error": {"message": "invalid api key"}})
        c = LLMClient(base_url=llm_stub.base_url, api_key="bad", model="")
        model, note = c.resolve_model()
        assert model is None
        assert "WHOCHAT_LLM_MODEL" in note
        assert "401" in note

    def test_keeps_provider_order_when_nothing_matches(self, llm_stub):
        """偏好表全没命中时，保留供应商给出的顺序（而不是重排成字母序）。

        供应商通常把主力模型排在前面，重排反而会把一个陌生但合适的模型
        换成一个更差的 —— 而且"同优先级按字母序"这种规则对用户是隐形的。
        """
        llm_stub.push(200, {"data": [{"id": "zzz-model"}, {"id": "aaa-model"}]})
        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="")
        model, _ = c.resolve_model()
        assert model == "zzz-model"

    def test_preference_beats_provider_order(self, llm_stub):
        """命中偏好表时优先用偏好的那个，哪怕它不是第一个。"""
        llm_stub.push(200, {"data": [{"id": "some-random-model"}, {"id": "qwen-plus"}]})
        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="")
        model, _ = c.resolve_model()
        assert model == "qwen-plus"


# ---------------------------------------------------------------- 错误分支


class TestErrorHandling:
    def test_401_fails_fast_without_retry(self, llm_stub):
        """认证失败重试没有意义，只会白等 3 次退避。"""
        llm_stub.default = (401, {"error": {"message": "invalid api key"}})
        c = LLMClient(base_url=llm_stub.base_url, api_key="bad", model="m")
        resp = c.chat([{"role": "user", "content": "hi"}])
        assert resp.ok is False
        assert resp.status == 401
        assert "invalid api key" in resp.error
        assert len(llm_stub.calls) == 1

    def test_429_retries_then_succeeds(self, llm_stub, monkeypatch):
        monkeypatch.setattr("Whochat.pipeline.llm_client.time.sleep", lambda s: None)
        llm_stub.push(429, {"error": {"message": "rate limited"}})
        llm_stub.push(200, _chat("pong"))

        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        assert c.chat([{"role": "user", "content": "hi"}]).ok is True
        assert len(llm_stub.calls) == 2

    def test_connection_error_is_swallowed(self):
        """连不上必须返回 ok=False 而不是抛异常 —— 整条链路不能因可选组件挂掉。"""
        c = LLMClient(base_url="http://127.0.0.1:1", api_key="k", model="m")
        c.max_retries = 0
        resp = c.chat([{"role": "user", "content": "hi"}])
        assert resp.ok is False
        assert resp.error

    def test_json_mode_unsupported_downgrades_automatically(self, llm_stub):
        """有的供应商不认 response_format，报 400 时要能自动去掉再试。"""
        llm_stub.push(400, {"error": {"message": "response_format is not supported"}})
        llm_stub.push(200, _chat('{"ok": true}'))

        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        resp = c.chat([{"role": "user", "content": "hi"}], json_mode=True)

        assert resp.ok is True
        assert "response_format" in llm_stub.calls[0]["body"]
        assert "response_format" not in llm_stub.calls[1]["body"]

    def test_non_json_response_does_not_crash(self, llm_stub):
        llm_stub.default = (200, "<html>502 Bad Gateway</html>")
        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        resp = c.chat([{"role": "user", "content": "hi"}])
        assert resp.ok is False
        assert "JSON" in resp.error

    def test_usage_is_accumulated(self, llm_stub):
        llm_stub.push(200, _chat('{"a":1}', prompt_tokens=10, completion_tokens=5))
        llm_stub.push(200, _chat('{"a":2}', prompt_tokens=20, completion_tokens=7))

        c = LLMClient(base_url=llm_stub.base_url, api_key="k", model="m")
        c.chat_json("sys", "u1")
        c.chat_json("sys", "u2")

        assert c.usage.requests == 2
        assert c.usage.prompt_tokens == 30
        assert c.usage.completion_tokens == 12
        assert "42 tokens" in c.usage.report()


# ---------------------------------------------------------------- 容错解析


class TestTolerantJson:
    @pytest.mark.parametrize(
        "raw",
        [
            '{"a": 1}',
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            '好的，结果是：{"a": 1}',
            '{"a": 1} 以上。',
        ],
    )
    def test_parses_common_wrappers(self, raw):
        """模型套 markdown 代码块、或在 JSON 前后加一句话，都是常态。"""
        assert _loads_tolerant(raw) == {"a": 1}

    @pytest.mark.parametrize("raw", ["", "完全不是 JSON", "[1,2,3]", "null"])
    def test_returns_none_on_garbage(self, raw):
        assert _loads_tolerant(raw) is None

    def test_truncated_json_is_repaired(self):
        """输出被 max_tokens 截断是最常见的失败形态，json_repair 要能兜住。"""
        pytest.importorskip("json_repair")
        out = _loads_tolerant('{"subject": "某品牌", "keywords": ["发热"')
        assert out is not None
        assert out.get("subject") == "某品牌"


class TestUsage:
    def test_merge_tolerates_missing_fields(self):
        u = LLMUsage()
        u.merge({})
        assert u.requests == 1
        assert u.total_tokens == 0


# ---------------------------------------------------------------- 序号对齐
#
# 这个函数被 llm_clean 和 topics.name_topics 共用。它存在的唯一理由是
# 「逐条回退会撞车」这个 bug 在两处各犯过一次（见函数文档）。


class TestAlignByIndex:
    def test_aligned_by_index_when_all_valid_and_unique(self):
        items = [{"i": 2}, {"i": 0}, {"i": 1}]
        assert align_by_index(items, 3) == [2, 0, 1]

    def test_falls_back_to_position_when_index_out_of_range(self):
        """模型从 1 开始数序号 —— 整体退回位置对齐，而不是逐条混着来。"""
        items = [{"i": 1}, {"i": 2}, {"i": 3}]
        assert align_by_index(items, 3) == [0, 1, 2]

    def test_falls_back_to_position_when_duplicated(self):
        """序号重复说明模型没在认真编号，此时按序号对齐会丢掉一半结果。"""
        items = [{"i": 0}, {"i": 0}, {"i": 1}]
        assert align_by_index(items, 3) == [0, 1, 2]

    def test_falls_back_when_index_missing_or_not_int(self):
        assert align_by_index([{"i": "x"}, {"nope": 1}], 2) == [0, 1]

    def test_no_collision_when_partially_out_of_range(self):
        """回归：这是逐条回退会翻车的具体场景（位置 2 被越界项覆盖）。"""
        items = [{"i": 0}, {"i": 1}, {"i": 2}]
        assert align_by_index(items, 3) == [0, 1, 2]

    def test_extra_items_are_dropped(self):
        """返回比输入多 —— 多余的位置填 None，调用方跳过。"""
        items = [{"i": 0}, {"i": 1}, {"i": 2}]
        assert align_by_index(items, 2) == [0, 1, None]

    def test_non_dict_item_does_not_drop_later_items(self):
        """回归：中间夹一条垃圾时，**后面**的有效结果不能被一起丢掉。

        序号因垃圾条目而失效 → 整体退回位置对齐，第 3 条落到槽位 2。
        关键是槽位 2 有人接管（不是 None）—— 早先的 `break` 写法会把它丢掉。
        """
        assert align_by_index([{"i": 0}, "垃圾", {"i": 1}], 3) == [0, None, 2]

    def test_empty_input(self):
        assert align_by_index([], 3) == []
