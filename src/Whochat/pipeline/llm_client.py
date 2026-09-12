"""LLM 客户端 —— 任何 OpenAI 兼容接口。

**只需要两个配置**：

    WHOCHAT_LLM_BASE_URL=https://api.deepseek.com/v1
    WHOCHAT_LLM_API_KEY=sk-xxxxxxxx

模型名可以不填：会自动调 `GET /models` 挑一个对话模型（挑不出来会明确报错，
让你填 `WHOCHAT_LLM_MODEL`）。base_url 也可以只写到域名 —— 会自动在
`{base}/chat/completions` 与 `{base}/v1/chat/completions` 之间探测一次并缓存。

## 为什么走 OpenAI 兼容协议

这套协议已是事实标准，DeepSeek / 通义千问 / Moonshot / 智谱 / OpenAI /
以及本地 Ollama 的 `/v1` 端点全都实现了它。换供应商只改 base_url，
调用方一行不动 —— 与 `CrawlerSource` 协议同一个思路（ADR#1 的教训）。

## 为什么不用官方 SDK

`requests` 本来就是核心依赖，而 `openai` SDK 会额外拖一堆东西进来。
这里只需要一个 POST，不值得为它加依赖 —— 也就保住了
「配两个环境变量就能用」这个承诺。

## 优雅降级（全项目一贯原则）

任何异常都不向上抛：网络不通、401、超时、返回不是 JSON —— 全部变成
`LLMResponse(ok=False, error=...)`。调用方看到 ok=False 就跳过 LLM 环节继续跑。
**整条链路不能因为一个可选组件挂掉。**
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from Whochat.config import settings

# 明显的非对话模型，自动挑模型时排除掉
_NON_CHAT_MARKERS = (
    "embed", "embedding", "rerank", "whisper", "tts", "audio",
    "image", "vision", "moderation", "dall-e", "stable-diffusion",
)

# 挑模型时的偏好顺序（命中越靠前越优先）。只是偏好，不是白名单 ——
# 全都没命中时退到"第一个候选"，不会因为没收录某个新模型就罢工。
_CHAT_PREFERENCE = (
    "deepseek-chat", "qwen-max", "qwen-plus", "qwen-turbo", "glm-4",
    "moonshot", "gpt-4o-mini", "gpt-4o", "gpt-4", "claude", "chat",
)


@dataclass
class LLMUsage:
    """token 用量累计。没有这个，跑完几万条不知道花了多少钱。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def merge(self, other: dict) -> None:
        self.requests += 1
        self.prompt_tokens += int(other.get("prompt_tokens") or 0)
        self.completion_tokens += int(other.get("completion_tokens") or 0)

    def report(self) -> str:
        return (
            f"{self.requests} 次请求 · "
            f"prompt {self.prompt_tokens} + completion {self.completion_tokens} "
            f"= {self.total_tokens} tokens"
        )


@dataclass
class LLMResponse:
    ok: bool
    content: str | None = None
    error: str | None = None
    status: int | None = None
    usage: dict | None = None


class LLMClient:
    """OpenAI 兼容的对话客户端。所有公开方法都不抛异常。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
        rpm: int | None = None,
    ):
        self.base_url = (base_url or settings.llm.base_url or "").strip().rstrip("/")
        self.api_key = (api_key or settings.llm.api_key or "").strip()
        self.model = (model if model is not None else settings.llm.model).strip()
        self.timeout = timeout if timeout is not None else settings.llm.timeout
        self.max_retries = (
            max_retries if max_retries is not None else settings.llm.max_retries
        )
        self.rpm = rpm if rpm is not None else settings.llm.max_requests_per_minute

        self.usage = LLMUsage()
        self._endpoint: str | None = None  # 探测成功后缓存
        self._resolved_model: str | None = None
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------ 配置

    @property
    def configured(self) -> bool:
        """是否配齐了 base_url 与 api_key（不看显式开关）。"""
        return bool(self.base_url and self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------ 端点探测

    def _candidates(self) -> list[str]:
        """按可能性排序的 chat/completions 地址。

        用户可能给 `https://api.deepseek.com`，也可能给
        `https://dashscope.aliyuncs.com/compatible-mode/v1` —— 有的带 /v1
        有的不带，与其要求人记住，不如探测一次。
        """
        if self.base_url.endswith("/chat/completions"):
            return [self.base_url]
        return [
            f"{self.base_url}/chat/completions",
            f"{self.base_url}/v1/chat/completions",
        ]

    def _model_candidates(self) -> list[str]:
        if self.base_url.endswith("/chat/completions"):
            root = self.base_url[: -len("/chat/completions")]
            return [f"{root}/models", f"{root}/v1/models"]
        return [f"{self.base_url}/models", f"{self.base_url}/v1/models"]

    @staticmethod
    def _short(resp: requests.Response) -> str:
        """把错误响应压成一行有用的信息。"""
        try:
            data = resp.json()
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict) and err.get("message"):
                    return str(err["message"])[:200]
                if isinstance(err, str):
                    return err[:200]
                if data.get("message"):
                    return str(data["message"])[:200]
        except Exception:
            pass
        return (resp.text or "")[:200].replace("\n", " ")

    # ------------------------------------------------------------ 模型解析

    def resolve_model(self) -> tuple[str | None, str]:
        """确定要用的模型名。返回 (模型名, 说明)。

        配置里填了就用配置的；没填则调 `GET /models` 自动挑。
        挑不出来时**明确报错而不是瞎猜** —— 猜错的表现是每条都失败，
        而且异常被兜底吞掉，看起来像"LLM 静默不生效"。
        """
        if self._resolved_model:
            return self._resolved_model, "ok"
        if self.model:
            self._resolved_model = self.model
            return self.model, "ok"

        names: list[str] = []
        last_err = ""
        for url in self._model_candidates():
            try:
                resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code} {self._short(resp)}"
                continue
            try:
                data = resp.json()
            except Exception:
                last_err = "返回不是 JSON"
                continue
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                last_err = "响应里没有 data 列表"
                continue
            names = [
                str(it.get("id") or it.get("name") or "")
                for it in items
                if isinstance(it, dict)
            ]
            names = [n for n in names if n]
            break

        if not names:
            return None, (
                f"无法自动确定模型，请显式设置 WHOCHAT_LLM_MODEL。\n"
                f"  探测失败原因: {last_err or '未知'}"
            )

        chat = [n for n in names if not any(m in n.lower() for m in _NON_CHAT_MARKERS)]
        pool = chat or names
        ranked = sorted(
            pool,
            key=lambda n: next(
                (i for i, p in enumerate(_CHAT_PREFERENCE) if p in n.lower()),
                len(_CHAT_PREFERENCE),
            ),
        )
        self._resolved_model = ranked[0]
        return self._resolved_model, f"自动选择（候选 {len(names)} 个）"

    # ------------------------------------------------------------ 可用性

    def available(self) -> tuple[bool, str]:
        """能否真正调用。返回 (可用, 说明)。会发一次真实请求。"""
        if not settings.llm.is_enabled:
            if not self.configured:
                return False, (
                    "未配置 LLM（.env 里填 WHOCHAT_LLM_BASE_URL 与 "
                    "WHOCHAT_LLM_API_KEY 即可）"
                )
            return False, "LLM 被显式关闭（WHOCHAT_LLM_ENABLED=false）"

        model, note = self.resolve_model()
        if not model:
            return False, note

        resp = self.chat([{"role": "user", "content": "ping"}], max_tokens=1)
        if resp.ok:
            return True, f"就绪 · 模型 {model}（{note}）"
        return False, f"调用失败 · 模型 {model} — {resp.error}"

    # ------------------------------------------------------------ 调用

    def _throttle(self) -> None:
        """按 RPM 限速。免费额度小的供应商很容易被 429 打回来。"""
        if self.rpm <= 0:
            return
        min_interval = 60.0 / self.rpm
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)

    def _post(self, url: str, payload: dict) -> requests.Response:
        self._throttle()
        self._last_request_at = time.monotonic()
        return requests.post(
            url, headers=self._headers(), json=payload, timeout=self.timeout
        )

    def chat(
        self,
        messages: list[dict],
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """发一次对话请求。任何失败都返回 ok=False，不抛异常。"""
        if not self.configured:
            return LLMResponse(ok=False, error="未配置 base_url / api_key")

        model, note = self.resolve_model()
        if not model:
            return LLMResponse(ok=False, error=note)

        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": (
                settings.llm.temperature if temperature is None else temperature
            ),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens

        # 端点探测最多两轮；json_mode 不被支持时去掉再试一次
        urls = [self._endpoint] if self._endpoint else self._candidates()
        drop_json_mode = False
        last_error = "未知错误"

        for attempt in range(self.max_retries + 1):
            for url in urls:
                if drop_json_mode and "response_format" in payload:
                    payload.pop("response_format", None)
                try:
                    resp = self._post(url, payload)
                except requests.exceptions.Timeout:
                    last_error = f"超时（{self.timeout}s）"
                    continue
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    continue

                if resp.status_code == 200:
                    self._endpoint = url  # 探测成功，后续不再试别的
                    return self._parse(resp)

                if resp.status_code == 404 and not self._endpoint:
                    # 这个候选地址不对，试下一个；两个都不对则算真失败
                    last_error = f"HTTP 404 端点不存在: {url}"
                    continue

                detail = self._short(resp)

                # 有的供应商不认 response_format，去掉重试一次（不计入退避）
                if (
                    resp.status_code == 400
                    and "response_format" in payload
                    and not drop_json_mode
                ):
                    drop_json_mode = True
                    last_error = f"该接口不支持 JSON Mode，已自动降级 — {detail}"
                    continue

                last_error = f"HTTP {resp.status_code} — {detail}"

                # 只有限流和服务端错误值得退避重试；401/400 重试没有意义
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if (retry_after or "").isdigit() else 0.0
                    if delay <= 0:
                        delay = min(2**attempt, 8)
                    if attempt < self.max_retries:
                        time.sleep(delay)
                    break
                return LLMResponse(
                    ok=False, error=last_error, status=resp.status_code
                )

        return LLMResponse(ok=False, error=last_error)

    @staticmethod
    def _parse(resp: requests.Response) -> LLMResponse:
        try:
            data = resp.json()
        except Exception:
            return LLMResponse(ok=False, error="响应不是 JSON", status=200)

        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            return LLMResponse(ok=False, error=f"响应缺少 choices: {str(data)[:200]}")

        usage = data.get("usage")
        return LLMResponse(
            ok=True,
            content=content,
            status=200,
            usage=usage if isinstance(usage, dict) else None,
        )

    def chat_json(
        self, system: str, user: str, temperature: float | None = None
    ) -> tuple[dict | None, str]:
        """要一段 JSON 回来。返回 (解析结果, 说明)。

        解析失败会带上原始输出 —— 排查时最需要的就是"模型到底吐了什么"。
        """
        resp = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            temperature=temperature,
        )
        if not resp.ok:
            return None, resp.error or "调用失败"

        if resp.usage:
            self.usage.merge(resp.usage)

        data = _loads_tolerant(resp.content or "")
        if data is None:
            return None, f"JSON 解析失败，原始输出: {(resp.content or '')[:200]}"
        return data, "ok"


def align_by_index(items: list, n: int) -> list[int | None]:
    """把模型返回的一批条目对齐到输入的 `n` 个位置。

    返回长度与 `items` 相同的列表，每项是"这条结果属于输入的第几项"，
    None 表示丢弃（多余的条目）。调用方按此写回结果。

    ## 为什么策略要整体决定，不能逐条回退

    模型返回 `i=1,2,3` 而我们只有 3 条输入时，逐条回退会这样翻车：
    第 1 条 `i=1` 落在范围内 → 写到位置 1；第 2 条 `i=2` → 写到位置 2；
    第 3 条 `i=3` 越界 → 回退到"位置 2" → **把刚写好的位置 2 覆盖掉**。
    于是位置 0 永远空着（被记成"漏项"），位置 2 只剩最后一条。

    表现是"莫名其妙的漏项"，而且随模型返回顺序变化，极难排查。
    这个 bug 在 `llm_clean` 和 `name_topics` 里各出现过一次 —— 所以抽到这里共用。
    """
    indices: list[int | None] = []
    for item in items[:n]:
        raw_i = item.get("i") if isinstance(item, dict) else None
        try:
            indices.append(int(raw_i))
        except (TypeError, ValueError):
            indices.append(None)

    usable = all(i is not None and 0 <= i < n for i in indices)
    by_index = usable and len(set(indices)) == len(indices)

    # 长度与 items 对齐，不可用的位置填 None（调用方跳过）。这里不能用 break：
    # 中间夹一条垃圾就把它**后面所有有效结果**一起丢掉，那是凭空的静默损失。
    out: list[int | None] = []
    for pos, item in enumerate(items):
        if pos >= n or not isinstance(item, dict):
            out.append(None)
            continue
        out.append(indices[pos] if by_index else pos)
    return out


def _loads_tolerant(raw: str) -> dict | None:
    """容错 JSON 解析：直接解 → 剥 markdown 代码块 → 截首尾大括号 → json_repair。

    即使开了 JSON Mode 也要容错 —— 输出被 max_tokens 截断是常态。
    """
    if not raw:
        return None

    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        try:
            obj = json.loads(stripped.strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    try:
        from json_repair import repair_json

        obj = repair_json(raw, return_objects=True)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def get_client() -> LLMClient | None:
    """拿一个可用的客户端；不可用时返回 None，调用方跳过 LLM 环节。

    只做配置检查，**不发探测请求** —— 调用方（如 demo）不该因为
    网络慢或没配 LLM 就多等一次超时。
    """
    client = LLMClient()
    if not settings.llm.is_enabled:
        return None
    if not client.configured:
        return None
    return client
