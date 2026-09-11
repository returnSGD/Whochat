"""人工导入后端 —— 应急兜底。

用途：
    1. 爬虫被风控完全封死时，手动导出数据继续分析
    2. 导入历史存档 / 第三方提供的数据集
    3. 把 MediaCrawler 已产出的 JSONL 直接吃进来（不必重跑采集）

这再次印证了 `CrawlerSource` 协议的价值：换数据来源，上层零改动。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Iterator

from wochat.crawler.base import CrawlTask
from wochat.crawler.normalize import (
    COMMENT_ALIASES,
    CONTENT_ALIASES,
    normalize_comment,
    normalize_content,
)


def _has_any(record: dict, keys: list[str]) -> bool:
    """别名表里任意一个字段有非空值即算命中。"""
    return any(k in record and record[k] not in (None, "", [], {}) for k in keys)


def _looks_like_comment(record: dict) -> bool:
    """只有同时具备「评论 ID」和「正文」才可能是评论。

    不能只看字面量 "comment_id" —— normalize.py 的别名表里 cid/id/rpid/tid
    同样表示评论 ID，`id`/`content` 又同时出现在内容与评论两套别名里，
    所以这里先按评论必需字段试探，归一化失败再退回内容（见 crawl）。
    带标题字段的记录按内容处理，避免把内容误当评论。
    """
    return (
        _has_any(record, COMMENT_ALIASES["comment_id"])
        and _has_any(record, COMMENT_ALIASES["text"])
        and not _has_any(record, CONTENT_ALIASES["title"])
    )


def _read_text(path: Path) -> str:
    """宽容解码：utf-8-sig → gb18030 → latin-1。

    中文 Windows 上 Excel 默认把 CSV 导出成 GBK/ANSI，写死 utf-8 会抛
    UnicodeDecodeError 直接中断整条采集。gb18030 兼容 GBK/GB2312；
    latin-1 永不失败，作为最后兜底保证不因编码丢数据。
    """
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")



class ManualImportSource:
    """从本地文件导入。支持 .jsonl / .json / .csv。"""

    name = "manual"

    def __init__(self, path: str | Path, platform: str | None = None):
        self.path = Path(path)
        self.platform = platform

    def supports(self, platform: str) -> bool:
        # 人工导入不挑平台
        return True

    def crawl(self, task: CrawlTask) -> Iterator[dict]:
        platform = self.platform or task.platform
        for line in self._records():
            # 靠字段特征判断是内容还是评论，而不是靠文件名 ——
            # 人工整理的数据文件名往往不规整。
            # 先试评论：评论比内容多了「归属内容 ID」这一硬性要求，
            # normalize_comment 失败（如 {'id','content'} 没有 note_id）时
            # 再退回内容，绝不让本可归一化的记录被静默丢弃。
            rec = normalize_comment(line, platform) if _looks_like_comment(line) else None
            if rec is None:
                rec = normalize_content(line, platform, task.target)

            if rec:
                yield rec
            else:
                # 两套归一化都失败 —— 人工导入必须留下线索，不能静默丢数据
                print(f"[manual] 跳过无法归一化的记录，字段: {sorted(line)[:8]}")

    def _records(self) -> Iterator[dict]:
        suffix = self.path.suffix.lower()

        if suffix == ".jsonl":
            # 走宽容解码，兼容 GBK 存档；splitlines 等价于逐行读
            for lineno, raw in enumerate(_read_text(self.path).splitlines(), 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[manual] 跳过损坏行 {lineno}")
                    continue
                if isinstance(obj, dict):
                    yield obj

        elif suffix == ".json":
            data = json.loads(_read_text(self.path))
            # 支持 [{...}, ...] 和 {"contents": [...], "comments": [...]} 两种结构
            if isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict):
                        yield obj
            elif isinstance(data, dict):
                for key in ("contents", "comments", "data", "items"):
                    if isinstance(data.get(key), list):
                        for obj in data[key]:
                            if isinstance(obj, dict):
                                yield obj

        elif suffix == ".csv":
            # newline="" 交给 csv 模块处理引号内的换行，避免行被错误拆分
            yield from csv.DictReader(io.StringIO(_read_text(self.path), newline=""))

        else:
            raise ValueError(f"不支持的文件类型: {suffix}（支持 .jsonl / .json / .csv）")


def register_manual(path: str | Path, platform: str | None = None) -> ManualImportSource:
    from wochat.crawler.base import register

    return register(ManualImportSource(path, platform))
