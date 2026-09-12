"""配置解析测试。

配置错了不会立刻报错，而是"看起来在跑、实际没生效"，
所以这两处都值得用回归用例钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from Whochat.config import ROOT, _abs_from_root, _env_bool, _resolve_db_url


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
