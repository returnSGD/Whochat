"""导航路由的回归测试。

背景（WORKLOG §十五）：`web/app.py` 里曾把 `url_path="dashboard"` 和
`default=True` 写在同一个 `st.Page` 上。Streamlit 的规定是「默认页的 url_path
恒为空字符串（根路径 /）」，实现就是 `"" if self._default else self._url_path`
—— 传进去的值被**静默忽略**，不报错、不警告。

后果：看板端实际注册在 `/`，`/dashboard` 这条路由压根不存在。访问它前端匹配不到，
弹出 "Page not found ... Running the app's main page."，然后回退到主页面 ——
而主页面恰好就是看板端自己，于是表现为"报错一下又刷新出来了"。

这个 bug 藏得住，是因为 **curl / HTTP 200 证明不了路由存在**：Streamlit 是 SPA，
任何路径都返回同一个静态外壳。这和 §十四 的浏览器禁用端口是同一类错误 ——
"服务端 200" 不等于 "这个地址真的能打开"。

所以这里钉两件事：
    1. `url_path` 与 `default=True` 不得同时出现（那个静默失效的组合）
    2. 实际注册的路由集合，必须与文档里写的入口一致
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="看板属于可选依赖 pip install -e .[web]")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PY = Path(__file__).resolve().parents[1] / "src" / "Whochat" / "web" / "app.py"

# 文档（README / app.py 顶部说明）对外承诺的两个入口。
# 看板端是默认页 → 根路径 ""；操作端 → "console"。
DOCUMENTED_URL_PATHS = {"", "console"}


def _page_calls() -> list[ast.Call]:
    """取出 app.py 里所有 `st.Page(...)` 调用。"""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Page"
    ]


class TestPageDeclaration:
    def test_there_are_page_calls(self):
        """守卫用例：上面的 AST 提取失效时，后面的断言会变成空转。"""
        assert len(_page_calls()) == 2

    def test_default_page_does_not_pass_url_path(self):
        """回归：`url_path` + `default=True` 是静默失效的组合。

        Streamlit 会把默认页的 url_path 强制置空，传进来的值被忽略且**不报错**。
        写的人以为自己注册了 /dashboard，实际只注册了 / —— 文档和现实就此分叉。
        """
        offenders = []
        for call in _page_calls():
            kwargs = {kw.arg for kw in call.keywords}
            if "url_path" in kwargs and "default" in kwargs:
                offenders.append(ast.unparse(call))
        assert not offenders, (
            "st.Page 同时传了 url_path 和 default=True —— url_path 会被静默忽略。"
            f"默认页的地址恒为 /，要改地址就不要让它当默认页。问题调用：{offenders}"
        )

    def test_url_path_has_no_slash(self):
        """Streamlit 不允许 url_path 含斜杠（会直接抛错，但早点报更清楚）。"""
        for call in _page_calls():
            for kw in call.keywords:
                if kw.arg == "url_path" and isinstance(kw.value, ast.Constant):
                    assert "/" not in str(kw.value.value)


class TestRegisteredRoutes:
    """真正跑一遍 app.py，看 Streamlit 实际注册了哪些路由。"""

    @pytest.fixture(scope="class")
    def registered_paths(self) -> set[str]:
        at = AppTest.from_file(str(APP_PY), default_timeout=60).run()
        assert not at.exception, f"app.py 启动即异常：{at.exception}"
        return {info.get("url_pathname") for info in at._registered_pages.values()}

    def test_routes_match_documented_entries(self, registered_paths):
        """文档承诺的入口必须真的存在 —— 这是 §十五 那个 bug 的核心。

        修复前 `url_pathname` 集合是 {'', 'console'}，而文档写的是
        /dashboard + /console：文档单方面承诺了一条不存在的路由。
        """
        assert registered_paths == DOCUMENTED_URL_PATHS

    def test_dashboard_is_reachable_at_root(self, registered_paths):
        """看板端是默认页，地址就是 /（不是 /dashboard）。"""
        assert "" in registered_paths
