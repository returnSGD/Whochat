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


def _env_bool_or_none(name: str) -> bool | None:
    """三态布尔：没设返回 None，用于「显式开关优先，否则自动判断」。

    ⚠️ **空字符串必须当成"没设"**，不能当成显式 False。

    这一点很要命：`.env.example` 里写的是 `WHOCHAT_LLM_ENABLED=`（留空，
    方便用户填）。若把空值判成 False，那么每一个从 `.env.example` 复制配置的
    人，即使把 URL 和 api_key 都填好了，LLM 也永远是关的 —— 而界面上不会有
    任何异常，只会安静地不生效。这正好把「只填两个值就能用」的承诺废掉。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return _env_bool(name)


ENV_PATH = ROOT / ".env"


def update_env(updates: dict[str, str]) -> Path:
    """把配置写进 `.env`，并让**当前进程立即生效**。

    为什么要有这个函数：操作端（`/console`）的定位是"可写"，却在 LLM 配置上
    只显示一句"去 .env 里填两个值" —— 让用户去手改文件，这和页面定位自相矛盾。

    为什么写 `.env` 而不是另起一套存储：它是本项目唯一的配置来源
    （`load_dotenv` 在模块导入时读它），而且已在 `.gitignore` 里。

    ⚠️ 改完必须手动同步 `os.environ`：`Settings` 是**导入时的快照**，
    不刷新的话"保存成功但测试连接仍失败"，表现得像保存没生效。
    """
    from dotenv import set_key

    ENV_PATH.touch(exist_ok=True)
    for key, value in updates.items():
        # quote_mode="never"：值本身不含空格/井号时不需要引号，写出来更可读
        set_key(str(ENV_PATH), key, value, quote_mode="never")
        os.environ[key] = value
    return ENV_PATH


def save_llm_settings(base_url: str, api_key: str | None, model: str) -> Path:
    """保存 LLM 配置。返回写入的文件路径。

    `api_key=None` 表示"不改动"（页面上留空 = 沿用已保存的），
    这是必要的：密钥不该回显到页面上，所以输入框永远是空的，
    无法用"空字符串"区分"没填"和"想清空"。
    """
    llm = settings.llm
    llm.base_url = base_url.strip()
    llm.model = model.strip()
    if api_key is not None:
        llm.api_key = api_key.strip()

    updates = {
        "WHOCHAT_LLM_BASE_URL": llm.base_url,
        "WHOCHAT_LLM_MODEL": llm.model,
        # 把开关写成显式 true：在页面上填地址和密钥这个动作，本身就表示
        # "我要用 LLM"。不写的话，若 `.env` 里原本是 `false`（或留空但被旧版
        # 判成 false），就会出现"界面里配好了、重启后又变回关的"这种
        # 最难排查的不一致。
        "WHOCHAT_LLM_ENABLED": "true",
    }
    if api_key is not None:
        updates["WHOCHAT_LLM_API_KEY"] = llm.api_key
    llm.enabled = True
    return update_env(updates)


def mask_secret(value: str) -> str:
    """把密钥显示成 `sk-1***abcd` 这种形态，用于界面回显。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


DATA_DIR = _abs_from_root(os.getenv("WHOCHAT_DATA_DIR") or ROOT / "data")
RAW_DIR = DATA_DIR / "raw"
DB_DIR = DATA_DIR / "db"
EXPORT_DIR = DATA_DIR / "exports"
DICT_DIR = _abs_from_root(os.getenv("WHOCHAT_DICT_DIR") or ROOT / "dicts")
VENDOR_DIR = ROOT / "vendor"

for _d in (DATA_DIR, RAW_DIR, DB_DIR, EXPORT_DIR, DICT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 数据源


@dataclass
class ProxyConfig:
    """代理配置。分成两组，用途完全不同，不要混用。"""

    # 下载用：GitHub / HuggingFace / pip / Ollama
    download: str = os.getenv("WHOCHAT_DOWNLOAD_PROXY", "http://127.0.0.1:7897")
    # 采集用：留空表示直连（推荐）
    crawl: str = os.getenv("WHOCHAT_CRAWL_PROXY", "")

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
    min_interval: float = float(os.getenv("WHOCHAT_CRAWL_INTERVAL", "4.0"))
    # 单次任务最多抓多少条
    max_items: int = int(os.getenv("WHOCHAT_CRAWL_MAX_ITEMS", "500"))
    # 是否抓二级评论
    include_sub_comments: bool = _env_bool("WHOCHAT_CRAWL_SUB_COMMENTS", True)
    # 失败重试次数（指数退避）
    max_retries: int = int(os.getenv("WHOCHAT_CRAWL_RETRIES", "3"))
    # MediaCrawler 所在目录
    mediacrawler_dir: Path = VENDOR_DIR / "MediaCrawler"


@dataclass
class LLMConfig:
    """LLM 分析配置 —— 任何 **OpenAI 兼容**接口都能用。

    最小配置就是两个值，模型名可以不填（会自动挑）：

        WHOCHAT_LLM_BASE_URL=https://api.deepseek.com/v1
        WHOCHAT_LLM_API_KEY=sk-xxxxxxxx

    为什么走 OpenAI 兼容协议而不是绑死某一家：这套协议现在是事实标准，
    DeepSeek / 通义千问 / Moonshot / 智谱 / OpenAI / 以及本地 Ollama 的
    `/v1` 端点全都实现了它。换供应商只改 base_url，代码一行不动 ——
    和 `CrawlerSource` 协议是同一个思路（方案文档 ADR#1 的教训）。

    ⚠️ 显存提示：本机是 RTX 3060 Laptop（6GB）。14B q4 约 9GB，**装不下**。
    想跑本地模型得用 7B q4（~4.7GB，勉强）或 3B/4B。
    """

    # 三态：显式设了 WHOCHAT_LLM_ENABLED 就用它；没设则"配齐了就自动开"
    enabled: bool | None = _env_bool_or_none("WHOCHAT_LLM_ENABLED")
    base_url: str = os.getenv("WHOCHAT_LLM_BASE_URL", "").strip()
    api_key: str = os.getenv("WHOCHAT_LLM_API_KEY", "").strip()
    # 留空 → 自动从 GET /models 里挑一个；挑不出来会明确报错让你填
    model: str = os.getenv("WHOCHAT_LLM_MODEL", "").strip()
    # 清洗/打标是确定性任务，温度必须为 0
    temperature: float = 0.0
    timeout: int = int(os.getenv("WHOCHAT_LLM_TIMEOUT", "120"))
    # 每次请求塞多少条文本。批量是省钱的关键（1 次请求 vs K 次），
    # 但太大容易让模型漏项/截断，所以给个保守默认。
    batch_size: int = int(os.getenv("WHOCHAT_LLM_BATCH", "10"))
    # 失败重试次数（指数退避，429/5xx/超时才重试）
    max_retries: int = int(os.getenv("WHOCHAT_LLM_RETRIES", "3"))
    # 每分钟最多几次请求，0 = 不限。给免费额度小的供应商留个刹车
    max_requests_per_minute: int = int(os.getenv("WHOCHAT_LLM_RPM", "0"))

    @property
    def is_enabled(self) -> bool:
        """没显式开关时：只要 base_url 与 api_key 都填了就算开启。

        「只填请求地址和 key 就能用」这条承诺靠的就是这个默认值 ——
        不该再让人去翻文档找那个额外的开关。
        """
        if self.enabled is not None:
            return self.enabled
        return bool(self.base_url and self.api_key)

    @property
    def configured(self) -> bool:
        """是否配齐了必要项（不看显式开关）。"""
        return bool(self.base_url and self.api_key)


@dataclass
class SentimentConfig:
    """情感分析配置。默认走词典法（零依赖），装了 transformers 可切模型。"""

    backend: str = os.getenv("WHOCHAT_SENTIMENT_BACKEND", "lexicon")  # lexicon | transformer
    model_name: str = os.getenv(
        "WHOCHAT_SENTIMENT_MODEL",
        "IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment",
    )
    # 判定阈值：分数绝对值低于此值算中性
    neutral_band: float = float(os.getenv("WHOCHAT_SENTIMENT_NEUTRAL_BAND", "0.15"))
    device: str = os.getenv("WHOCHAT_SENTIMENT_DEVICE", "cpu")


@dataclass
class AlertConfig:
    """预警配置。

    企微机器人硬限制（方案文档 §6.2）：
        20 条/分钟/机器人，单条 body ≤ 2048 字节，Markdown 不支持 @人。
    因此推送前必须做窗口聚合 + 分级 + 冷却，否则爆发时会被限流打爆。
    """

    # 企微机器人 webhook。留空则只记录不推送（dry-run）。
    wecom_webhook: str = os.getenv("WHOCHAT_WECOM_WEBHOOK", "")
    # 同一事件冷却秒数
    cooldown_seconds: int = int(os.getenv("WHOCHAT_ALERT_COOLDOWN", "300"))
    # 聚合窗口秒数：窗口内的告警合并成一条推送
    agg_window_seconds: int = int(os.getenv("WHOCHAT_ALERT_AGG_WINDOW", "600"))
    # 每分钟最多推送条数（企微限制 20，留安全余量）
    max_per_minute: int = int(os.getenv("WHOCHAT_ALERT_MAX_PER_MINUTE", "15"))
    # 单条消息字节上限
    max_body_bytes: int = 2048
    # 哪些等级实时推送，其余进日报
    realtime_levels: tuple[str, ...] = ("red",)
    # 发送失败的告警会保持 pending 自动重试，超过这个时长仍未成功才放弃
    # （标记 failed）。避免配错 webhook 时无限重试刷日志。
    retry_max_age_seconds: int = int(os.getenv("WHOCHAT_ALERT_RETRY_MAX_AGE", "21600"))


@dataclass
class WebConfig:
    """看板配置。

    ⚠️ 端口不能选浏览器禁用端口。Chrome / Edge / Firefox 内置一份禁用端口
    清单（6665~6669 等原 IRC 端口段也在其中），浏览器在建立连接前就拒绝，
    显示"无法访问此页面"。而 curl / requests 不检查这份清单，服务端一切正常
    —— 于是 HTTP 200 会给人"页面没问题"的错觉。默认改用 8501。

    另注意 MediaCrawler 自带 WebUI 占 8080，别撞。
    """

    # 用 default_factory 而不是裸默认值：dataclass 的默认值在**导入时**求值一次，
    # 之后改环境变量不会重新读（StoreConfig 同理）。
    port: int = field(
        default_factory=lambda: int(os.getenv("WHOCHAT_DASHBOARD_PORT", "8501"))
    )

    @property
    def dashboard_url(self) -> str:
        """给推送消息里附的看板链接用。"""
        return f"http://localhost:{self.port}"


def _resolve_db_url() -> str:
    """数据库连接串。相对 sqlite 路径锚定到项目根目录。

    `.env.example` 里给的示例是 `sqlite:///data/db/Whochat.db`，而 SQLAlchemy
    会相对**当前工作目录**解析它 —— 只要不是从项目根目录启动就会报
    `unable to open database file`。这里统一转成绝对路径。
    """
    raw = os.getenv("WHOCHAT_DB_URL")
    if not raw:
        return f"sqlite:///{DB_DIR / 'Whochat.db'}"

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
    echo: bool = _env_bool("WHOCHAT_DB_ECHO")


@dataclass
class Settings:
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    web: WebConfig = field(default_factory=WebConfig)


settings = Settings()
