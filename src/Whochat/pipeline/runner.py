"""流水线编排 —— 把各层串成完整链路。

    采集 → 落库 → 规则清洗 → 去重 → 情感分析 → 落库
                              ↓
                        快通道预警（不等模型）

**关键设计：采集与分析解耦**（方案文档 ADR #4）。
采集到的原始数据全量落库，任何分析都可以事后重跑。这样：

- 换了情感模型 → 重跑分析，不用重爬
- 调了预警阈值 → 重跑规则，不用重爬
- 发现漏了字段 → 抱歉，这个补不回来（所以采集层必须抓全）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from Whochat.analysis.sentiment import get_analyzer
from Whochat.config import RAW_DIR
from Whochat.crawler.base import CrawlTask, resolve_source
from Whochat.pipeline import dedup as dedup_mod
from Whochat.pipeline.rules import clean, extract_keywords, is_spam
from Whochat.store.models import utcnow
from Whochat.store.repository import Repository


@dataclass
class PipelineStats:
    """每一步的产出计数。没有这个，出问题时你不知道是哪一层丢了数据。"""

    crawled_contents: int = 0
    crawled_comments: int = 0
    stored_contents: int = 0
    stored_comments: int = 0
    snapshots: int = 0
    dropped_spam: int = 0
    dropped_dup: int = 0
    analyzed: int = 0
    errors: list[str] = field(default_factory=list)

    def report(self) -> str:
        return (
            f"采集 内容 {self.crawled_contents} / 评论 {self.crawled_comments}\n"
            f"落库 内容 {self.stored_contents} / 评论 {self.stored_comments} / 快照 {self.snapshots}\n"
            f"过滤 广告 {self.dropped_spam} / 重复 {self.dropped_dup}\n"
            f"分析 {self.analyzed} 条"
        )


class Pipeline:
    def __init__(self, repo: Repository | None = None, version: str | None = None):
        self.repo = repo or Repository()
        self.analyzer = get_analyzer()
        # 版本号包含后端名 —— 换模型后结果不会被旧数据覆盖，且可对比
        self.version = version or f"{self.analyzer.name}-v1"

    # ============================================================ 采集

    def crawl(self, task: CrawlTask, source_name: str | None = None) -> PipelineStats:
        """采集并落库。采集层不做任何过滤 —— 全量保留。"""
        stats = PipelineStats()
        source = resolve_source(task.platform, source_name)

        contents, comments = [], []
        for record in source.crawl(task):
            if "comment_id" in record:
                comments.append(record)
            else:
                contents.append(record)

        stats.crawled_contents = len(contents)
        stats.crawled_comments = len(comments)

        stats.stored_contents = self.repo.upsert_contents(contents)
        stats.stored_comments = self.repo.upsert_comments(comments)

        # 指标快照：同内容多次采集才有多点，所以每次采集都记一次
        stats.snapshots = self.repo.add_snapshots(
            [
                {
                    "content_id": c["content_id"],
                    "snapshot_time": utcnow(),
                    "like_count": c.get("like_count"),
                    "comment_count": c.get("comment_count"),
                    "share_count": c.get("share_count"),
                }
                for c in contents
            ]
        )

        # 顺手把原始数据存一份到磁盘 —— 数据库 schema 变了也能从这儿恢复
        self._dump_raw(task, contents, comments)

        return stats

    def _dump_raw(self, task: CrawlTask, contents: list, comments: list) -> None:
        import json

        stamp = utcnow().strftime("%Y%m%d_%H%M%S")
        # task.target 可以为空 —— `cli import` 不带 --keyword 时就是 None
        # （README 里演示的正是这种用法），旧代码直接迭代 None 会抛 TypeError，
        # 让整条导入命令在数据已经落库之后崩掉。空目标用 "import" 占位。
        target = task.target or "import"
        slug = "".join(ch for ch in target if ch.isalnum() or ch in "一-鿿")[:24] or "import"
        path = Path(RAW_DIR) / f"{task.platform}_{task.mode}_{slug}_{stamp}.jsonl"
        try:
            with open(path, "w", encoding="utf-8") as f:
                for row in contents + comments:
                    # raw_json 里已经存了原始内容，这里再存一份归一化后的
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            print(f"[pipeline] 原始数据落盘失败: {e}")

    # ============================================================ 清洗

    def clean_comments(self, limit: int = 100000) -> tuple[list, list[dict], list[tuple]]:
        """规则清洗 + 去重。返回 (保留的评论, 清洗后的文本, 被淘汰的评论)。

        注意这里**不删除**任何数据库记录 —— 只是决定哪些进入分析环节。
        声量统计仍然用全量数据（被过滤的广告也是"声量"的一部分，
        只是不该计入情感和主题分析）。

        第三个返回值是 `[(comment, 原因)]`，调用方必须把它们也写进
        analysis_results（is_valid=False）。**不写就会有 bug**：
        `comments_for_analysis()` 把"没有分析结果行"当作"待分析"，
        于是被过滤掉的评论下一轮又会被取出来重新走一遍；更糟的是它的
        重复孪生兄弟已经入库，去重池里没有它，它这次反而会被当正常评论
        分析掉。每重跑一次就多放进来一批本该被过滤的评论，情感统计越跑越脏。
        """
        pending = self.repo.comments_for_analysis(self.version, limit=limit)
        if not pending:
            return [], [], []

        kept, texts, rejected = [], [], []
        spam_count = 0

        for c in pending:
            if not c.text:
                rejected.append((c, "empty"))
                continue
            cleaned = clean(c.text)
            if is_spam(cleaned):
                spam_count += 1
                rejected.append((c, "spam"))
                continue
            kept.append(c)
            texts.append(cleaned)

        # 近重复去重：同一内容被搬运/转发多次，会让声量虚高
        if len(kept) > 1:
            candidates = list(kept)  # 去重前的候选，用于反查被淘汰者
            result = dedup_mod.dedupe(
                list(zip(kept, texts)),
                key=lambda pair: pair[1],
                near=True,
                prefer_minhash=True,
            )
            pairs = result.kept
            kept = [p[0] for p in pairs]
            texts = [p[1] for p in pairs]
            # total_dropped = 精确 + 近重复。只读 len(dropped) 会漏掉精确去重那部分
            self._last_dup_count = result.total_dropped
            # 被淘汰者 = 候选 − 幸存者。不要用 result.dropped 反查：
            # 它**只装近重复**，精确重复（文本完全相同的）那条路径只记数量
            # 不留对象，靠它会漏掉大多数被删的评论。
            kept_objs = {id(c) for c in kept}
            rejected.extend((c, "duplicate") for c in candidates if id(c) not in kept_objs)
        else:
            self._last_dup_count = 0

        self._last_spam_count = spam_count
        return kept, texts, rejected

    # ============================================================ 分析

    def analyze(self, limit: int = 100000) -> PipelineStats:
        """情感分析 + 关键词抽取，批量写入 analysis_results。"""
        stats = PipelineStats()
        comments, texts, rejected = self.clean_comments(limit=limit)

        # 注意这里是 and 不是 or —— 全部被淘汰时也必须落库，
        # 否则淘汰结果丢失，下一轮又会被当成"待分析"重新取出来。
        if not comments and not rejected:
            print("[pipeline] 没有待分析的评论")
            return stats

        print(f"[pipeline] 待分析 {len(comments)} 条（已过滤广告 {self._last_spam_count} / 重复 {self._last_dup_count}）")
        stats.dropped_spam = self._last_spam_count
        stats.dropped_dup = self._last_dup_count

        rows = []
        if comments:
            results = self.analyzer.analyze_batch(texts)
            for comment, text, sent in zip(comments, texts, results):
                rows.append(
                    {
                        "item_id": comment.comment_id,
                        "item_type": "comment",
                        "analysis_version": self.version,
                        "cleaned_text": text,
                        "sentiment_label": sent.label,
                        "sentiment_score": sent.score,
                        "keywords": extract_keywords(text, top_k=8),
                        "is_valid": True,
                        "processed_at": utcnow(),
                    }
                )

        # 被淘汰的评论占一行 is_valid=False：它只用于标记"该条已处理过"，
        # 让 comments_for_analysis() 下一轮不再取到它。
        # 读侧（_comment_analysis）会把 is_valid=False 排除在统计之外，
        # 所以这些行不会污染情感分布 / 趋势 / 高频词。
        for comment, reason in rejected:
            rows.append(
                {
                    "item_id": comment.comment_id,
                    "item_type": "comment",
                    "analysis_version": self.version,
                    "cleaned_text": None,
                    "sentiment_label": None,
                    "sentiment_score": None,
                    "keywords": None,
                    "is_valid": False,
                    "is_ad": reason == "spam",
                    "processed_at": utcnow(),
                }
            )

        self.repo.save_analysis(rows)
        stats.analyzed = len(comments)  # 只统计真正分析的条数，不含淘汰
        print(
            f"[pipeline] 已写入 {len(rows)} 条分析结果"
            f"（其中有效 {len(comments)}）版本 {self.version}"
        )
        return stats

    # ============================================================ 全流程

    def run_all(self, task: CrawlTask, source_name: str | None = None) -> PipelineStats:
        """采集 + 分析一条龙。预警由调用方单独触发（快通道要独立跑）。"""
        stats = self.crawl(task, source_name)
        analysis = self.analyze()
        stats.analyzed = analysis.analyzed
        stats.dropped_spam = analysis.dropped_spam
        stats.dropped_dup = analysis.dropped_dup
        return stats
