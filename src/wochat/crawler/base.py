"""采集层接口抽象。

**这个文件的 20 行协议是整个项目最重要的解耦点**（方案文档 §2.5）。

为什么值得单独抽象：今天的实现是爬虫，但爬虫方案**不可能用于对外交付**
（平台授权才是合规路径）。有了这层协议，将来从「爬虫」换成「授权 API」
或「采购数据源」时，上层的清洗 / 分析 / 看板 / 预警**一行都不用改**。

成本几乎为零，收益极大 —— 现在就做。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Protocol, runtime_checkable

# ---------------------------------------------------------------- 任务


@dataclass
class CrawlTask:
    platform: str  # douyin / xhs / bilibili / weibo / kuaishou / zhihu / tieba
    mode: str  # keyword | content_id | creator
    target: str  # 关键词 / 内容ID / 主页ID
    max_items: int = 500
    include_sub_comments: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class CrawlResult:
    """一次采集的产出。contents 和 comments 分开，因为下游处理方式不同。"""

    contents: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.contents) + len(self.comments)


@runtime_checkable
class CrawlerSource(Protocol):
    """所有采集后端实现此协议。

    MVP 实现：MockSource（造数据验证链路）、MediaCrawlerSource（真实采集）
    将来实现：OfficialAPISource（平台授权）、ManualImportSource（人工导入兜底）
    """

    name: str

    def supports(self, platform: str) -> bool:
        """该后端是否支持这个平台。"""
        ...

    def crawl(self, task: CrawlTask) -> Iterator[dict]:
        """产出统一 schema 的记录（content 或 comment 混流）。"""
        ...


# ---------------------------------------------------------------- 工具

_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MENTION_RE = re.compile(r"@[\w一-鿿\-_]{1,30}")
_HTML_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_RE = re.compile(r"[​-‏‪-‮﻿]")


def anonymize_id(raw: str | int | None, salt: str = "wochat") -> str | None:
    """用户 ID 哈希脱敏。

    合规要求：不存储平台原始用户 ID。哈希保留可关联性（同一用户仍能聚合），
    但不可反查。salt 让不同部署之间无法互相碰撞。
    """
    if raw is None or raw == "":
        return None
    return hashlib.sha256(f"{salt}:{raw}".encode("utf-8")).hexdigest()[:24]


def clean_text_basic(text: str | None) -> str:
    """基础清洗 —— 无模型，毫秒级。

    这里只做「无损」清洗（去噪声，不改语义）。分词、去停用词等
    影响语义的操作放在 pipeline/rules.py，因为词云和主题建模的需求不同。
    """
    if not text:
        return ""
    t = _HTML_RE.sub(" ", text)
    t = _URL_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = _ZERO_WIDTH_RE.sub("", t)
    t = _WS_RE.sub(" ", t)
    return t.strip()


def to_iso(value) -> datetime | None:
    """把平台时间统一成 aware datetime。"""
    from wochat.store.repository import parse_time

    return parse_time(value)


def content_record(
    *,
    content_id: str,
    platform: str,
    search_keyword: str | None = None,
    author_raw_id: str | int | None = None,
    **kwargs,
) -> dict:
    """构造一条符合 RawContent 表结构的记录。

    注意 `raw_json` 必须带上 —— 采集不可逆，字段漏了就靠它兜底。

    `author_id` 和 comment_record 一样在这里完成脱敏：内容的发布者同样是
    自然人，原始平台用户 ID 绝不能落库。之前只有评论侧做了脱敏，
    内容侧直接把 raw 的 user_id 写进了库（合规要求见方案文档 §12）。
    """
    from wochat.store.models import utcnow

    # 防御性丢弃：调用方若图省事传了 author_id=，它会在下面的 **kwargs 展开里
    # **覆盖**掉我们刚脱敏的值，平台原始用户 ID 就原样落库了（这正是之前的 bug）。
    # 这里直接扔掉，保证"原始 ID 永不落库"是无条件成立的，而不是靠调用方自觉。
    kwargs.pop("author_id", None)
    kwargs.pop("author_name_raw", None)

    return {
        "content_id": str(content_id),
        "platform": platform,
        "search_keyword": search_keyword,
        "author_id": anonymize_id(author_raw_id),
        "crawl_time": utcnow(),
        "raw_json": kwargs.pop("raw_json", None),
        **kwargs,
    }


def comment_record(
    *,
    comment_id: str,
    content_id: str,
    platform: str,
    text: str,
    author_raw_id: str | int | None = None,
    **kwargs,
) -> dict:
    """构造一条符合 Comment 表结构的记录。

    `author_id` 在这里就完成脱敏，确保原始 ID 永远不会落库。
    """
    from wochat.store.models import utcnow

    # 和 content_record 一样防御性丢弃：调用方若传了 author_id=，
    # 会在下面的 **kwargs 展开里覆盖刚脱敏的值，原始 ID 就落库了。
    kwargs.pop("author_id", None)
    kwargs.pop("author_name_raw", None)

    return {
        "comment_id": str(comment_id),
        "content_id": str(content_id),
        "platform": platform,
        "text": clean_text_basic(text),
        "author_id": anonymize_id(author_raw_id),
        "crawl_time": utcnow(),
        "raw_json": kwargs.pop("raw_json", None),
        **kwargs,
    }


# ---------------------------------------------------------------- 注册表

_REGISTRY: dict[str, CrawlerSource] = {}


def register(source: CrawlerSource) -> CrawlerSource:
    _REGISTRY[source.name] = source
    return source


def get_source(name: str) -> CrawlerSource:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的采集后端: {name}。已注册: {list(_REGISTRY)}")
    return _REGISTRY[name]


def list_sources() -> list[str]:
    return list(_REGISTRY)


def resolve_source(platform: str, preferred: str | None = None) -> CrawlerSource:
    """挑一个能处理该平台的后端。优先用 preferred，否则按注册顺序找。"""
    if preferred:
        src = get_source(preferred)
        if not src.supports(platform):
            raise ValueError(f"{preferred} 不支持平台 {platform}")
        return src
    for src in _REGISTRY.values():
        if src.supports(platform):
            return src
    raise KeyError(f"没有采集后端支持平台 {platform}")
