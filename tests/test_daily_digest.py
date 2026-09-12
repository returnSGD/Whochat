"""日报与本地控制台输出的回归测试。

两个 bug 都在"真跑一次"时才暴露：

1. `build_daily_digest` 的版本号默认写死 `"v1"`，而实际分析版本是
   `{情感后端}-v1`（默认 `lexicon-v1`）。日报查的版本不存在 → 情感分布
   永远空，日报里"声量 0 条"，等于日报的核心内容一直是坏的。
2. `notifier.flush()` 的 dry-run 分支会把带 emoji 的企微文案 `print` 到
   控制台。Windows 默认控制台是 GBK，编码不了 emoji，抛
   UnicodeEncodeError —— README 的头号命令 `python -m wochat.cli demo`
   因此跑到一半就崩（退出码 1），后面的词云与汇总全没跑。
"""

from __future__ import annotations

import io

from wochat.alert.notifier import build_daily_digest
from wochat.console import configure_console
from wochat.pipeline.runner import Pipeline
from wochat.store.models import utcnow

NEG = "发热严重，售后也联系不上，太失望了"
POS = "物流很快，包装完好，客服态度也不错"


class TestDailyDigestVersion:
    def test_empty_db_has_no_version(self, repo):
        assert repo.latest_analysis_version() is None

    def test_resolves_latest_version(self, repo):
        pipe = Pipeline(repo)
        assert repo.latest_analysis_version() is None
        repo.upsert_comments(
            [dict(comment_id="n1", content_id="c1", platform="xhs", text=NEG, publish_time=utcnow())]
        )
        pipe.analyze()
        assert repo.latest_analysis_version() == pipe.version

    def test_digest_uses_real_version_not_literal_v1(self, repo):
        """日报必须自动解析真实版本号，而不是查写死的 "v1"。"""
        repo.upsert_comments(
            [
                dict(comment_id="n1", content_id="c1", platform="xhs", text=NEG, publish_time=utcnow()),
                dict(comment_id="p1", content_id="c1", platform="xhs", text=POS, publish_time=utcnow()),
            ]
        )
        pipe = Pipeline(repo)
        pipe.analyze()
        assert pipe.version != "v1"

        digest = build_daily_digest(repo)
        # 修复前：dist 查的是不存在的 "v1"，这里会是 "声量**：0 条评论"
        assert "2 条评论" in digest, digest
        assert "负面 1" in digest, digest
        assert "正面 1" in digest, digest

    def test_explicit_version_still_honored(self, repo):
        repo.upsert_comments(
            [dict(comment_id="n1", content_id="c1", platform="xhs", text=NEG, publish_time=utcnow())]
        )
        Pipeline(repo, version="custom-v9").analyze()

        digest = build_daily_digest(repo, version="custom-v9")
        assert "1 条评论" in digest, digest


class TestConsoleEncoding:
    def test_gbk_console_no_longer_crashes_on_emoji(self):
        """模拟 Windows GBK 控制台：emoji 应降级成 '?'，而不是抛异常。"""
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="gbk")

        configure_console(stream)
        stream.write("🔴 舆情预警 📊")
        stream.flush()

        written = buf.getvalue()
        assert b"?" in written, "无法编码的 emoji 应被替换掉"
        assert "舆情预警".encode("gbk") in written, "GBK 可编码的中文必须原样保留"

    def test_missing_reconfigure_is_ignored(self):
        """pytest 的捕获流等没有 reconfigure 时不能报错。"""

        class Dummy:
            def write(self, _):
                pass

        configure_console(Dummy())  # 不抛异常即通过
