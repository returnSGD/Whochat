"""主题建模（BERTopic）。

选型理由（方案文档 §4.2）：BERTopic 语义强、自动定主题数、短文本友好。

流程：语义向量编码(SBERT) → UMAP 降维 → HDBSCAN 聚类 → c-TF-IDF 关键词抽取

⚠️ **两个踩坑点**：
1. **文档数 < 1000 时效果可能很差** —— 小样本场景建议改用关键词规则 + 人工分类
2. **评论是短文本**，逐条建模会得到一堆碎片主题。正确做法是先按 content_id
   **聚合**成"一篇文档"再建模。

`-1` 主题是 HDBSCAN 的离群点，评论场景下占比可能很高，需要调 min_cluster_size。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from Whochat.config import EXPORT_DIR
from Whochat.pipeline.rules import stopwords, tokenize


@dataclass
class TopicInfo:
    topic_id: int
    label: str
    keywords: list[str]
    doc_count: int
    rep_docs: list[str] = field(default_factory=list)


@dataclass
class TopicResult:
    ok: bool
    topics: list[TopicInfo] = field(default_factory=list)
    outlier_count: int = 0
    message: str = ""
    doc_topic: list[int] = field(default_factory=list)


def _jieba_tokenizer(text: str) -> list[str]:
    return tokenize(text, min_len=2)


def is_available() -> tuple[bool, str]:
    try:
        import bertopic  # noqa: F401
        import umap  # noqa: F401
        import hdbscan  # noqa: F401

        return True, "ok"
    except ImportError as e:
        return False, (
            f"未安装主题建模依赖 ({e.name})。"
            "安装: pip install bertopic umap-learn hdbscan"
        )


# 中文嵌入模型候选，按优先级尝试。
# ⚠️ 必须写全名。只写 "text2vec-base-chinese" 会被解析成
#    sentence-transformers/text2vec-base-chinese —— 那个仓库不存在，
#    报 401 RepositoryNotFoundError。
EMBEDDING_CANDIDATES = [
    "shibing624/text2vec-base-chinese",
    "paraphrase-multilingual-MiniLM-L12-v2",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
]


def model_topics(
    docs: list[str],
    *,
    min_topic_size: int = 3,
    embedding_model: str | None = None,
    top_k_keywords: int = 8,
) -> TopicResult:
    """对文档集合做主题建模。

    Args:
        docs: 文档列表。**评论请先按 content_id 聚合**再传进来。
        min_topic_size: 最小簇大小。评论量大时可调大以合并碎片主题。
        embedding_model: 中文建议用 text2vec-base-chinese 或
                         paraphrase-multilingual-MiniLM-L12-v2。
    """
    ok, msg = is_available()
    if not ok:
        return TopicResult(False, message=msg)

    # 文档太少时 BERTopic 会退化成一堆单文档主题，没有意义
    if len(docs) < 20:
        return TopicResult(
            False,
            message=f"文档数只有 {len(docs)} 条，太少（建议 ≥100）。小样本请改用关键词规则分类。",
        )

    try:
        from bertopic import BERTopic
        from sklearn.feature_extraction.text import CountVectorizer

        vectorizer = CountVectorizer(
            tokenizer=_jieba_tokenizer,
            ngram_range=(1, 1),
            # 把停用词直接交给 CountVectorizer，避免污染 c-TF-IDF
            stop_words=list(stopwords()),
        )

        # 文档少时自动缩小簇 —— 固定 min_topic_size=5 在 30 篇文档上
        # 会把几乎所有内容都归成离群点
        effective_min = min(min_topic_size, max(2, len(docs) // 10))

        base_kwargs: dict = {
            "vectorizer_model": vectorizer,
            "min_topic_size": effective_min,
            "calculate_probabilities": False,
            "verbose": False,
        }

        # 嵌入模型降级链：中文模型 → 多语言 → BERTopic 默认。
        # 错误发生在 fit_transform（真正加载权重时），不是构造时，
        # 所以必须把 fit 一起放进重试里，只 try 构造是抓不到的。
        candidates = [embedding_model] if embedding_model else list(EMBEDDING_CANDIDATES)
        candidates.append(None)  # None = 用 BERTopic 默认

        model = None
        topics = None
        last_error = None

        for candidate in candidates:
            try:
                kwargs = dict(base_kwargs)
                if candidate:
                    kwargs["embedding_model"] = candidate
                model = BERTopic(**kwargs)
                topics, _ = model.fit_transform(docs)
                if candidate:
                    print(f"[topics] 嵌入模型: {candidate}")
                break
            except Exception as e:
                last_error = e
                hint = ""
                if "RepositoryNotFound" in type(e).__name__ or "Repository Not Found" in str(e):
                    hint = "（模型名写错了，或需要设置 HF_ENDPOINT=https://hf-mirror.com）"
                print(f"[topics] {candidate or '默认模型'} 不可用: {type(e).__name__}{hint}")
                model = None

        if model is None or topics is None:
            # 嵌入模型全挂了（断网、模型名错、HF 被墙）——
            # 退到**零下载**的 TF-IDF + SVD + HDBSCAN 路线。
            # 效果不如 BERTopic，但本地工具不该因为拉不到模型就整个功能报废。
            print(f"[topics] 所有嵌入模型不可用（{last_error}），改用离线方案")
            return _offline_topics(docs, effective_min, top_k_keywords)

    except Exception as e:
        return TopicResult(False, message=f"主题建模失败: {type(e).__name__}: {e}")

    infos: list[TopicInfo] = []
    outlier_count = 0

    for tid in set(topics):
        if tid == -1:
            outlier_count = topics.count(-1)
            continue
        try:
            words = [w for w, _ in model.get_topic(tid)][:top_k_keywords]
        except Exception:
            words = []
        count = topics.count(tid)

        # 取该主题下单条文档长度适中的作为代表（太短的没有信息量）
        rep_docs = []
        for doc, t in zip(docs, topics):
            if t == tid and 15 <= len(doc) <= 120:
                rep_docs.append(doc)
                if len(rep_docs) >= 3:
                    break

        infos.append(
            TopicInfo(
                topic_id=int(tid),
                label=" / ".join(words[:3]) if words else f"主题{tid}",
                keywords=words,
                doc_count=count,
                rep_docs=rep_docs,
            )
        )

    infos.sort(key=lambda t: t.doc_count, reverse=True)

    return TopicResult(
        ok=True,
        topics=infos,
        outlier_count=outlier_count,
        doc_topic=[int(t) for t in topics],
        message=f"共 {len(infos)} 个主题，{outlier_count} 条未归类",
    )


def _offline_topics(docs: list[str], min_topic_size: int, top_k_keywords: int) -> TopicResult:
    """零下载的主题建模：TF-IDF → SVD(LSA) → HDBSCAN → 簇内 TF-IDF 关键词。

    本质是 LSA + 聚类，语义能力弱于 BERTopic（识别不了"汽车/车辆"同义），
    但**不需要任何模型权重**，断网也能跑。

    这是刻意的降级设计：主题建模不该因为拉不到嵌入模型就整个功能报废。
    有网时会自动优先用 BERTopic。
    """
    try:
        from sklearn.cluster import HDBSCAN
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        vectorizer = TfidfVectorizer(
            tokenizer=_jieba_tokenizer,
            ngram_range=(1, 1),
            stop_words=list(stopwords()),
            max_features=5000,
        )
        matrix = vectorizer.fit_transform(docs)
        if matrix.shape[1] < 2:
            return TopicResult(False, message="词汇量太少，无法建模（文本是否过短？）")

        # SVD 降维到稠密向量 —— 这就是"没有嵌入模型时的嵌入"
        #
        # ⚠️ 主成分数必须远小于文档数。取 50 个主成分 / 57 篇文档时
        # 解释方差高达 0.999，等于没降维 —— 保留全部噪声，
        # HDBSCAN 在高维稀疏空间里会把每个点都判成离群。
        # 取 10~15 个主成分才有真正的低秩近似和去噪效果。
        n_components = max(2, min(15, len(docs) // 4, matrix.shape[1] - 1))
        if n_components < 2:
            return TopicResult(False, message="文档太少，无法降维")
        dense = TruncatedSVD(n_components=n_components, random_state=42).fit_transform(matrix)

        # 离线方案用小得多的簇阈值：TF-IDF+SVD 的向量分布比神经嵌入扁平，
        # 沿用 BERTopic 的 min_cluster_size 会把所有点都判成离群
        labels = HDBSCAN(
            min_cluster_size=max(2, min(min_topic_size, max(2, len(docs) // 15))),
            min_samples=1,
            allow_single_cluster=False,
        ).fit_predict(dense)

        terms = vectorizer.get_feature_names_out()
        infos: list[TopicInfo] = []
        outlier_count = int((labels == -1).sum())

        for tid in sorted(set(labels)):
            if tid == -1:
                continue
            member_idx = [i for i, lb in enumerate(labels) if lb == tid]
            # 簇内平均 TF-IDF 最高的词就是主题词
            mean_tfidf = matrix[member_idx].mean(axis=0).A1
            top_idx = mean_tfidf.argsort()[::-1][:top_k_keywords]
            words = [terms[i] for i in top_idx if mean_tfidf[i] > 0]

            rep_docs = [docs[i] for i in member_idx if 15 <= len(docs[i]) <= 120][:3]
            infos.append(
                TopicInfo(
                    topic_id=int(tid),
                    label=" / ".join(words[:3]) if words else f"主题{tid}",
                    keywords=words,
                    doc_count=len(member_idx),
                    rep_docs=rep_docs,
                )
            )

        infos.sort(key=lambda t: t.doc_count, reverse=True)
        return TopicResult(
            ok=True,
            topics=infos,
            outlier_count=outlier_count,
            doc_topic=[int(x) for x in labels],
            message=(
                f"离线方案（TF-IDF+SVD+HDBSCAN）：{len(infos)} 个主题，"
                f"{outlier_count} 条未归类。语义能力弱于 BERTopic，联网后可重跑提升。"
            ),
        )
    except ImportError as e:
        return TopicResult(False, message=f"离线方案依赖缺失: {e}（需要 scikit-learn）")
    except Exception as e:
        return TopicResult(False, message=f"离线主题建模失败: {type(e).__name__}: {e}")


def aggregate_by_content(repo, version: str, keyword: str | None = None) -> tuple[list[str], list[str]]:
    """把评论按 content_id 聚合成文档 —— 短文本主题建模的正确姿势。

    返回 (文档列表, 对应的 content_id 列表)，两者一一对应。

    只取**该版本下被判定有效**的评论分析结果：被清洗规则淘汰的广告/近重复
    （is_valid=False）和从未分析过的评论都不该进入主题建模，否则聚出来的
    是全量原始评论，主题对应的不是调用方请求的 analysis_version。
    """
    from collections import defaultdict

    from sqlalchemy import select

    from Whochat.store.models import AnalysisResult, Comment, RawContent
    from Whochat.store.repository import _comment_analysis

    # 复用 repository 的过滤口径（item_type == "comment" 且 is_valid IS NOT FALSE），
    # 不在这里重写 SQL，避免两处口径漂移后统计结果对不上
    stmt = (
        select(Comment)
        .join(AnalysisResult, AnalysisResult.item_id == Comment.comment_id)
        .where(
            AnalysisResult.analysis_version == version,
            Comment.text.isnot(None),
            Comment.text != "",
        )
    )
    stmt = _comment_analysis(stmt)
    if keyword:
        sub = select(RawContent.content_id).where(RawContent.search_keyword == keyword)
        stmt = stmt.where(Comment.content_id.in_(sub))

    buckets: dict[str, list[str]] = defaultdict(list)
    for c in repo.session.scalars(stmt):
        if c.text:
            buckets[c.content_id].append(c.text)

    contents = repo.content_map(list(buckets.keys()))

    doc_ids, docs = [], []
    for cid, texts in buckets.items():
        # 文档 = 标题 + 正文 + 评论。
        #
        # 为什么必须带上正文：评论池在同一次舆情里高度同质（都在说同一件事），
        # 只用评论拼文档会导致所有文档向量几乎相同，主题建模得到 0 个簇。
        # 正文才是区分"这篇在讨论质量 / 那篇在讨论售后"的信号。
        parts = []
        content = contents.get(cid)
        if content:
            if content.title:
                parts.append(content.title)
            if content.body_text:
                parts.append(content.body_text)
        # 评论最多取 20 条，避免评论量把正文淹掉
        parts.extend(texts[:20])

        joined = "。".join(parts)
        if len(joined) >= 30:
            doc_ids.append(cid)
            docs.append(joined)
    return docs, doc_ids


def export_result(result: TopicResult, path: Path | None = None) -> Path | None:
    """导出主题结果供看板使用。"""
    import json

    if not result.ok:
        return None
    path = Path(path or (EXPORT_DIR / "topics.json"))
    payload = [
        {
            "topic_id": t.topic_id,
            "label": t.label,
            "keywords": t.keywords,
            "doc_count": t.doc_count,
            "rep_docs": t.rep_docs,
        }
        for t in result.topics
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
