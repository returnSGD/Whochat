"""测试夹具。

数据库相关测试必须**每个用例一个全新的库**：Repository 的 engine 是模块级
懒加载单例，如果不重置，用例之间会互相看到对方写的数据（预警冷却、
analysis_version 排行等都会被污染），测试就变成顺序相关的了。
"""

from __future__ import annotations

import pytest

from wochat.config import settings


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """指向临时 SQLite 的全新 Repository。"""
    import wochat.store.repository as R

    monkeypatch.setattr(R, "_engine", None)
    monkeypatch.setattr(R, "_SessionFactory", None)
    monkeypatch.setattr(settings.store, "url", f"sqlite:///{tmp_path / 'test.db'}")

    R.init_db()
    r = R.Repository()
    try:
        yield r
    finally:
        r.close()
        monkeypatch.setattr(R, "_engine", None)
        monkeypatch.setattr(R, "_SessionFactory", None)
