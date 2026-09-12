"""LLM 字段映射测试。

这个功能的价值在于**泛化到别名表没收录的平台字段**，风险则在于**写错映射**——
往库里写错数据比留空更糟。所以这里重点钉住两类约束：

1. 只接受指向已知目标字段、且来自真实原始字段名的映射（模型编的一律丢掉）
2. 手写别名表优先级**高于**模型学到的（学到的只补空档，不能改写已有行为）
"""

from __future__ import annotations

import json

from Whochat.crawler.llm_map import (
    COMMENT_SPEC,
    CONTENT_SPEC,
    FieldMap,
    extend_aliases,
    _cache_key,
    known_keys,
    learn_field_map,
)
from Whochat.crawler.normalize import (
    COMMENT_ALIASES,
    CONTENT_ALIASES,
    normalize_comment,
    normalize_content,
)
from Whochat.pipeline.llm_client import LLMClient


def _client(stub):
    return LLMClient(base_url=stub.base_url, api_key="k", model="m")


def _reply(mapping: dict, unmapped: list[str] | None = None) -> str:
    return json.dumps({"mapping": mapping, "unmapped": unmapped or []})


# 一个"新平台"的原始记录：字段名全是我们别名表里没有的
NEW_PLATFORM_RAW = {
    "post_uid": "P123",
    "headline": "某品牌新机翻车",
    "rich_text": "刚买三天就发热严重",
    "poster_uid": "U456",
    "poster_nick": "路人甲",
    "thumb_up": 1200,
    "posted_at": 1757000000,
}


class TestLearning:
    def test_learns_mapping_and_persists_nothing_when_uncached(
        self, llm_stub, tmp_path
    ):
        llm_stub.push_chat(
            _reply(
                {
                    "post_uid": "content_id",
                    "headline": "title",
                    "rich_text": "body_text",
                    "poster_uid": "author_id",
                    "posted_at": "publish_time",
                },
                unmapped=["poster_nick", "thumb_up"],
            )
        )
        fm = learn_field_map(
            NEW_PLATFORM_RAW,
            CONTENT_SPEC,
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        assert fm.source == "llm"
        assert fm.mapping["post_uid"] == "content_id"
        assert fm.mapping["rich_text"] == "body_text"
        assert set(fm.unmapped) == {"poster_nick", "thumb_up"}

    def test_model_invented_raw_key_is_dropped(self, llm_stub, tmp_path):
        """模型报了一个输入里根本不存在的字段名 —— 必须丢掉。"""
        llm_stub.push_chat(
            _reply({"post_uid": "content_id", "根本不存在": "title"})
        )
        fm = learn_field_map(
            NEW_PLATFORM_RAW,
            CONTENT_SPEC,
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        assert "根本不存在" not in fm.mapping
        assert "post_uid" in fm.mapping

    def test_unknown_target_is_dropped(self, llm_stub, tmp_path):
        """模型映射到 schema 里没有的目标字段 —— 丢掉。"""
        llm_stub.push_chat(
            _reply({"post_uid": "content_id", "headline": "sentiment_v2"})
        )
        fm = learn_field_map(
            NEW_PLATFORM_RAW,
            CONTENT_SPEC,
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        assert "sentiment_v2" not in fm.mapping.values()

    def test_duplicate_target_keeps_first_only(self, llm_stub, tmp_path):
        """两个原始字段都被映射到 title：只认第一个，避免后者覆盖前者。"""
        llm_stub.push_chat(_reply({"headline": "title", "rich_text": "title"}))
        fm = learn_field_map(
            {"headline": "a", "rich_text": "b"},
            CONTENT_SPEC,
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        assert list(fm.mapping.values()).count("title") == 1
        assert fm.mapping["headline"] == "title"

    def test_known_keys_are_not_asked_about(self, llm_stub, tmp_path):
        """别名表已覆盖的字段不该浪费 token 再问一遍。"""
        llm_stub.push_chat(_reply({"nonsense_key": "title"}))
        learn_field_map(
            {"title": "某标题", "nonsense_key": "x"},
            CONTENT_SPEC,
            known_keys=known_keys(CONTENT_ALIASES),
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        sent = json.dumps(llm_stub.last_body, ensure_ascii=False)
        assert "nonsense_key" in sent
        # "title" 是已知字段，不该出现在待映射清单里
        assert '"  - title\\n"' not in sent and "  - title\n" not in sent

    def test_no_unknown_keys_skips_the_call(self, llm_stub, tmp_path):
        fm = learn_field_map(
            {"title": "标题", "content": "正文"},
            CONTENT_SPEC,
            known_keys=known_keys(CONTENT_ALIASES),
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        assert fm.source == "empty"
        assert llm_stub.calls == []

    def test_no_client_degrades_gracefully(self, tmp_path, monkeypatch):
        """没配 LLM 时返回空映射，不抛异常。"""
        monkeypatch.setattr("Whochat.pipeline.llm_client.get_client", lambda: None)
        fm = learn_field_map(
            NEW_PLATFORM_RAW, CONTENT_SPEC, client=None, cache_path=tmp_path / "m.json"
        )
        assert fm.mapping == {}
        assert "未配置" in fm.note


class TestCache:
    def test_second_call_hits_cache_without_llm(self, llm_stub, tmp_path):
        """字段名在一个平台内是稳定的 —— 学一次就该够，不能每条记录都问。"""
        cache = tmp_path / "m.json"
        llm_stub.push_chat(_reply({"post_uid": "content_id"}))
        first = learn_field_map(
            NEW_PLATFORM_RAW, CONTENT_SPEC, client=_client(llm_stub), cache_path=cache
        )
        calls_after_first = len(llm_stub.calls)

        second = learn_field_map(
            NEW_PLATFORM_RAW, CONTENT_SPEC, client=_client(llm_stub), cache_path=cache
        )

        assert second.source == "cache"
        assert second.mapping == first.mapping
        assert len(llm_stub.calls) == calls_after_first  # 没有新请求

    def test_cache_key_is_stable_across_processes(self, tmp_path):
        """回归：缓存键必须跨进程稳定。

        内置 `hash()` 对 str/tuple 有进程级随机盐，同样输入换个进程就是
        另一个键，缓存永远命不中 —— 而且不报错，只是每次都重新学一遍。
        （第二轮在 mock 种子上踩过同一个坑。）
        """
        import subprocess
        import sys

        code = (
            "from Whochat.crawler.llm_map import _cache_key, CONTENT_SPEC;"
            "print(_cache_key(['b','a','c'], CONTENT_SPEC))"
        )
        results = [
            subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            for _ in range(2)
        ]
        # ⚠️ 必须断言"确实拿到了值"：子进程里抛异常时 stdout 是空串，
        # 集合去重后只剩 {''}，`len(...) == 1` 照样成立 —— 测试会假绿。
        outs = {r.stdout.strip() for r in results}
        assert all(r.returncode == 0 for r in results), results[0].stderr[-500:]
        assert outs and "" not in outs, f"子进程没输出: {results[0].stderr[-500:]}"
        assert len(outs) == 1, f"跨进程不稳定: {outs}"

    def test_cache_key_separates_content_and_comment_specs(self):
        """回归：缓存键必须含目标 schema。

        内容与评论的原始字段名常常是同一批（`id` / `content` / `create_time`…），
        但两者目标 schema 不同。键里不含 spec 的话，先学的内容映射会被
        评论那轮命中、按 COMMENT_SPEC 一过滤全丢，返回空映射 ——
        评论字段永远学不到，而且全程不报错。
        """
        keys = ["id", "content", "create_time"]
        assert _cache_key(keys, CONTENT_SPEC) != _cache_key(keys, COMMENT_SPEC)

    def test_same_raw_keys_with_different_specs_learn_separately(
        self, llm_stub, tmp_path
    ):
        """同一批字段名，内容与评论各学各的，互不污染。"""
        cache = tmp_path / "m.json"
        sample = {"uid": "u1", "body": "文本", "ts": 1757000000}

        llm_stub.push_chat(_reply({"uid": "content_id", "ts": "publish_time"}))
        content_map = learn_field_map(
            sample, CONTENT_SPEC, client=_client(llm_stub), cache_path=cache
        )
        assert content_map.mapping["uid"] == "content_id"

        # 评论那轮必须**自己发一次请求**，而不是命中上面那份缓存
        llm_stub.push_chat(_reply({"uid": "comment_id", "body": "text"}))
        comment_map = learn_field_map(
            sample, COMMENT_SPEC, client=_client(llm_stub), cache_path=cache
        )

        assert comment_map.source == "llm"  # 不是 cache
        assert comment_map.mapping["uid"] == "comment_id"  # 没被内容映射带偏
        assert comment_map.mapping["body"] == "text"

    def test_corrupt_cache_file_is_ignored(self, llm_stub, tmp_path):
        cache = tmp_path / "m.json"
        cache.write_text("这不是 JSON", encoding="utf-8")
        llm_stub.push_chat(_reply({"post_uid": "content_id"}))
        fm = learn_field_map(
            NEW_PLATFORM_RAW, CONTENT_SPEC, client=_client(llm_stub), cache_path=cache
        )
        assert fm.source == "llm"


class TestExtendAliases:
    def test_learned_keys_are_appended_not_prepended(self):
        """手写别名表是人工核对过的，优先级必须高于模型猜的。

        追加到末尾 = 既有的候选先被检查；只有全都没命中才会轮到学到的。
        """
        fm = FieldMap(mapping={"post_uid": "content_id"})
        merged = extend_aliases(CONTENT_ALIASES, fm)
        original = CONTENT_ALIASES["content_id"]
        assert merged["content_id"][: len(original)] == original
        assert merged["content_id"][-1] == "post_uid"

    def test_does_not_mutate_the_original_table(self):
        fm = FieldMap(mapping={"post_uid": "content_id"})
        extend_aliases(CONTENT_ALIASES, fm)
        assert "post_uid" not in CONTENT_ALIASES["content_id"]

    def test_empty_map_returns_same_object(self):
        assert extend_aliases(CONTENT_ALIASES, FieldMap()) is CONTENT_ALIASES


class TestEndToEnd:
    def test_unknown_platform_fields_are_normalized_after_learning(
        self, llm_stub, tmp_path
    ):
        """这是整个功能的验收点：一个字段名全新的平台，学一次之后就能正常归一化。"""
        # 先确认：不学映射时，别名表确实认不出来
        bare = normalize_content(NEW_PLATFORM_RAW, "newplat")
        assert bare is None  # 连 content_id 都提取不到

        llm_stub.push_chat(
            _reply(
                {
                    "post_uid": "content_id",
                    "headline": "title",
                    "rich_text": "body_text",
                    "poster_uid": "author_id",
                    "posted_at": "publish_time",
                    "thumb_up": "like_count",
                }
            )
        )
        fm = learn_field_map(
            NEW_PLATFORM_RAW,
            CONTENT_SPEC,
            known_keys=known_keys(CONTENT_ALIASES),
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )

        rec = normalize_content(
            NEW_PLATFORM_RAW,
            "newplat",
            aliases=extend_aliases(CONTENT_ALIASES, fm),
        )
        assert rec is not None
        assert rec["content_id"] == "P123"
        assert rec["title"] == "某品牌新机翻车"
        assert rec["body_text"] == "刚买三天就发热严重"
        assert rec["like_count"] == 1200
        # 作者原始 ID 必须仍然被哈希脱敏
        assert rec["author_id"] != "U456"

    def test_alias_table_still_wins_over_learned_mapping(self, tmp_path):
        """学到的映射把 content_id 指向一个错的字段时，别名表要先命中。

        这里不调模型，手工构造一个"错误"的映射来验证优先级。
        """
        raw = {"note_id": "REAL", "wrong_key": "FAKE"}
        fm = FieldMap(mapping={"wrong_key": "content_id"})
        rec = normalize_content(
            raw, "xhs", aliases=extend_aliases(CONTENT_ALIASES, fm)
        )
        assert rec["content_id"] == "REAL"

    def test_manual_import_with_llm_map_learns_then_imports(self, llm_stub, tmp_path, monkeypatch):
        """验收：吃一份字段名完全陌生的 JSONL。

        不学映射时整份文件都会被跳过（一条都导不进来）；
        开 --llm-map 之后应当能正常导入。
        """
        import json as _json

        from Whochat.crawler.base import CrawlTask
        from Whochat.crawler.manual_source import ManualImportSource

        monkeypatch.setattr("Whochat.crawler.llm_map.CACHE_PATH", tmp_path / "map.json")
        # 打在源模块上：llm_map 是**函数内**延迟导入 get_client 的，
        # 模块命名空间里没有这个名字，打在它上面会 AttributeError。
        monkeypatch.setattr(
            "Whochat.pipeline.llm_client.get_client", lambda: _client(llm_stub)
        )

        rows = [
            {"post_uid": f"P{i}", "headline": f"标题{i}", "rich_text": f"正文{i}"}
            for i in range(3)
        ]
        path = tmp_path / "weird.jsonl"
        path.write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

        # 不学映射：一条都导不进来
        plain = list(
            ManualImportSource(path, "newplat").crawl(
                CrawlTask(platform="newplat", mode="keyword", target=None)
            )
        )
        assert plain == []

        # 学一次映射后：三条全部导入。
        # 两次请求对应两套 spec（内容 / 评论），目标字段必须落在各自的 spec 里，
        # 否则会被 learn_field_map 过滤掉。
        llm_stub.push_chat(
            _reply(
                {
                    "post_uid": "content_id",
                    "headline": "title",
                    "rich_text": "body_text",
                }
            )
        )
        llm_stub.push_chat(_reply({}))  # 评论 spec：这份文件里没有评论字段
        src = ManualImportSource(path, "newplat", llm_map=True)
        got = list(src.crawl(CrawlTask(platform="newplat", mode="keyword", target=None)))

        assert [r["content_id"] for r in got] == ["P0", "P1", "P2"]
        assert got[0]["title"] == "标题0"
        assert got[0]["body_text"] == "正文0"

        # 只学一次：3 条记录不该产生 3 轮学习请求（这里正好用了 2 次：内容+评论两套 spec）
        assert len(llm_stub.calls) == 2

    def test_comment_mapping_end_to_end(self, llm_stub, tmp_path):
        raw = {
            "reply_uid": "C1",
            "say": "客服根本不理人",
            "post_id": "P1",
            "thumbs": 33,
        }
        llm_stub.push_chat(
            _reply(
                {
                    "reply_uid": "comment_id",
                    "say": "text",
                    "post_id": "parent_comment_id",
                    "thumbs": "like_count",
                }
            )
        )
        fm = learn_field_map(
            raw,
            COMMENT_SPEC,
            known_keys=known_keys(COMMENT_ALIASES),
            client=_client(llm_stub),
            cache_path=tmp_path / "m.json",
        )
        rec = normalize_comment(
            raw, "newplat", content_id="P1", aliases=extend_aliases(COMMENT_ALIASES, fm)
        )
        assert rec["comment_id"] == "C1"
        assert rec["text"] == "客服根本不理人"
        assert rec["like_count"] == 33
