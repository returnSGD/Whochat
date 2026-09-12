"""LLM 结构化打标 —— 广告识别 / 主体识别 / 关键词抽取。

**职责边界（重要，方案文档 §3.3 / ADR#5）**：
LLM 只做「清洗 + 打标」：判断是不是广告、这条在讨论谁、抽关键词。
**不做最终情感判定** —— 那是封闭分类任务，小模型/词典法更快更准更可复现。
（实测佐证：transformer 情感后端在同一标注集上 60.6%，词典法 90.9%。）

## 为什么这块该用 LLM

规则法在这三个任务上都有硬天花板：
- **广告识别**：`rules.py::is_spam` 是关键词正则，第三轮就出过误杀 ——
  「商家刷单太明显了，太失望了」是本该被分析的**负面舆情**，却因为含
  "刷单"被当广告永久排除出情感统计。这是语义判断，正则做不了。
- **主体识别**：「这条在骂哪个产品/型号」规则完全无能为力，
  `analysis_results.subject` 字段建了表就一直空着。
- **关键词**：jieba + TF-IDF 抽的是高频词，不是"这条在说什么"。

## 批量是省钱的关键

一次请求处理 K 条，比逐条请求省 K 倍的钱和往返。但批量会引入**漏项风险**：
模型可能少返回几条，或者把序号搞乱。所以这里按序号对齐，**缺项一律回落到
"未经 LLM 处理"而不是丢弃** —— 丢数据比少打一个标严重得多。

## 优雅降级

任何失败都不抛异常：Ollama 没起、API key 错、网络不通、返回不是 JSON ——
全部返回 `CleanResult(error=...)`，调用方跳过 LLM 环节继续跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from Whochat.config import settings
from Whochat.pipeline.llm_client import LLMClient, align_by_index, get_client
from Whochat.pipeline.rules import is_spam

# 单条文本给模型的长度上限。评论文本一般很短，设这个是为了防
# 某条异常长的文本把整个批次的上下文撑爆（会连带整批失败）。
MAX_TEXT_CHARS = 1000

SYSTEM_PROMPT = """你是一个中文社交媒体文本清洗助手。你的任务是把用户给出的评论文本，
转换成结构化 JSON 数据。

规则：
1. 只输出 JSON，不要输出任何解释、markdown 代码块标记或其他文字。
2. 严格按给定字段输出，不要增删字段。
3. `subject` 是这条评论在讨论的对象（品牌/产品/型号/人物/事件），没有则填 null。
4. `keywords` 是 1~5 个最能代表这条评论的名词，不要包含停用词和标点。
5. 拿不准时 `sentiment_hint` 填 neutral，不要瞎猜。
6. 判断 `is_ad` 要看**意图**而不是关键词：用户在抱怨"商家刷单太明显了"
   是在**批评**刷单行为，不是广告，`is_ad` 应为 false。
7. 必须为输入里的每一个序号都输出一条结果，一条都不能少。
8. 不要输出思考过程。"""


@dataclass
class CleanResult:
    is_ad: bool | None = None
    is_valid: bool | None = None
    subject: str | None = None
    sentiment_hint: str | None = None
    keywords: list[str] = field(default_factory=list)
    raw_output: str | None = None
    error: str | None = None
    # True 表示这条真的经过了模型；False 表示回落（跳过/漏项/失败）
    from_llm: bool = False


def _single_schema() -> str:
    return """{
  "i": 序号(整数),
  "is_ad": false,
  "is_valid": true,
  "subject": "讨论对象或null",
  "sentiment_hint": "positive|neutral|negative",
  "keywords": ["词1", "词2"]
}"""


def build_user_prompt(texts: list[str]) -> str:
    """拼批量请求。带序号是为了让模型输出能和输入对齐。"""
    body = "\n".join(
        f"[{i}] {t[:MAX_TEXT_CHARS]}" for i, t in enumerate(texts)
    )
    return (
        f"请处理下面 {len(texts)} 条文本，逐条输出。\n"
        f'返回 JSON：{{"results": [ ... ]}}，其中每一项形如：\n{_single_schema()}\n\n'
        f"待处理文本：\n{body}"
    )


class LLMCleaner:
    """基于 OpenAI 兼容接口的结构化打标器。

    用法：
        cleaner = LLMCleaner()
        if cleaner.available()[0]:
            results = cleaner.clean_batch(["这个手机发热严重", ...])
    """

    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()

    @property
    def model(self) -> str:
        return self.client._resolved_model or self.client.model or ""

    # -------------------------------------------------- 可用性

    def available(self) -> tuple[bool, str]:
        return self.client.available()

    # -------------------------------------------------- 单条

    def clean(self, text: str) -> CleanResult:
        """清洗单条。保留这个入口是为了单条试跑/测试方便。"""
        return self.clean_batch([text], verbose=False)[0]

    # -------------------------------------------------- 批量

    def clean_batch(
        self, texts: list[str], batch_size: int | None = None, verbose: bool = True
    ) -> list[CleanResult]:
        """批量打标。返回的列表与输入**等长且顺序一致**。

        这是本模块最重要的契约：调用方按 `zip(comments, results)` 消费结果，
        长度对不上就会错位 —— 把 A 的标签贴到 B 身上，且完全静默。
        """
        size = max(1, batch_size or settings.llm.batch_size)

        results: list[CleanResult] = [CleanResult(error="未处理") for _ in texts]

        # 先用便宜的规则挡掉明显垃圾，剩下的才值得花 token
        pending: list[int] = []
        for i, t in enumerate(texts):
            if not t or not t.strip():
                results[i] = CleanResult(is_valid=False, error="empty")
            elif is_spam(t):
                results[i] = CleanResult(is_ad=True, is_valid=False)
            else:
                pending.append(i)

        done = 0
        reported = 0
        for start in range(0, len(pending), size):
            idxs = pending[start : start + size]
            chunk = [texts[i] for i in idxs]
            self._run_chunk(chunk, idxs, results)
            done += len(chunk)
            if verbose and done - reported >= 50:
                print(f"[llm_clean] {done}/{len(pending)}")
                reported = done

        if verbose and pending:
            ok = sum(1 for i in pending if results[i].from_llm)
            print(f"[llm_clean] 打标完成 {ok}/{len(pending)} 条（{self.client.usage.report()}）")

        return results

    def _run_chunk(
        self, chunk: list[str], idxs: list[int], results: list[CleanResult]
    ) -> None:
        """跑一批。失败/漏项时保留占位，绝不丢条目。"""
        # 先一律置为"漏项"，对齐成功的再覆盖 —— 这样"没被覆盖到"就等价于
        # "模型没返回这条"，语义明确。**绝不能**默认成 is_ad=False/is_valid=True：
        # 那等于把"没拿到结果"谎报成"模型判定它不是广告"，是最坏的一种静默错误。
        for i in idxs:
            results[i] = CleanResult(error="模型漏项")

        data, note = self.client.chat_json(SYSTEM_PROMPT, build_user_prompt(chunk))
        if data is None:
            for i in idxs:
                results[i] = CleanResult(error=note)
            return

        items = data.get("results")
        if not isinstance(items, list):
            # 有的模型会直接把单条结果平铺在最外层（批量=1 时尤其常见）
            items = [data] if "is_ad" in data or "is_valid" in data else []
        if not items:
            for i in idxs:
                results[i] = CleanResult(error=f"响应里没有 results: {str(data)[:160]}")
            return

        # 对齐逻辑见 align_by_index 的文档 —— 关键是策略整体决定，不能逐条回退
        for item, local in zip(items, align_by_index(items, len(idxs))):
            if local is None:
                continue
            results[idxs[local]] = parse_item(item)


def parse_item(item: dict) -> CleanResult:
    """把模型返回的一条 JSON 转成 CleanResult。"""
    sentiment = item.get("sentiment_hint")
    if sentiment not in ("positive", "neutral", "negative"):
        sentiment = None

    keywords = item.get("keywords")
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    elif not isinstance(keywords, list):
        keywords = []

    subject = item.get("subject")
    if subject in ("null", "", None):
        subject = None
    else:
        subject = str(subject)[:256]

    return CleanResult(
        is_ad=_as_bool(item.get("is_ad")),
        is_valid=_as_bool(item.get("is_valid")),
        subject=subject,
        sentiment_hint=sentiment,
        keywords=[str(k) for k in keywords][:5],
        raw_output=None,
        from_llm=True,
    )


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def get_cleaner() -> LLMCleaner | None:
    """获取打标器。未配置/未启用时返回 None，调用方跳过该环节。"""
    client = get_client()
    return LLMCleaner(client) if client else None
