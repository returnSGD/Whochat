"""操作端渲染测试。

操作端会写库、触发任务，渲染路径里任何异常都可能让人误以为"操作已生效"，
所以至少要保证：有数据/有规则时，5 个分层 tab 全部能正常渲染。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="操作端属于可选依赖 pip install -e .[web]")

from streamlit.testing.v1 import AppTest  # noqa: E402

from Whochat.alert.rules_engine import seed_default_rules  # noqa: E402

CONSOLE = Path(__file__).resolve().parents[1] / "src" / "Whochat" / "web" / "console.py"


def test_console_renders_all_layer_tabs(repo):
    seed_default_rules(repo)

    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()

    assert list(at.exception) == [], [e.message for e in at.exception]
    assert len(at.tabs) == 5, "L1~L5 五个分层 tab 都应渲染"
    # 规则编辑器应当出现（默认规则已 seed）
    assert any("neg_surge" in (e.label or "") for e in at.expander), [
        e.label for e in at.expander
    ]


def test_llm_config_is_editable_from_the_console(repo):
    """L2 的 LLM 配置必须能在页面上填完。

    回归：早先这里只渲染一句"去 .env 里填两个值" —— 而操作端的定位就是
    "可写"，让人去手改文件与这个定位自相矛盾。
    """
    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()
    assert list(at.exception) == [], [e.message for e in at.exception]

    labels = [t.label for t in at.text_input]
    assert any("base_url" in l for l in labels), "缺少请求地址输入框"
    assert any("API Key" in l for l in labels), "缺少 API Key 输入框"
    assert any("模型名" in l for l in labels), "缺少模型名输入框"

    buttons = [b.label for b in at.button]
    assert "保存并测试连接" in buttons


def test_llm_api_key_field_is_masked(repo):
    """密钥框必须做成 password —— 不能让 key 明文出现在页面上。"""
    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()

    key_field = next((t for t in at.text_input if "API Key" in t.label), None)
    assert key_field is not None
    # proto.type: 0 = default, 1 = password
    assert key_field.proto.type == 1

    url_field = next(t for t in at.text_input if "base_url" in t.label)
    assert url_field.proto.type == 0


def test_saving_from_the_console_writes_env(repo, monkeypatch, tmp_path):
    """验收：在页面上填地址与 key → 点保存 → `.env` 真的写出来了。

    这条才是这个功能的验收点 —— 前面几条只证明"输入框存在"。
    """
    from Whochat.config import settings

    env_file = tmp_path / ".env"
    monkeypatch.setattr("Whochat.config.ENV_PATH", env_file)
    monkeypatch.setattr(settings.llm, "base_url", "")
    monkeypatch.setattr(settings.llm, "api_key", "")
    monkeypatch.setattr(settings.llm, "model", "")
    monkeypatch.setattr(settings.llm, "enabled", None)

    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()

    for t in at.text_input:
        if "base_url" in t.label:
            t.set_value("https://api.deepseek.com/v1")
        elif "API Key" in t.label:
            t.set_value("sk-from-ui")
        elif "模型名" in t.label:
            t.set_value("deepseek-chat")

    next(b for b in at.button if b.label == "保存").click().run()

    assert list(at.exception) == [], [e.message for e in at.exception]
    assert env_file.exists(), "保存按钮没有写出 .env"
    text = env_file.read_text(encoding="utf-8")
    assert "WHOCHAT_LLM_BASE_URL=https://api.deepseek.com/v1" in text
    assert "WHOCHAT_LLM_API_KEY=sk-from-ui" in text
    assert "WHOCHAT_LLM_MODEL=deepseek-chat" in text

    # 保存后当前进程立即生效，不必重启看板
    assert settings.llm.base_url == "https://api.deepseek.com/v1"
    assert settings.llm.is_enabled is True


def test_empty_base_url_is_rejected(repo, monkeypatch, tmp_path):
    """地址为空时报错而不是写一个空配置进去。"""
    from Whochat.config import settings

    env_file = tmp_path / ".env"
    monkeypatch.setattr("Whochat.config.ENV_PATH", env_file)

    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()
    next(b for b in at.button if b.label == "保存").click().run()

    assert list(at.exception) == [], [e.message for e in at.exception]
    assert not env_file.exists(), "地址为空时不该写出配置"
    assert any("请求地址不能为空" in e.value for e in at.error)


def test_llm_key_is_never_prefilled(repo, monkeypatch):
    """已配置时输入框也不能回显密钥 —— 空值 + placeholder 提示"留空则不修改"。"""
    from Whochat.config import settings

    monkeypatch.setattr(settings.llm, "api_key", "sk-super-secret-value")
    at = AppTest.from_file(str(CONSOLE), default_timeout=120).run()

    key_field = next(t for t in at.text_input if "API Key" in t.label)
    assert key_field.value == ""
    # 界面上只允许出现掩码形态
    assert "sk-super-secret-value" not in str(key_field.placeholder)
    assert "***" in key_field.placeholder
