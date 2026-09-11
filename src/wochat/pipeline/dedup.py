"""去重 —— 精确 + 近重复。

为什么必须做：网络采集的数据通常含 **5~30% 的近重复**。
同一内容在微博被转发、在抖音被搬运、在小红书被洗稿，文本高度相似。
不去重的话：

- 声量统计虚高（一条内容算十次）
- 情感分布被少数爆款绑架
- 主题建模会把同一个主题拆成十个簇

分两层，从廉价到昂贵：

    ① 精确去重   SHA256(规范化文本)          —— 极快，抓完全相同的
    ② 近重复检测  SimHash 分桶 + 汉明距离      —— 快，抓近似重复
                  MinHash+LSH（装了 datasketch 时自动启用，更准）
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from wochat.pipeline.rules import clean, tokenize

# ---------------------------------------------------------------- 精确去重


def normalize_for_hash(text: str | None) -> str:
    """生成用于比对指纹的规范化文本。

    去掉所有标点和空白 —— 「这个真好用！」和「这个真好用」应当判为同一条。
    """
    if not text:
        return ""
    t = clean(text)
    return re.sub(r"[^\w一-鿿]", "", t).lower()


def text_fingerprint(text: str | None) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


def exact_dedupe(items: Sequence, key=lambda x: x) -> list:
    """按文本指纹精确去重，保留首次出现。空文本直接丢弃。"""
    seen: set[str] = set()
    out = []
    for item in items:
        # 注意：不能拿 text_fingerprint() 判空 —— sha256 的十六进制摘要
        # 永远非空，`not fp` 是死代码，会让第一条空文本被保留、其余被当成
        # "重复"丢掉。这里改成对规范化后的文本判空，语义才和注释一致。
        norm = normalize_for_hash(key(item))
        if not norm:
            continue
        fp = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        if fp in seen:
            continue
        seen.add(fp)
        out.append(item)
    return out


# ---------------------------------------------------------------- SimHash

_MASK64 = (1 << 64) - 1


def _hash64(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")


def simhash(text: str | None, *, f_bits: int = 64) -> int | None:
    """64 位 SimHash。

    对短文本（评论）效果弱于 MinHash，但对模板化/搬运类内容极快且够用。
    自己实现以避免引入依赖 —— 算法只有十几行。
    """
    tokens = tokenize(text, min_len=1)
    if not tokens:
        return None

    weights: dict[str, int] = defaultdict(int)
    for t in tokens:
        weights[t] += 1

    vector = [0] * f_bits
    for token, weight in weights.items():
        h = _hash64(token)
        for i in range(f_bits):
            if h >> i & 1:
                vector[i] += weight
            else:
                vector[i] -= weight

    fingerprint = 0
    for i in range(f_bits):
        if vector[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK64).count("1")


@dataclass
class DedupResult:
    """去重结果。保留重复映射，便于事后审计"为什么这条被删了"。

    `dropped` 只装近重复的**对象**（数量通常不大，值得留着看）；
    `exact_dropped` 只记**数量**（精确重复可能占绝大多数，
    把上万条一模一样的文本都留在内存里没有意义）。
    """

    kept: list
    dropped: list
    duplicate_of: dict[str, str]  # 被删项的指纹 → 保留项的指纹
    exact_dropped: int = 0

    @property
    def total_dropped(self) -> int:
        """精确 + 近重复的总删除数。上层统计必须用这个。"""
        return self.exact_dropped + len(self.dropped)

    @property
    def total(self) -> int:
        return len(self.kept) + self.total_dropped

    @property
    def ratio(self) -> float:
        return self.total_dropped / self.total if self.total else 0.0


# ---------------------------------------------------------------- 近重复


def _band_keys(fingerprint: int, bands: int = 4, bits: int = 64) -> list[tuple[int, int]]:
    """把指纹切成若干段 —— 同段相同即为候选对，把 O(n²) 降到近似线性。"""
    width = bits // bands
    return [(i, (fingerprint >> (i * width)) & ((1 << width) - 1)) for i in range(bands)]


def near_dedupe(
    items: Sequence,
    key=lambda x: x,
    *,
    threshold: int = 3,
    bands: int = 4,
) -> DedupResult:
    """SimHash 近重复去重。

    Args:
        threshold: 汉明距离 ≤ threshold 视为重复。64 位下 3 是常用值
                   （约对应 Jaccard 相似度 0.8+）。
        bands: 分段数。段数越少越快但漏检越多；4 段在召回与速度间平衡较好。
    """
    kept: list = []
    dropped: list = []
    duplicate_of: dict[str, str] = {}

    # 桶：段号+段值 → 已保留项的索引
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    kept_fps: list[int] = []
    kept_keys: list[str] = []

    for item in items:
        fp = simhash(key(item))
        if fp is None:
            # 分不出词（纯符号/空白）—— 保留，交给下游的 is_meaningful 处理
            kept.append(item)
            kept_fps.append(0)
            kept_keys.append("")
            continue

        # 只和共享任一段的候选比对，避免全量两两比较
        candidates: set[int] = set()
        for bk in _band_keys(fp, bands):
            candidates.update(buckets.get(bk, ()))

        dup_of: int | None = None
        for idx in candidates:
            if hamming(fp, kept_fps[idx]) <= threshold:
                dup_of = idx
                break

        if dup_of is None:
            kept.append(item)
            kept_fps.append(fp)
            kept_keys.append(text_fingerprint(key(item)))
            for bk in _band_keys(fp, bands):
                buckets[bk].append(len(kept) - 1)
        else:
            dropped.append(item)
            src = kept_keys[dup_of]
            if src:
                duplicate_of[text_fingerprint(key(item))] = src

    return DedupResult(kept=kept, dropped=dropped, duplicate_of=duplicate_of)


# ---------------------------------------------------------------- MinHash


def minhash_dedupe(
    items: Sequence,
    key=lambda x: x,
    *,
    threshold: float = 0.8,
    num_perm: int = 128,
) -> DedupResult | None:
    """MinHash + LSH。比 SimHash 准，但需要 datasketch。

    装了 `pip install datasketch` 才可用；没装返回 None，调用方回退到 SimHash。

    中文短文本建议用**字符级 3-gram** 做 shingle —— 词级对评论这种
    口语化短文本太稀疏，效果反而差。
    """
    try:
        from datasketch import MinHash, MinHashLSH
    except ImportError:
        return None

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    kept, dropped = [], []
    duplicate_of: dict[str, str] = {}
    fps: dict[str, str] = {}  # lsh key → text fingerprint

    for i, item in enumerate(items):
        text = normalize_for_hash(key(item))
        if not text:
            kept.append(item)
            continue

        m = MinHash(num_perm=num_perm)
        # 3-gram 对长度 ≤2 的文本会产生 0 个 shingle，MinHash 全空 →
        # 所有空签名互相判为近重复，于是"支持""谢谢""关注"这类高频短评
        # 只会留下第一条，其余静默丢出情感/主题统计。
        # 按文本长度回退 shingle 宽度；长度 ≥3 时与原来的 3-gram 完全一致。
        width = min(3, len(text))
        for j in range(len(text) - width + 1):
            m.update(text[j : j + width].encode("utf-8"))

        matches = lsh.query(m)
        item_key = f"item_{i}"
        if matches:
            source = fps.get(matches[0], "")
            dropped.append(item)
            if source:
                duplicate_of[text_fingerprint(key(item))] = source
        else:
            lsh.insert(item_key, m)
            fps[item_key] = text_fingerprint(key(item))
            kept.append(item)

    return DedupResult(kept=kept, dropped=dropped, duplicate_of=duplicate_of)


# ---------------------------------------------------------------- 组合


def dedupe(
    items: Sequence,
    key=lambda x: x,
    *,
    near: bool = True,
    prefer_minhash: bool = True,
    threshold: int = 3,
) -> DedupResult:
    """完整去重流水线：先精确，再近重复。

    先精确后近重复的顺序很重要 —— 精确去重极快且能砍掉一大半，
    让后面的近重复检测只需处理剩下的。
    """
    original = len(items)
    exact_kept = exact_dedupe(items, key)
    exact_dropped = original - len(exact_kept)

    if not near or len(exact_kept) < 2:
        return DedupResult(kept=exact_kept, dropped=[], duplicate_of={}, exact_dropped=exact_dropped)

    if prefer_minhash:
        result = minhash_dedupe(exact_kept, key)
        if result is not None:
            # 把精确去重的计数并进来 —— 否则上层统计会漏掉大头
            return DedupResult(
                kept=result.kept,
                dropped=result.dropped,
                duplicate_of=result.duplicate_of,
                exact_dropped=exact_dropped,
            )

    near_result = near_dedupe(exact_kept, key, threshold=threshold)
    near_result.exact_dropped = exact_dropped
    return near_result
