"""测试夹具。

数据库相关测试必须**每个用例一个全新的库**：Repository 的 engine 是模块级
懒加载单例，如果不重置，用例之间会互相看到对方写的数据（预警冷却、
analysis_version 排行等都会被污染），测试就变成顺序相关的了。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from Whochat.config import settings


# ---------------------------------------------------------------- LLM 桩服务
#
# 起一个真的 HTTP 服务而不是 mock `requests`：LLM 客户端最容易出错的地方
# 恰恰是 HTTP 层（端点探测、状态码分支、退避重试、JSON Mode 降级）。
# 把 post 换掉就等于把要测的东西测掉了。桩服务让 requests 真的走一遍 socket，
# 而且整个测试**不需要 API key、不需要外网**。


class LLMStubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._respond()

    def do_POST(self):  # noqa: N802
        self._respond()

    def _respond(self):
        stub: LLMStubServer = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw_body) if raw_body else None
        except Exception:
            payload = None

        stub.calls.append({"method": self.command, "path": self.path, "body": payload})

        if stub.responses:
            status, data = stub.responses.pop(0)
        else:
            status, data = stub.default
        body = (
            data.encode("utf-8")
            if isinstance(data, str)
            else json.dumps(data).encode("utf-8")
        )

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 别把测试输出刷屏
        pass


class LLMStubServer:
    """按脚本依次回包的假 OpenAI 服务。"""

    def __init__(self):
        self.calls: list[dict] = []
        # 队列：(状态码, 响应体)。队列空了以后一律回 default
        self.responses: list[tuple[int, object]] = []
        self.default: tuple[int, object] = (404, {"error": {"message": "not found"}})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), LLMStubHandler)
        self._httpd.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def push(self, status: int, data: object) -> None:
        """排入一个响应。"""
        self.responses.append((status, data))

    def push_chat(self, content: str, **usage) -> None:
        """排入一个成功的对话响应。"""
        body: dict = {"choices": [{"message": {"content": content}}]}
        if usage:
            body["usage"] = usage
        self.push(200, body)

    @property
    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    @property
    def last_body(self) -> dict | None:
        return self.calls[-1]["body"] if self.calls else None

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def llm_stub():
    s = LLMStubServer()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """清掉代理环境变量。

    生产代码保持 `requests` 默认的 trust_env=True —— 访问境外 API 确实需要
    走本机代理。但测试必须确保请求不会甩给 127.0.0.1:7897 那个真实的 Clash，
    否则测试就依赖本机环境，换台机器就莫名其妙地红。
    """
    for var in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
        "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """指向临时 SQLite 的全新 Repository。"""
    import Whochat.store.repository as R

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
