"""把各平台千奇百怪的字段名，归一化成我们的统一 schema。

各平台字段名差异很大（详见 `CONTENT_ALIASES` / `COMMENT_ALIASES`），
而且随时可能变。所以这里用**别名表 + 多候选回退**的策略，而不是硬编码映射：
某个平台改了字段名，通常还能命中别的候选，不至于整条链路挂掉。

配合 `raw_json` 全量存档，即使归一化失败也能事后补救。
"""

from __future__ import annotations

import re
from typing import Any

from wochat.crawler.base import anonymize_id, clean_text_basic, content_record, comment_record

# ---------------------------------------------------------------- 字段别名

CONTENT_ALIASES: dict[str, list[str]] = {
    "content_id": ["note_id", "aweme_id", "video_id", "content_id", "article_id", "tid", "id", "mid"],
    "title": ["title", "note_title", "aweme_title", "video_title"],
    # content_text / video_content 是 zhihu / bilibili 的真实正文字段名
    "body_text": ["desc", "content", "text", "body", "description", "abstract", "content_text", "video_content"],
    "url": ["note_url", "aweme_url", "video_url", "content_url", "url", "share_url"],
    # created_time 是 zhihu 的发布时间字段
    "publish_time": ["create_time", "time", "publish_time", "created_at", "pub_time", "date", "created_time"],
    # creator_hash 是 MediaCrawler 所有平台输出的作者字段（平台侧已匿名哈希）。
    # 少了它，真实采集的 author_id 恒为 NULL —— KOL 识别/作者聚合全部失效。
    "author_id": ["user_id", "author_id", "uid", "sec_uid", "creator_id", "creator_hash"],
    # user_nickname 是 zhihu / tieba 的昵称字段
    "author_name": ["nickname", "author_name", "user_name", "screen_name", "name", "uname", "user_nickname"],
    "author_follower_count": ["follower_count", "fans", "followers", "fans_count"],
    "author_verified": ["is_verified", "verified", "official_verify"],
    "like_count": ["liked_count", "like_count", "digg_count", "likes", "voteup_count"],
    # video_comment 是 bilibili 的评论数
    "comment_count": ["comment_count", "comments_count", "reply_count", "answer_count", "video_comment"],
    "share_count": ["share_count", "shared_count", "repost_count", "forward_count", "video_share_count"],
    "collect_count": ["collected_count", "collect_count", "fav_count", "favorite_count", "video_favorite_count"],
    "parent_content_id": ["parent_content_id", "retweet_id", "repost_id", "forward_id", "origin_id"],
    # aweme_type / video_type 是 douyin / kuaishou 的内容类型
    "content_type": ["type", "content_type", "note_type", "media_type", "aweme_type", "video_type"],
}

COMMENT_ALIASES: dict[str, list[str]] = {
    "comment_id": ["comment_id", "cid", "id", "rpid", "tid"],
    "text": ["content", "text", "comment", "body", "message"],
    "publish_time": ["create_time", "time", "publish_time", "created_at", "ctime"],
    "author_id": ["user_id", "author_id", "uid", "mid", "creator_hash"],
    "author_name": ["nickname", "user_name", "author_name", "uname", "user_nickname"],
    "author_follower_count": ["follower_count", "fans", "followers"],
    # comment_like_count 是 weibo 的评论点赞数
    "like_count": ["like_count", "liked_count", "digg_count", "vote", "like", "comment_like_count"],
    "reply_count": ["sub_comment_count", "reply_count", "sub_comment_num", "replies"],
    "parent_comment_id": ["parent_comment_id", "parent_id", "root_comment_id", "pid"],
    "reply_to_comment_id": ["reply_to_comment_id", "reply_id", "to_comment_id"],
    "ip_location": ["ip_location", "ip_region", "location", "region", "area"],
}


def pick(data: dict, aliases: list[str]) -> Any:
    """按候选顺序取第一个有值的字段。"""
    for key in aliases:
        if key in data:
            value = data[key]
            if value not in (None, "", [], {}):
                return value
    return None


# ---------------------------------------------------------------- 数值解析

_COUNT_UNITS = {"万": 10_000, "w": 10_000, "W": 10_000, "k": 1_000, "K": 1_000, "亿": 100_000_000}


def parse_count(value: Any) -> int | None:
    """解析各平台五花八门的计数格式。

    真实数据里长这样："1.2万"、"3,456"、"1.5w"、"1234"、1234、"暂无"。
    直接 int() 会炸，必须容错。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None

    s = value.strip().replace(",", "").replace(" ", "").replace("+", "")
    if not s or s in ("暂无", "无", "-", "--", "null", "None"):
        return None

    # 容忍"约/近/超/多于"这类修饰前缀，但**必须整串都是合法的计数**。
    # 之前用非锚定的 re.match，会把 "1.2.3" 解析成 1、"1e3" 解析成 1
    # （丢掉 'e' 后面的内容），静默产生错误数字 —— 比返回 None 危险得多。
    s = re.sub(r"^[约近超过大于等于多]+", "", s)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([万亿wWkK]?)", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit:
        num *= _COUNT_UNITS.get(unit, 1)
    return int(num)


_TRUE_WORDS = ("1", "true", "yes", "y", "是", "v", "verified")
_FALSE_WORDS = ("0", "false", "no", "n", "否", "none", "null", "")


def parse_bool(value: Any) -> bool | None:
    """三态解析：True / False / None（未知）。

    未知值返回 None 而不是 False —— 否则"平台明确说未认证"和
    "平台压根没给这个字段/给了个没见过的值"会被混为一谈，
    `author_verified` 这类字段的统计口径就说不清了。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
        return None
    return None


def _first_nonempty(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


# ---------------------------------------------------------------- 归一化


def normalize_content(raw: dict, platform: str, keyword: str | None = None) -> dict | None:
    """平台原始内容 → RawContent 记录。缺关键字段则返回 None。"""
    content_id = pick(raw, CONTENT_ALIASES["content_id"])
    if not content_id:
        return None

    title = pick(raw, CONTENT_ALIASES["title"])
    body = pick(raw, CONTENT_ALIASES["body_text"])

    # 有些平台把标题塞在 desc 里，避免两者完全重复
    if title and body and str(title).strip() == str(body).strip():
        title = None

    ctype = pick(raw, CONTENT_ALIASES["content_type"])
    if isinstance(ctype, int):
        ctype = {1: "video", 2: "note"}.get(ctype, str(ctype))

    return content_record(
        content_id=str(content_id),
        platform=platform,
        search_keyword=keyword or raw.get("source_keyword") or raw.get("keyword"),
        content_type=str(ctype) if ctype else None,
        title=clean_text_basic(str(title)) if title else None,
        body_text=clean_text_basic(str(body)) if body else None,
        url=pick(raw, CONTENT_ALIASES["url"]),
        publish_time=pick(raw, CONTENT_ALIASES["publish_time"]),
        # 必须传 author_raw_id：content_record 内部做脱敏后再写 author_id。
        # 若这里直接传 author_id=，它会被 content_record 的 **kwargs 展开覆盖，
        # 平台原始用户 ID 就原样落库了。
        author_raw_id=pick(raw, CONTENT_ALIASES["author_id"]),
        author_name=pick(raw, CONTENT_ALIASES["author_name"]),
        author_follower_count=parse_count(pick(raw, CONTENT_ALIASES["author_follower_count"])),
        author_verified=parse_bool(pick(raw, CONTENT_ALIASES["author_verified"])),
        like_count=parse_count(pick(raw, CONTENT_ALIASES["like_count"])),
        comment_count=parse_count(pick(raw, CONTENT_ALIASES["comment_count"])),
        share_count=parse_count(pick(raw, CONTENT_ALIASES["share_count"])),
        collect_count=parse_count(pick(raw, CONTENT_ALIASES["collect_count"])),
        parent_content_id=pick(raw, CONTENT_ALIASES["parent_content_id"]),
        raw_json=raw,  # 全量存档 —— 采集不可逆，这是唯一的后悔药
    )


def normalize_comment(raw: dict, platform: str, content_id: str | None = None) -> dict | None:
    """平台原始评论 → Comment 记录。"""
    comment_id = pick(raw, COMMENT_ALIASES["comment_id"])
    text = pick(raw, COMMENT_ALIASES["text"])
    if not comment_id or not text:
        return None

    # 所属内容 ID：平台字段名各异，从别名表 + 调用方传入取
    parent_content = _first_nonempty(
        content_id,
        pick(raw, ["note_id", "aweme_id", "video_id", "content_id", "article_id", "tid"]),
    )
    if not parent_content:
        return None

    parent_comment = pick(raw, COMMENT_ALIASES["parent_comment_id"])
    # bilibili 的顶层评论固定写 parent_comment_id="0"（字符串），
    # pick 认为非空 → 一级评论被全量误标成 level=2 并挂到不存在的父 "0"。
    if parent_comment is not None and str(parent_comment).strip() in ("", "0"):
        parent_comment = None
    level = 2 if parent_comment else 1

    return comment_record(
        comment_id=str(comment_id),
        content_id=str(parent_content),
        platform=platform,
        text=str(text),
        author_raw_id=pick(raw, COMMENT_ALIASES["author_id"]),
        parent_comment_id=str(parent_comment) if parent_comment else None,
        reply_to_comment_id=pick(raw, COMMENT_ALIASES["reply_to_comment_id"]),
        level=level,
        publish_time=pick(raw, COMMENT_ALIASES["publish_time"]),
        author_follower_count=parse_count(pick(raw, COMMENT_ALIASES["author_follower_count"])),
        like_count=parse_count(pick(raw, COMMENT_ALIASES["like_count"])),
        reply_count=parse_count(pick(raw, COMMENT_ALIASES["reply_count"])),
        ip_location=pick(raw, COMMENT_ALIASES["ip_location"]),
        raw_json=raw,
    )
