"""用 LLM 学习「平台原始字段名 → 我们 schema」的映射。

## 为什么需要它

`normalize.py` 的别名表是**手写**的。它的天花板在第三轮已经撞到过：MediaCrawler
真实字段 `creator_hash` / `user_nickname` 没被收录，导致真实采集的作者 ID 恒为 NULL、
KOL 识别完全失效。而平台字段名会变、新平台会加 —— 每次都得人去翻源码补表。

LLM 能做这件事是因为"把 `creator_hash` 认成作者 ID"是语义判断，不是查表。
而且它**能泛化到没见过的平台**，别名表不能。

## 关键设计：学一次，不是每条都问

字段名在同一个平台/同一个采集源里是**稳定**的。所以对**一组未知字段名**问一次
模型就够，学到的映射会：

1. 缓存到 `data/llm_field_map.json`（按字段名集合的哈希索引）
2. 之后机械套用到所有记录上

逐条记录调模型是不可行的：一个采集任务几万条，光这一项就能烧掉几百块。
缓存键用 **CRC32 而不是内置 `hash()`** —— 后者有进程级随机盐，同一份输入
换个进程就是另一个键，缓存永远命不中（第二轮踩过一模一样的坑）。

## 只补盲区，不改已有行为

学到的映射只用来**兜底**：别名表命中时一律以别名表为准，只有别名表全没命中
的字段才会去查学到的映射。这样它不可能把已经正确的映射搞坏。
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from Whochat.config import DATA_DIR

CACHE_PATH = DATA_DIR / "llm_field_map.json"

# 各字段的含义说明 —— 模型靠这些描述来判断"这个原始字段对应我们哪一列"。
# 描述写清楚比字段名本身重要：`sec_uid` 光看名字猜不出是作者 ID。
CONTENT_SPEC: dict[str, str] = {
    "content_id": "内容在本平台的唯一 ID",
    "title": "标题",
    "body_text": "正文 / 视频文案 / 笔记正文",
    "url": "内容原始链接",
    "publish_time": "发布时间（时间戳或时间字符串）",
    "author_id": "作者的唯一 ID（可能已被平台哈希）",
    "author_name": "作者昵称 / 用户名",
    "author_follower_count": "作者粉丝数",
    "author_verified": "作者是否认证 / 加 V",
    "like_count": "点赞数",
    "comment_count": "评论数",
    "share_count": "转发 / 分享数",
    "collect_count": "收藏数",
    "parent_content_id": "被转发/引用/回复的上游内容 ID",
    "content_type": "内容类型（视频/笔记/文章/回答）",
}

COMMENT_SPEC: dict[str, str] = {
    "comment_id": "评论在本平台的唯一 ID",
    "text": "评论正文",
    "publish_time": "评论发布时间",
    "author_id": "评论者唯一 ID",
    "author_name": "评论者昵称",
    "author_follower_count": "评论者粉丝数",
    "like_count": "评论点赞数",
    "reply_count": "这条评论的回复数",
    "parent_comment_id": "所属一级评论的 ID（二级评论用）",
    "reply_to_comment_id": "被回复的那条评论 ID",
    "ip_location": "IP 属地",
}

SYSTEM_PROMPT = """你在做数据字段映射。用户给出一组原始字段名（来自某个社媒平台
的采集结果），以及我们目标 schema 的字段清单与含义，请判断每个原始字段
对应目标 schema 里的哪一个。

规则：
1. 只输出 JSON，不要解释、不要 markdown 代码块标记。
2. 拿不准的**不要猜**，放进 `unmapped`，猜错的代价是往库里写错数据，比留空更糟。
3. 一个目标字段只能被映射一次；若多个原始字段都像，选最贴切的，其余进 `unmapped`。
4. 只做映射，不要编造目标 schema 里没有的字段。"""


def build_prompt(raw_keys: list[str], spec: dict[str, str], sample: dict) -> str:
    targets = "\n".join(f"  - {k}：{v}" for k, v in spec.items())
    keys = "\n".join(f"  - {k}" for k in raw_keys)
    # 带几个样例值：只看字段名容易猜错，看一眼值几乎不会错
    examples = []
    for k in raw_keys[:20]:
        v = sample.get(k)
        if v not in (None, "", [], {}):
            examples.append(f"  {k} = {str(v)[:80]!r}")
    sample_block = "\n".join(examples) if examples else "  （无样例值）"

    return (
        "【目标 schema】\n" + targets + "\n\n"
        "【原始字段名】\n" + keys + "\n\n"
        "【样例值（供参考）】\n" + sample_block + "\n\n"
        '返回 JSON：{"mapping": {"原始字段名": "目标字段名", ...}, '
        '"unmapped": ["认不出的原始字段名", ...]}'
    )


@dataclass
class FieldMap:
    """一次学习的结果。"""

    mapping: dict[str, str] = field(default_factory=dict)  # 原始字段名 → 目标字段
    unmapped: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | cache | empty
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.mapping)


def _cache_key(raw_keys: list[str], spec: dict[str, str]) -> str:
    """字段名集合 + 目标 schema 的稳定指纹。

    ⚠️ **必须把 spec 算进去**。内容与评论的原始字段名经常是同一批
    （`id` / `content` / `create_time` …），但两者的目标 schema 不同。
    只用字段名做键的话，先学的「内容」会把结果缓存下来，接着学「评论」时
    命中同一份缓存，按 COMMENT_SPEC 一过滤**全被丢掉**，返回一个空映射 ——
    于是评论字段永远学不到，而且全程不报错。实测踩过。

    用 CRC32 而**不是**内置 `hash()`：后者对 str/tuple 有进程级随机盐
    （PYTHONHASHSEED），同样的输入换个进程就是另一个键，缓存永远命不中，
    而且不报错 —— 表现为"每次都重新学一遍，日志里看不出异常"。
    """
    joined = "\x1f".join(sorted(raw_keys)) + "\x1e" + "\x1f".join(sorted(spec))
    return f"{zlib.crc32(joined.encode('utf-8')) & 0xFFFFFFFF:08x}"


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # 缓存写不进去只是慢一点，不该影响主流程


def learn_field_map(
    sample: dict,
    spec: dict[str, str],
    known_keys: set[str] | None = None,
    client=None,
    cache_path: Path | None = None,
    use_cache: bool = True,
) -> FieldMap:
    """学一组字段名到目标 schema 的映射。

    参数
    ----
    sample
        一条原始记录（字段名 + 样例值）。样例值很有用，别只传字段名。
    spec
        目标 schema：字段名 → 含义描述。
    known_keys
        已经能识别的字段名（别名表里的），这些不会再问模型。
    """
    cache_path = cache_path or CACHE_PATH
    known = {k.lower() for k in (known_keys or set())}

    raw_keys = [k for k in sample if k.lower() not in known]
    if not raw_keys:
        return FieldMap(source="empty", note="没有未知字段")

    key = _cache_key(raw_keys, spec)
    if use_cache:
        cached = _load_cache(cache_path).get(key)
        if isinstance(cached, dict) and isinstance(cached.get("mapping"), dict):
            return FieldMap(
                mapping={k: v for k, v in cached["mapping"].items() if v in spec},
                unmapped=list(cached.get("unmapped") or []),
                source="cache",
                note="命中缓存",
            )

    if client is None:
        from Whochat.pipeline.llm_client import get_client

        client = get_client()
    if client is None:
        return FieldMap(source="empty", note="未配置 LLM")

    data, note = client.chat_json(
        SYSTEM_PROMPT, build_prompt(raw_keys, spec, sample)
    )
    if data is None:
        return FieldMap(source="empty", note=f"调用失败: {note}")

    raw_mapping = data.get("mapping")
    if not isinstance(raw_mapping, dict):
        return FieldMap(source="empty", note=f"响应缺少 mapping: {str(data)[:160]}")

    # 只接受指向真实目标字段的映射，且一个目标字段只认一次
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for raw_key, target in raw_mapping.items():
        if raw_key not in raw_keys:
            continue  # 模型编出来的字段名，丢掉
        t = str(target)
        if t not in spec or t in taken:
            continue
        mapping[str(raw_key)] = t
        taken.add(t)

    unmapped = [k for k in raw_keys if k not in mapping]
    if use_cache and mapping:
        cache = _load_cache(cache_path)
        cache[key] = {"mapping": mapping, "unmapped": unmapped}
        _save_cache(cache_path, cache)

    return FieldMap(mapping=mapping, unmapped=unmapped, source="llm", note="ok")


def extend_aliases(
    aliases: dict[str, list[str]], field_map: FieldMap
) -> dict[str, list[str]]:
    """把学到的映射合并进别名表。

    **追加在候选列表末尾**，不是插到前面 —— 手写别名表是人工核对过的，
    优先级必须高于模型猜的。学到的映射只补别名表覆盖不到的空档。
    """
    if not field_map:
        return aliases
    merged = {k: list(v) for k, v in aliases.items()}
    for raw_key, target in field_map.mapping.items():
        merged.setdefault(target, [])
        if raw_key not in merged[target]:
            merged[target].append(raw_key)
    return merged


def known_keys(aliases: dict[str, list[str]]) -> set[str]:
    """别名表已经覆盖的所有字段名。"""
    return {k for keys in aliases.values() for k in keys}
