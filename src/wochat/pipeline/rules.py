"""规则清洗 —— 无模型，毫秒级。

这一层只做「不改语义的降噪」和「统计口径的规范化」，
不做词干化/去停用词之类的语义操作（那是给词云和主题建模用的，见 tokenize()）。

设计原则：**规则清洗必须是纯函数、可无限重跑、零副作用**。
它跑在每条数据上，是整个链路里调用次数最多的一环。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from wochat.config import DICT_DIR

# ---------------------------------------------------------------- 词典

DEFAULT_STOPWORDS = """的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 他 她 它 我们 你们 他们 这个 那个 什么 怎么 为什么 可以 但是 因为 所以 如果 还是
啊 吧 呢 吗 呀 哦 嗯 哈 啦 嘛 唉 哎 哇 噗 呵呵 哈哈 哈哈哈 666 233
以及 并且 而且 虽然 然而 不过 只是 就是 还有 然后 现在 已经 一下 一直 一些 一样 这么 那么
真的 感觉 觉得 应该 可能 也许 大概 反正 其实 确实 的确 有点 非常 特别 十分 比较 稍微 完全
个 只 条 件 次 种 位 名 张 台 部 款 点 分 秒 天 年 月 日 时
请 请问 谢谢 感谢 麻烦 帮忙 问下 想问 有人 有没有 知道 哪个 哪些 多少 哪里 怎么 如何
https http www com cn net org
"""


@lru_cache(maxsize=1)
def stopwords() -> set[str]:
    """停用词表。优先读 dicts/stopwords.txt，没有就用内置的。"""
    path = Path(DICT_DIR) / "stopwords.txt"
    words = set(DEFAULT_STOPWORDS.split())
    if path.exists():
        words |= {w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()}
    return words


@lru_cache(maxsize=1)
def sensitive_words() -> set[str]:
    """敏感词表 —— 快通道预警用。dicts/sensitive_words.txt，一行一个。"""
    path = Path(DICT_DIR) / "sensitive_words.txt"
    if not path.exists():
        return set()
    return {w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}


@lru_cache(maxsize=1)
def negative_words() -> set[str]:
    """负面情绪词表 —— 快通道预警的速度突变检测用。"""
    path = Path(DICT_DIR) / "negative_words.txt"
    if not path.exists():
        return set()
    return {w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}


# ---------------------------------------------------------------- 正则

_RE_URL = re.compile(r"https?://\S+|www\.\S+")
_RE_MENTION = re.compile(r"@[\w一-鿿\-_]{1,30}")
_RE_TOPIC = re.compile(r"#([^#]{1,50})#")
_RE_HTML = re.compile(r"<[^>]+>")
_RE_ZERO_WIDTH = re.compile(r"[​-‏‪-‮﻿]")
_RE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF←-⇿⬀-⯿]"
)
_RE_WS = re.compile(r"\s+")
_RE_CN = re.compile(r"[一-鿿]")

# 广告/水军启发式规则
#
# 注意：这些模式是从真实评论里踩坑总结的，"加V信 abc12345" 这种
# 中间带空格的写法必须在正则里显式允许，否则最典型的引流广告会全部漏掉。
# 单个拉丁字母 v/V 只有在独立成词时才算微信号 —— "version"/"video" 的首字母
# 也是 v，若不加边界会把大量正常英文评论误杀（违反"宁可漏杀，不可错杀"）。
# 用 ASCII 字母负向环视而非 \b：\b 把中文也当词字符，"加V信" 里的 V 会被误判为非独立。
_BARE_CONTACT = r"(?<![a-zA-Z])[vV](?![a-zA-Z])"
_CONTACT = rf"(?:{_BARE_CONTACT}|vx|VX|wx|WX|微信|威信|薇信|微微|扣扣|QQ|qq|电报|telegram|飞机)"
_ID = r"[a-zA-Z0-9_\-]{4,}"

_SPAM_PATTERNS = [
    # 加V / 加微信 / 加V信 xxx  ← 允许 "加" 与 "V" 之间、以及标识符前有空格
    re.compile(rf"(?:加|＋|\+)\s*{_CONTACT}\s*[信号码]?\s*[:：,，]?\s*{_ID}"),
    # 联系方式 + 引导动作（私聊/详聊/咨询/领取）
    re.compile(rf"{_CONTACT}\s*[信号码]?\s*[:：,，]?\s*{_ID}"),
    re.compile(r"(私聊|私信|详聊|咨询|联系)\s*(我|本人|博主)"),
    re.compile(r"(代运营|刷单|刷量|涨粉|推广|引流|带货)\s*(服务|公司|团队)?"),
    re.compile(r"(优惠券|折扣|特价|清仓|秒杀|福利).{0,12}(链接|点击|领取|下单|私信|主页)"),
    re.compile(r"(点击|戳)\s*(链接|这里|主页|下方)"),
    # 纯数字/字母长串（QQ号、微信号、网址残留）
    re.compile(r"[a-zA-Z0-9]{6,}[:：]?[0-9]{5,}"),
    re.compile(r"(全网最低|厂家直销|一件代发|免费领|限时抢)"),
]


def clean(text: str | None, *, keep_topic: bool = True) -> str:
    """通用清洗。保留语义，只去噪声。"""
    if not text:
        return ""
    t = _RE_HTML.sub(" ", text)
    t = _RE_URL.sub(" ", t)
    t = _RE_MENTION.sub(" ", t)
    t = _RE_ZERO_WIDTH.sub("", t)
    t = _RE_EMOJI.sub(" ", t)
    if keep_topic:
        # 话题标签保留内容（#iPhone17# → iPhone17），因为话题本身就是主题信号
        t = _RE_TOPIC.sub(r"\1", t)
    else:
        t = _RE_TOPIC.sub(" ", t)
    return _RE_WS.sub(" ", t).strip()


def is_spam(text: str | None) -> bool:
    """启发式判定广告/水军。宁可漏杀，不可错杀 —— 误判会污染所有下游统计。"""
    if not text:
        return True
    if len(text) < 2:
        return True
    # 中文占比过低（纯表情/纯符号/纯外文广告）
    if len(text) > 10 and len(_RE_CN.findall(text)) / len(text) < 0.15:
        return True
    hits = sum(1 for p in _SPAM_PATTERNS if p.search(text))
    return hits >= 1


def is_meaningful(text: str | None, min_chars: int = 4) -> bool:
    """是不是一条有分析价值的内容。

    太短的评论（"顶"、"好"、"666"、"哈哈"）对情感和主题分析都是噪声，
    但注意：它们在**声量统计**里仍然算数。所以这个判定只用于分析环节，
    不用于采集过滤 —— 采集层永远保留全量。
    """
    if not text:
        return False
    stripped = clean(text)
    return len(_RE_CN.findall(stripped)) >= min_chars


# ---------------------------------------------------------------- 分词


@lru_cache(maxsize=1)
def _jieba():
    import jieba

    # 加载领域自定义词典（否则"降本增效""以旧换新"这类词会被切碎）
    user_dict = Path(DICT_DIR) / "user_dict.txt"
    if user_dict.exists():
        jieba.load_userdict(str(user_dict))
    return jieba


def tokenize(text: str | None, *, min_len: int = 2, drop_stopwords: bool = True) -> list[str]:
    """中文分词 —— 词云和主题建模的基础。

    过滤：停用词、纯数字、单字（中文单字信息量太低）。
    """
    if not text:
        return []
    jb = _jieba()
    cleaned = clean(text)
    tokens = jb.lcut(cleaned)
    sw = stopwords() if drop_stopwords else set()

    out = []
    for w in tokens:
        w = w.strip()
        if not w or w in sw:
            continue
        if w.isdigit():
            continue
        if len(w) < min_len:
            continue
        out.append(w)
    return out


def extract_keywords(text: str | None, top_k: int = 10) -> list[str]:
    """TF-IDF 关键词抽取。用于词云和 trending 词统计。"""
    if not text or len(text) < 8:
        return []
    import jieba.analyse

    try:
        return jieba.analyse.extract_tags(clean(text), topK=top_k)
    except Exception:
        # 关键词抽取失败不能拖垮整条链路
        return tokenize(text)[:top_k]


def count_matches(text: str | None, words: set[str]) -> int:
    """命中词表多少个词 —— 快通道增速检测用。"""
    if not text or not words:
        return 0
    return sum(1 for w in words if w in text)
