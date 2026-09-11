"""全局配置。

所有可调参数集中在这里，通过项目根目录的 .env 覆盖。

代理策略（重要，见方案文档 §2.3）：
    下载类流量（GitHub / HuggingFace / pip / Ollama）走系统代理 7897；
    采集类流量 **直连**，不走代理 —— 代理 IP 特征反而会触发平台风控，
    且节点出口地域跳变会与账号登录态冲突。
    因此本项目把两者分成两组独立配置，互不影响。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------- 路径

ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")


def _abs_from_root(value: str | Path) -> Path:
    """相对路径一律锚定到**项目根目录**，而不是当前工作目录。

    从 IDE、计划任务或别的目录启动时 CWD 不是项目根，相对路径会解析到
    别处 —— 数据目录被意外创建在奇怪的位置，或者直接找不到。"""
    p = Path(value)
    return p if p.is_absolute() else (ROOT / p).resolve()


def _env_bool(name: str, default: bool = False) -> bool:
    """解析布尔环境变量。

    只认字符串 "true" 会把 `1` / `yes` / `on` 静默当成 False ——
    用户以为开了 LLM 清洗，实际没开，还没有任何提示。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "是")


DATA_DIR = _abs_from_root(os.getenv("WOCHAT_DATA_DIR") or ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
DB_DIR = DATA_DIR / "db"
EXPORT_DIR = DATA_DIR / "exports"
DICT_DIR = _abs_from_root(os.getenv("WOCHAT_DICT_DIR") or ROOT / "dicts")
VENDOR_DIR = ROOT / "vendor"

for _d in (DATA_DIR, RAW_DIR, DB_DIR, EXPORT_DIR, DICT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 数据源


@dataclass
class ProxyConfig:
    """代理配置。分成两组，用途完全不同，不要混用。"""

    # 下载用：GitHub / HuggingFace / pip / Ollama
    download: str = os.getenv("WOCHAT_DOWNLOAD_PROXY", "http://127.0.0.1:7897")
    # 采集用：留空表示直连（推荐）
    crawl: str = os.getenv("WOCHAT_CRAWL_PROXY", "")

    def download_env(self) -> dict[str, str]:
        """返回给 requests / huggingface_hub 等下载工具用的环境变量。"""
        if not self.download:
            return {}
        return {
            "HTTP_PROXY": self.download,
            "HTTPS_PROXY": self.download,
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }

    def crawl_env(self) -> dict[str, str]:
        """返回给采集进程用的环境变量 —— 显式清掉代理。"""
        if self.crawl:
            return {"HTTP_PROXY": self.crawl, "HTTPS_PROXY": self.crawl}
        return {
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "NO_PROXY": "*",
        }


@dataclass
class CrawlConfig:
    """采集参数。限速是硬要求，不要为提速把它调小。"""

    # 每个请求之间的最小间隔（秒）。低于 3 秒会显著提高被封概率。
    min_interval: float = float(os.getenv("WOCHAT_CRAWL_INTERVAL", "4.0"))
    # 单次任务最多抓多少条
    max_items: int = int(os.getenv("WOCHAT_CRAWL_MAX_ITEMS", "500"))
    # 是否抓二级评论
    include_sub_comments: bool = _env_bool("WOCHAT_CRAWL_SUB_COMMENTS", True)
    # 失败重试次数（指数退避）
    max_retries: int = int(os.getenv("WOCHAT_CRAWL_RETRIES", "3"))
    # MediaCrawler 所在目录
    mediacrawler_dir: Path = VENDOR_DIR / "MediaCrawler"


@dataclass
class LLMConfig:
    """本地模型清洗配置（Ollama）。"""

    enabled: bool = _env_bool("WOCHAT_LLM_ENABLED")
    base_url: str = os.getenv("WOCHAT_LLM_BASE_URL", "http://127.0.0.1:11434")
    model: str = os.getenv("WOCHAT_LLM_MODEL", "qwen2.5:14b-instruct-q4_K_M")
    # 清洗是确定性任务，温度必须为 0
    temperature: float = 0.0
    timeout: int = int(os.getenv("WOCHAT_LLM_TIMEOUT", "120"))
    # 批量清洗时每批多少条
    batch_size: int = int(os.getenv("WOCHAT_LLM_BATCH", "1"))


@dataclass
class SentimentConfig:
    """情感分析配置。默认走词典法（零依赖），装了 transformers 可切模型。"""

    backend: str = os.getenv("WOCHAT_SENTIMENT_BACKEND", "lexicon")  # lexicon | transformer
    model_name: str = os.getenv(
        "WOCHAT_SENTIMENT_MODEL",
        "IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment",
    )
    # 判定阈值：分数绝对值低于此值算中性
    neutral_band: float = float(os.getenv("WOCHAT_SENTIMENT_NEUTRAL_BAND", "0.15"))
    device: str = os.getenv("WOCHAT_SENTIMENT_DEVICE", "cpu")


@dataclass
class AlertConfig:
    """预警配置。

    企微机器人硬限制（方案文档 §6.2）：
        20 条/分钟/机器人，单条 body ≤ 2048 字节，Markdown 不支持 @人。
    因此推送前必须做窗口聚合 + 分级 + 冷却，否则爆发时会被限流打爆。
    """

    # 企微机器人 webhook。留空则只记录不推送（dry-run）。
    wecom_webhook: str = os.getenv("WOCHAT_WECOM_WEBHOOK", "")
    # 同一事件冷却秒数
    cooldown_seconds: int = int(os.getenv("WOCHAT_ALERT_COOLDOWN", "300"))
    # 聚合窗口秒数：窗口内的告警合并成一条推送
    agg_window_seconds: int = int(os.getenv("WOCHAT_ALERT_AGG_WINDOW", "600"))
    # 每分钟最多推送条数（企微限制 20，留安全余量）
    max_per_minute: int = int(os.getenv("WOCHAT_ALERT_MAX_PER_MINUTE", "15"))
    # 单条消息字节上限
    max_body_bytes: int = 2048
    # 哪些等级实时推送，其余进日报
    realtime_levels: tuple[str, ...] = ("red",)
    # 发送失败的告警会保持 pending 自动重试，超过这个时长仍未成功才放弃
    # （标记 failed）。避免配错 webhook 时无限重试刷日志。
    retry_max_age_seconds: int = int(os.getenv("WOCHAT_ALERT_RETRY_MAX_AGE", "21600"))


def _resolve_db_url() -> str:
    """数据库连接串。相对 sqlite 路径锚定到项目根目录。

    `.env.example` 里给的示例是 `sqlite:///data/db/wochat.db`，而 SQLAlchemy
    会相对**当前工作目录**解析它 —— 只要不是从项目根目录启动就会报
    `unable to open database file`。这里统一转成绝对路径。
    """
    raw = os.getenv("WOCHAT_DB_URL")
    if not raw:
        return f"sqlite:///{DB_DIR / 'wochat.db'}"

    prefix = "sqlite:///"
    if raw.startswith(prefix):
        path = raw[len(prefix) :]
        # sqlite:///:memory: 这类特殊值不要动
        if path and path != ":memory:" and not Path(path).is_absolute():
            resolved = (ROOT / path).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{resolved}"
    return raw


@dataclass
class StoreConfig:
    """存储配置。MVP 用 SQLite；DDL 按 PostgreSQL 写，迁移只需改连接串。"""

    url: str = field(default_factory=_resolve_db_url)
    echo: bool = _env_bool("WOCHAT_DB_ECHO")


@dataclass
class Settings:
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    store: StoreConfig = field(default_factory=StoreConfig)


settings = Settings()
