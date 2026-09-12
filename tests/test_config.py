"""配置解析测试。

配置错了不会立刻报错，而是"看起来在跑、实际没生效"，
所以这两处都值得用回归用例钉死。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from Whochat.config import (
    ROOT,
    LLMConfig,
    WebConfig,
    _abs_from_root,
    _env_bool,
    _env_bool_or_none,
    _resolve_db_url,
    mask_secret,
    save_llm_settings,
    settings,
)


class TestEnvBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "y", "on", "是"])
    def test_truthy(self, monkeypatch, raw):
        """回归：原来只认字符串 "true"，`WHOCHAT_LLM_ENABLED=1` 会被静默当成 False，
        用户以为开了 LLM 清洗，实际没开且没有任何提示。"""
        monkeypatch.setenv("WHOCHAT_TEST_FLAG", raw)
        assert _env_bool("WHOCHAT_TEST_FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("WHOCHAT_TEST_FLAG", raw)
        assert _env_bool("WHOCHAT_TEST_FLAG") is False

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("WHOCHAT_TEST_FLAG", raising=False)
        assert _env_bool("WHOCHAT_TEST_FLAG", True) is True
        assert _env_bool("WHOCHAT_TEST_FLAG") is False


class TestEnvBoolOrNone:
    """三态布尔：没设 → None（交给自动判断）。

    ⚠️ **空字符串必须当成"没设"**。`.env.example` 里 `WHOCHAT_LLM_ENABLED=`
    就是留空的，若把空值判成显式 False，那么每个从 `.env.example` 复制配置的人
    即使把 URL 和 api_key 都填好，LLM 也永远是关的 —— 界面无异常，只是安静地
    不生效。这会把「只填两个值就能用」的承诺彻底废掉。
    """

    @pytest.mark.parametrize("raw", ["", "   ", "\t"])
    def test_blank_means_unset(self, monkeypatch, raw):
        monkeypatch.setenv("WHOCHAT_TEST_FLAG", raw)
        assert _env_bool_or_none("WHOCHAT_TEST_FLAG") is None

    def test_missing_means_unset(self, monkeypatch):
        monkeypatch.delenv("WHOCHAT_TEST_FLAG", raising=False)
        assert _env_bool_or_none("WHOCHAT_TEST_FLAG") is None

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
    def test_explicit_true(self, monkeypatch, raw):
        monkeypatch.setenv("WHOCHAT_TEST_FLAG", raw)
        assert _env_bool_or_none("WHOCHAT_TEST_FLAG") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_explicit_false(self, monkeypatch, raw):
        monkeypatch.setenv("WHOCHAT_TEST_FLAG", raw)
        assert _env_bool_or_none("WHOCHAT_TEST_FLAG") is False


class TestLLMEnabled:
    """`is_enabled` = 显式开关优先，没设则"配齐了就自动开"。"""

    def test_auto_enables_when_configured(self, monkeypatch):
        monkeypatch.delenv("WHOCHAT_LLM_ENABLED", raising=False)
        cfg = LLMConfig()
        cfg.base_url = "https://api.deepseek.com/v1"
        cfg.api_key = "sk-x"
        cfg.enabled = None
        assert cfg.is_enabled is True

    def test_blank_enabled_behaves_as_auto(self, monkeypatch):
        """回归：`.env.example` 的 `WHOCHAT_LLM_ENABLED=` 不能把 LLM 关掉。"""
        monkeypatch.setenv("WHOCHAT_LLM_ENABLED", "")
        cfg = LLMConfig()
        cfg.base_url = "https://api.deepseek.com/v1"
        cfg.api_key = "sk-x"
        cfg.enabled = _env_bool_or_none("WHOCHAT_LLM_ENABLED")
        assert cfg.is_enabled is True

    def test_explicit_false_wins(self):
        cfg = LLMConfig()
        cfg.base_url = "https://api.deepseek.com/v1"
        cfg.api_key = "sk-x"
        cfg.enabled = False
        assert cfg.is_enabled is False

    def test_not_configured_stays_off(self):
        cfg = LLMConfig()
        cfg.base_url = ""
        cfg.api_key = ""
        cfg.enabled = None
        assert cfg.is_enabled is False
        assert cfg.configured is False


class TestMaskSecret:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("short", "***"),
            ("sk-1234567890abcd", "sk-1***abcd"),
        ],
    )
    def test_masks(self, raw, expected):
        assert mask_secret(raw) == expected

    def test_never_leaks_the_middle(self):
        secret = "sk-abcdefghijklmnop"
        masked = mask_secret(secret)
        assert "efghijkl" not in masked


class TestSaveLLMSettings:
    """操作端在页面上填的配置要落盘到 .env，并让当前进程立即生效。"""

    @pytest.fixture
    def sandbox(self, monkeypatch, tmp_path):
        """隔离：不碰真 .env，也不留下对环境变量的污染。"""
        monkeypatch.setattr("Whochat.config.ENV_PATH", tmp_path / ".env")
        monkeypatch.setattr(os, "environ", dict(os.environ))
        monkeypatch.setattr(settings.llm, "base_url", "")
        monkeypatch.setattr(settings.llm, "api_key", "")
        monkeypatch.setattr(settings.llm, "model", "")
        monkeypatch.setattr(settings.llm, "enabled", None)
        return tmp_path / ".env"

    def test_writes_all_keys(self, sandbox):
        save_llm_settings("https://api.deepseek.com/v1", "sk-secret", "deepseek-chat")
        text = sandbox.read_text(encoding="utf-8")
        assert "WHOCHAT_LLM_BASE_URL=https://api.deepseek.com/v1" in text
        assert "WHOCHAT_LLM_API_KEY=sk-secret" in text
        assert "WHOCHAT_LLM_MODEL=deepseek-chat" in text
        assert "WHOCHAT_LLM_ENABLED=true" in text

    def test_none_key_keeps_existing(self, sandbox):
        """页面上密钥框永远留空（不回显密钥），所以 None 必须表示"不改动"。"""
        settings.llm.api_key = "sk-existing"
        save_llm_settings("https://api.deepseek.com/v1", None, "")
        assert settings.llm.api_key == "sk-existing"
        text = sandbox.read_text(encoding="utf-8")
        assert "sk-existing" not in text  # 没重写密钥那一行

    def test_updates_existing_file_in_place(self, sandbox):
        """回归：必须改原行，不能追加重复键（后者会让 .env 里出现两个值）。"""
        sandbox.write_text(
            "WHOCHAT_LLM_BASE_URL=https://old.example/v1\n"
            "WHOCHAT_LLM_API_KEY=sk-old\n",
            encoding="utf-8",
        )
        save_llm_settings("https://new.example/v1", "sk-new", "")
        text = sandbox.read_text(encoding="utf-8")
        assert text.count("WHOCHAT_LLM_BASE_URL") == 1
        assert text.count("WHOCHAT_LLM_API_KEY") == 1
        assert "https://new.example/v1" in text
        assert "sk-old" not in text

    def test_takes_effect_in_current_process_without_restart(self, sandbox):
        """保存后当前进程立即生效 —— 否则界面会显示"保存了但测试连接仍失败"。"""
        save_llm_settings("https://api.deepseek.com/v1", "sk-new", "")
        assert settings.llm.base_url == "https://api.deepseek.com/v1"
        assert settings.llm.api_key == "sk-new"
        assert settings.llm.is_enabled is True

    def test_creates_env_file_when_missing(self, sandbox):
        assert not sandbox.exists()
        save_llm_settings("https://api.deepseek.com/v1", "sk-x", "")
        assert sandbox.exists()


class TestAbsFromRoot:
    def test_relative_is_anchored_to_project_root(self):
        assert _abs_from_root("data/db") == (ROOT / "data/db").resolve()

    def test_absolute_is_untouched(self, tmp_path):
        assert _abs_from_root(tmp_path) == tmp_path

    def test_does_not_depend_on_cwd(self, monkeypatch, tmp_path):
        """从别的目录启动时相对路径不能跟着 CWD 跑偏。"""
        before = _abs_from_root("data")
        monkeypatch.chdir(tmp_path)
        assert _abs_from_root("data") == before


class TestResolveDbUrl:
    def test_relative_sqlite_path_is_absolute(self, monkeypatch):
        """回归：.env.example 给的示例是相对路径，SQLAlchemy 会相对 CWD 解析，
        不是从项目根目录启动就报 unable to open database file。"""
        monkeypatch.setenv("WHOCHAT_DB_URL", "sqlite:///data/db/Whochat.db")
        url = _resolve_db_url()
        assert url.startswith("sqlite:///")
        path = Path(url[len("sqlite:///") :])
        assert path.is_absolute()
        assert path == (ROOT / "data/db/Whochat.db").resolve()

    def test_memory_db_is_untouched(self, monkeypatch):
        monkeypatch.setenv("WHOCHAT_DB_URL", "sqlite:///:memory:")
        assert _resolve_db_url() == "sqlite:///:memory:"

    def test_postgres_url_is_untouched(self, monkeypatch):
        monkeypatch.setenv("WHOCHAT_DB_URL", "postgresql+psycopg://u:p@localhost:5432/Whochat")
        assert _resolve_db_url() == "postgresql+psycopg://u:p@localhost:5432/Whochat"

    def test_default_is_absolute(self, monkeypatch):
        monkeypatch.delenv("WHOCHAT_DB_URL", raising=False)
        url = _resolve_db_url()
        assert Path(url[len("sqlite:///") :]).is_absolute()


# 浏览器（Chromium 系 / Firefox）的受限端口清单：这些端口在建连前就被拒绝，
# 报"无法访问此页面"；而 curl / requests 不检查该清单，服务端看起来完全正常。
# 正是这个差异让默认端口 6666 长期没被发现（见 WORKLOG §十四）。
BROWSER_BLOCKED_PORTS = {
    *range(1, 1024),  # 特权端口
    2049, 3659, 4045, 5060, 5061, 6000, 6566,
    *range(6665, 6670),  # 原 IRC 段 —— 6666 就在这里
    6697, 10080,
}


class TestDashboardPort:
    def test_default_port_is_not_browser_blocked(self, monkeypatch):
        """回归：默认端口必须能被浏览器打开。

        `curl` 拿到 200 只证明服务端在监听，**不证明人能打开** ——
        这就是 6666 一直没被发现的原因。"""
        monkeypatch.delenv("WHOCHAT_DASHBOARD_PORT", raising=False)
        assert WebConfig().port not in BROWSER_BLOCKED_PORTS

    def test_dashboard_url_matches_configured_port(self, monkeypatch):
        monkeypatch.setenv("WHOCHAT_DASHBOARD_PORT", "9001")
        assert WebConfig().dashboard_url == "http://localhost:9001"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("WHOCHAT_DASHBOARD_PORT", "9002")
        assert WebConfig().port == 9002

    def test_settings_wires_web_config(self):
        assert isinstance(settings.web, WebConfig)
