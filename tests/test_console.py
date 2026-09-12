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
