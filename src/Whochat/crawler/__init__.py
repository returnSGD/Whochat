"""采集后端的选择与注册。

⚠️ 为什么不能省掉这个模块：`scheduler.jobs.job_crawl` 是无人值守进程，
它不会像 CLI 那样显式调用 `register_mediacrawler()`。而 `resolve_source()`
查的是一张进程内的注册表 —— 空表意味着每个任务都命中
`KeyError: 未注册的采集后端`，长期采集**从未真正跑起来**，却只表现为
每个任务失败、日志里一行报错。这里按配置统一构建并注册，调度器只依赖它。
"""

from __future__ import annotations

from Whochat.config import settings
from Whochat.crawler.base import CrawlerSource


def build_source(name: str | None = None) -> CrawlerSource:
    """按名字（默认取配置）构建并注册一个采集后端。"""
    backend = (name or settings.crawl.source or "mediacrawler").lower()

    if backend == "mock":
        from Whochat.crawler.mock_source import register_mock

        return register_mock()

    if backend == "mediacrawler":
        from Whochat.crawler.mediacrawler_source import register_mediacrawler

        return register_mediacrawler(
            login_type=settings.crawl.login_type,
            headless=settings.crawl.headless,
            cookies=settings.crawl.cookies,
        )

    raise ValueError(f"未知采集后端: {backend}（可选 mediacrawler / mock）")


def source_for(platform: str) -> CrawlerSource:
    """给某个平台挑后端。`mock` 平台始终走 MockSource（自检/离线用）。"""
    if platform == "mock":
        return build_source("mock")
    return build_source(settings.crawl.source)
