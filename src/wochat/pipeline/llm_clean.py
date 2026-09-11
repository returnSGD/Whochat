"""本地模型清洗 —— 结构化打标。

**职责边界（重要，方案文档 §3.3）**：
LLM 只做「清洗 + 打标」：判断是不是广告、提取主体、抽关键词。
**不做最终情感判定** —— 那是封闭分类任务，小模型更快更准。

后端：Ollama + Qwen2.5。选 Qwen 的原因是其 **JSON 可靠性被评为 Excellent**
（专为结构化输出训练）；7B 只是 Good，更小的模型在复杂 schema 下经常出非法语法。

结构化输出的可靠性阶梯（从高到低）：
    文法约束解码（Outlines/vLLM）> 原生 Function Calling > JSON Mode > 纯提示词
    ⚠️ Outlines 不支持 Ollama，所以 Ollama 路线用 Instructor 做校验重试。

**优雅降级**：Ollama 没装/没启动/模型没拉，不抛异常，直接返回 None，
调用方跳过 LLM 环节继续跑。整条链路不能因为一个可选组件挂掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from wochat.config import settings
from wochat.pipeline.rules import is_spam

# ---------------------------------------------------------------- Schema

SYSTEM_PROMPT = """你是一个中文社交媒体文本清洗助手。你的任务是把用户给出的评论文本，
转换成结构化 JSON 数据。

规则：
1. 只输出 JSON，不要输出任何解释、markdown 代码块标记或其他文字。
2. 严格按给定字段输出，不要增删字段。
3. `subject` 是这条评论在讨论的对象（品牌/产品/型号/人物/事件），没有则填 null。
4. `keywords` 是 1~5 个最能代表这条评论的名词，不要包含停用词和标点。
5. 拿不准时 `sentiment_hint` 填 neutral，不要瞎猜。
6. 不要输出思考过程。"""

JSON_SCHEMA_HINT = """请严格按以下 JSON 结构输出：
{
  "is_ad": false,
  "is_valid": true,
  "subject": "讨论对象或null",
  "sentiment_hint": "positive|neutral|negative",
  "keywords": ["词1", "词2"]
}"""


@dataclass
class CleanResult:
    is_ad: bool | None = None
    is_valid: bool | None = None
    subject: str | None = None
    sentiment_hint: str | None = None
    keywords: list[str] = field(default_factory=list)
    raw_output: str | None = None
    error: str | None = None


# ---------------------------------------------------------------- 客户端


def _resolve_model(configured: str, names: set[str]) -> str | None:
    """从已安装模型名里解析出真正可用的 tag。

    先要求精确 tag 匹配；没有时才退到同族（同 base）模型。
    绝不返回未安装的 tag —— 否则 clean() 会调用一个不存在的模型，
    异常又被兜底吞掉，表现为「模型不可用但 available() 说 True」。
    """
    if configured in names:
        return configured
    base = configured.split(":")[0]
    # 同族仅认 "base" 或 "base:tag"，避免 qwen2.5 误匹配 qwen2.5-coder
    family = sorted(n for n in names if n == base or n.startswith(base + ":"))
    return family[0] if family else None


class LLMCleaner:
    """Ollama 本地模型清洗器。

    用法：
        cleaner = LLMCleaner()
        if cleaner.available():
            result = cleaner.clean("这个手机发热严重")
    """

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or settings.llm.model
        self.base_url = base_url or settings.llm.base_url
        self._client = None
        # available() 解析出的已安装模型名；clean() 必须用它，而不是配置里的 tag
        self._resolved_model: str | None = None

    # -------------------------------------------------- 可用性

    def available(self) -> tuple[bool, str]:
        """检查 Ollama 服务和模型是否就绪。返回 (可用, 说明)。"""
        if not settings.llm.enabled:
            return False, "LLM 清洗未启用（.env 里设 WOCHAT_LLM_ENABLED=true 开启）"

        try:
            import ollama  # noqa: F401
        except ImportError:
            return False, "未安装 ollama 包，执行: pip install -e .[llm]"

        try:
            import ollama

            client = ollama.Client(host=self.base_url)
            models = client.list()
            names = {
                m.get("model") or m.get("name", "")
                for m in (models.get("models") or [])
            }
            resolved = _resolve_model(self.model, names)
            if resolved is None:
                return False, (
                    f"Ollama 中未找到模型 {self.model}。\n"
                    f"  已安装: {sorted(names) or '无'}\n"
                    f"  拉取: ollama pull {self.model}"
                )
            # 后续 clean() 用真正存在的 tag，否则每次都调用缺失模型、异常被静默吞掉
            self._resolved_model = resolved
            return True, "ok"
        except Exception as e:
            return False, f"连接 Ollama 失败 ({self.base_url}): {e}"

    # -------------------------------------------------- 清洗

    def clean(self, text: str) -> CleanResult:
        """清洗单条文本。任何异常都吞掉并记录，不向上抛。"""
        if not text or not text.strip():
            return CleanResult(is_valid=False, error="empty")

        # 先用便宜规则过滤掉明显垃圾，避免浪费模型调用
        if is_spam(text):
            return CleanResult(is_ad=True, is_valid=False, keywords=[])

        try:
            import ollama

            if self._client is None:
                self._client = ollama.Client(host=self.base_url)

            resp = self._client.chat(
                # 用 available() 解析出的真实 tag；未解析时退回配置值
                model=self._resolved_model or self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{JSON_SCHEMA_HINT}\n\n文本：{text}"},
                ],
                format="json",  # Ollama 的 JSON Mode：保证合法 JSON
                options={
                    # 清洗是确定性任务，温度必须为 0
                    "temperature": settings.llm.temperature,
                    "top_p": 1.0,
                    "seed": 42,
                },
            )
            raw = resp["message"]["content"]
            return self._parse(raw)

        except Exception as e:
            return CleanResult(error=f"{type(e).__name__}: {e}")

    def clean_batch(self, texts: list[str], verbose: bool = True) -> list[CleanResult]:
        results = []
        for i, t in enumerate(texts, 1):
            results.append(self.clean(t))
            if verbose and i % 50 == 0:
                print(f"[llm_clean] {i}/{len(texts)}")
        return results

    # -------------------------------------------------- 解析

    @staticmethod
    def _parse(raw: str) -> CleanResult:
        """解析模型输出。即使有 JSON Mode，也要容错 —— 输出被截断是常态。"""
        data = _loads_tolerant(raw)
        if data is None:
            return CleanResult(raw_output=raw, error="JSON 解析失败")

        sentiment = data.get("sentiment_hint")
        if sentiment not in ("positive", "neutral", "negative"):
            sentiment = None

        keywords = data.get("keywords")
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        elif not isinstance(keywords, list):
            keywords = []

        return CleanResult(
            is_ad=_as_bool(data.get("is_ad")),
            is_valid=_as_bool(data.get("is_valid")),
            subject=data.get("subject") if data.get("subject") not in ("null", "", None) else None,
            sentiment_hint=sentiment,
            keywords=[str(k) for k in keywords][:5],
            raw_output=raw,
        )


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def _loads_tolerant(raw: str) -> dict | None:
    """容错 JSON 解析：直接解 → 剥 markdown 代码块 → 截取首尾大括号 → json_repair。"""
    if not raw:
        return None

    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    # 模型有时会套一层 ```json ... ```
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        try:
            obj = json.loads(stripped.strip())
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass

    # 截取第一个 { 到最后一个 }
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass

    # 最后手段：json_repair 修复被截断/污染的 JSON
    try:
        from json_repair import repair_json

        obj = repair_json(raw, return_objects=True)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------- 工厂


def get_cleaner() -> LLMCleaner | None:
    """获取清洗器。不可用时返回 None，调用方跳过该环节。"""
    cleaner = LLMCleaner()
    ok, msg = cleaner.available()
    if not ok:
        print(f"[llm_clean] 跳过 LLM 清洗: {msg}")
        return None
    print(f"[llm_clean] 使用模型 {cleaner._resolved_model or cleaner.model}")
    return cleaner
