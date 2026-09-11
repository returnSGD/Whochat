"""MediaCrawler 适配器 —— 主力采集后端。

设计要点：

1. **子进程调用，不 import**。MediaCrawler 是独立项目（有自己的配置体系、
   全局变量、浏览器生命周期），把它当库 import 会污染进程状态。子进程隔离更干净。
2. **不走代理**。见 config.crawl_env() —— 代理 IP 特征反而会触发平台风控。
3. **读 JSONL 输出再归一化**。MediaCrawler 输出的是平台原始 dict，
   字段名各平台不同，交给 normalize.py 统一。

⚠️ **许可证限制（重要）**：MediaCrawler 使用
   NON-COMMERCIAL LEARNING LICENSE 1.1，**明确禁止商业用途**。
   本适配器仅用于学习研究。若要商业交付，必须实现 OfficialAPISource
   （平台授权接口）替换本模块 —— 这正是 CrawlerSource 协议存在的意义。

输出目录结构（MediaCrawler tools/async_file_writer.py）：
    {save_data_path}/{platform}/jsonl/{crawler_type}_{item_type}_{date}.jsonl
    例：data/mc_out/xhs/jsonl/search_contents_2026-09-11.jsonl
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Iterator

from wochat.config import DATA_DIR, settings
from wochat.crawler.base import CrawlTask
from wochat.crawler.normalize import normalize_comment, normalize_content

# 我们的平台名 → MediaCrawler 的平台名
PLATFORM_MAP = {
    "douyin": "dy",
    "xhs": "xhs",
    "kuaishou": "ks",
    "bilibili": "bili",
    "weibo": "wb",
    "tieba": "tieba",
    "zhihu": "zhihu",
}

# 我们的模式 → MediaCrawler 的爬取类型
MODE_MAP = {
    "keyword": "search",
    "content_id": "detail",
    "creator": "creator",
}


class MediaCrawlerSource:
    """把 MediaCrawler 包成 CrawlerSource。"""

    name = "mediacrawler"

    def __init__(
        self,
        crawler_dir: Path | None = None,
        python_exe: str | None = None,
        login_type: str = "qrcode",
        cookies: str = "",
        headless: bool = False,
        output_dir: Path | None = None,
    ):
        self.crawler_dir = Path(crawler_dir or settings.crawl.mediacrawler_dir)
        # 默认用当前解释器；MediaCrawler 依赖多，建议单独建环境后指定
        self.python_exe = python_exe or os.getenv("WOCHAT_MC_PYTHON") or sys.executable
        self.login_type = login_type
        self.cookies = cookies
        # 登录需要扫码，默认必须有头
        self.headless = headless
        self.output_dir = Path(output_dir or (DATA_DIR / "mc_out"))

    # ------------------------------------------------------------

    def supports(self, platform: str) -> bool:
        return platform in PLATFORM_MAP

    def available(self) -> tuple[bool, str]:
        """自检：目录和入口文件在不在。给 CLI 报友好错误用。"""
        if not self.crawler_dir.exists():
            return False, f"MediaCrawler 目录不存在: {self.crawler_dir}"
        if not (self.crawler_dir / "main.py").exists():
            return False, f"未找到入口文件: {self.crawler_dir / 'main.py'}"
        return True, "ok"

    # ------------------------------------------------------------

    def build_command(self, task: CrawlTask) -> list[str]:
        mc_platform = PLATFORM_MAP[task.platform]
        mc_type = MODE_MAP[task.mode]

        cmd = [
            self.python_exe,
            "main.py",
            "--platform", mc_platform,
            "--lt", self.login_type,
            "--type", mc_type,
            "--save_data_option", "jsonl",
            "--save_data_path", str(self.output_dir),
            "--get_comment", "true",
            "--get_sub_comment", "true" if task.include_sub_comments else "false",
            "--headless", "true" if self.headless else "false",
            "--crawler_max_notes_count", str(max(1, task.max_items // 10)),
            "--max_comments_count_singlenotes", "100",
        ]

        if task.mode == "keyword":
            cmd += ["--keywords", task.target]
        elif task.mode == "content_id":
            cmd += ["--specified_id", task.target]
        elif task.mode == "creator":
            cmd += ["--creator_id", task.target]
        else:
            raise ValueError(f"不支持的模式: {task.mode}")

        if self.cookies:
            cmd += ["--cookies", self.cookies]

        return cmd

    def build_env(self) -> dict[str, str]:
        """构造子进程环境：**显式清掉代理**（方案文档 §2.3）。"""
        env = os.environ.copy()
        env.update(settings.proxy.crawl_env())
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    # ------------------------------------------------------------

    def crawl(self, task: CrawlTask) -> Iterator[dict]:
        """跑 MediaCrawler，产出归一化后的记录。"""
        ok, msg = self.available()
        if not ok:
            raise RuntimeError(msg)

        # 只统计当前任务的平台目录：MediaCrawler 输出目录是所有平台共用的，
        # 若不按平台过滤，上一轮别的平台残留的文件会被误判为本次产出，
        # 进而被 _read_outputs 贴上当前平台的标签（跨平台数据污染）。
        before = set(self._output_files(task.platform))
        cmd = self.build_command(task)

        print(f"[mediacrawler] 执行: {' '.join(cmd)}")
        print(f"[mediacrawler] 工作目录: {self.crawler_dir}")
        print("[mediacrawler] 注意：首次运行需要扫码登录，浏览器会弹出")

        proc = subprocess.run(
            cmd,
            cwd=str(self.crawler_dir),
            env=self.build_env(),
            # 不用 capture_output：让登录二维码/进度直接打在终端上
        )
        if proc.returncode != 0:
            print(f"[mediacrawler] 退出码 {proc.returncode}（可能被风控中断，已产出的数据仍会被读取）")

        new_files = set(self._output_files(task.platform)) - before
        if not new_files:
            # 日期跨天等情况，退化为读取本平台今天的全部文件
            new_files = set(self._output_files(task.platform))
            print("[mediacrawler] 未发现新文件，回退读取本平台全部输出文件")

        yield from self._read_outputs(new_files, task.platform, task.target)

    # ------------------------------------------------------------

    def _output_files(self, platform: str) -> list[Path]:
        """只返回指定平台的输出文件（MediaCrawler 各平台输出目录互不干扰）。"""
        if not self.output_dir.exists():
            return []
        mc_platform = PLATFORM_MAP.get(platform, platform)
        return sorted(self.output_dir.glob(f"{mc_platform}/jsonl/*.jsonl"))

    def _read_outputs(self, files: set[Path], platform: str, keyword: str) -> Iterator[dict]:
        # contents / comments 分开去重：两者别名表都接受 tid/generic id，
        # 共用一个 seen 会把「与主贴同 tid 的评论」当成重复内容丢掉。
        seen_contents: set[str] = set()
        seen_comments: set[str] = set()
        for path in sorted(files):
            item_type = "comments" if "comments" in path.name else "contents"
            for line in self._iter_jsonl(path):
                if item_type == "contents":
                    rec = normalize_content(line, platform, keyword)
                    if rec and rec["content_id"] not in seen_contents:
                        seen_contents.add(rec["content_id"])
                        yield rec
                else:
                    rec = normalize_comment(line, platform)
                    if rec and rec["comment_id"] not in seen_comments:
                        seen_comments.add(rec["comment_id"])
                        yield rec

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterator[dict]:
        """容错读 JSONL —— 爬虫被中断时最后一行常常是半截的。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # 最后一行被截断是常态，跳过而不是炸掉整个采集
                        print(f"[mediacrawler] 跳过损坏行 {path.name}:{lineno}")
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except OSError as e:
            print(f"[mediacrawler] 读取失败 {path}: {e}")


def register_mediacrawler(**kwargs) -> MediaCrawlerSource:
    from wochat.crawler.base import register

    return register(MediaCrawlerSource(**kwargs))
