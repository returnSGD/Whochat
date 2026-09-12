"""看板渲染的回归测试。

之前"看板是否真的能渲染"只能靠人眼看，HTTP 200 只能证明 Streamlit 起得来
（它返回的是静态外壳，脚本要等会话连接才执行）。这里用 Streamlit 官方的
`AppTest` 真正跑一遍 app.py —— 6 个 tab 的查询函数、图表组装、筛选参数
全部会被执行，任何异常都会被捕获。

没装 streamlit 时整文件跳过（它属于 web 可选依赖）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="看板属于可选依赖 pip install -e .[web]")

from streamlit.testing.v1 import AppTest  # noqa: E402

from datetime import timedelta  # noqa: E402

from Whochat.pipeline.runner import Pipeline  # noqa: E402
from Whochat.store.models import utcnow  # noqa: E402

NEG = "发热严重，售后也联系不上，太失望了"
POS = "物流很快，包装完好，客服态度也不错"


def _seed(repo):
    repo.upsert_comments(
        [
            dict(comment_id="n1", content_id="c1", platform="xhs", text=NEG, publish_time=utcnow()),
            dict(comment_id="p1", content_id="c1", platform="xhs", text=POS, publish_time=utcnow()),
            dict(comment_id="n2", content_id="c1", platform="douyin", text=NEG, publish_time=utcnow()),
        ]
    )
    Pipeline(repo).analyze()

    # 传播页要真的有东西可画：内容带转发关系 + 多时间点快照
    repo.upsert_contents(
        [
            dict(content_id="c1", platform="xhs", title="原创内容", author_follower_count=100000),
            dict(
                content_id="c2",
                platform="douyin",
                title="转发内容",
                author_follower_count=500,
                parent_content_id="c1",
            ),
        ]
    )
    now = utcnow()
    repo.add_snapshots(
        [
            {"content_id": "c1", "snapshot_time": now - timedelta(hours=h), "like_count": v}
            for h, v in ((4, 10), (2, 60), (0, 100))
        ]
    )


DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "Whochat" / "web" / "app.py"


def test_dashboard_renders_all_tabs_without_exception(repo):
    _seed(repo)

    at = AppTest.from_file(str(DASHBOARD), default_timeout=120).run()

    assert list(at.exception) == [], [e.message for e in at.exception]
    assert len(at.tabs) == 6, "总览/负面/主题/词云/传播/预警 六个 tab 都应渲染"
