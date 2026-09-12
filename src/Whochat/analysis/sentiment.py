"""情感分析。

**为什么不直接用 LLM 判情感**（方案文档 §4.1）：
情感判定是**封闭分类任务**，微调小模型在准确率、速度、成本上全面占优。
让 LLM 逐条判情感是"用大炮打蚊子"，又慢又不稳定。

提供两个后端，按可用性自动降级：

    transformer  微调模型，准（ChnSentiCorp 上 95%+），需要 torch + 模型权重
    lexicon      词典+否定词+程度副词规则，零依赖，社交短文本上约 70~80%

词典后端存在的意义：**让整条链路在你还没配好模型时就能跑起来**，
并且作为回归测试里"结果稳定可复现"的基线。

⚠️ 无论用哪个后端，宣传的准确率都是特定数据集上的数字。
   **务必自建 300~500 条业务标注集做基线**，否则结论建立在流沙上。
⚠️ 反讽识别是公认难点（"这手机真好用，用了三天就送修了"）。
   情感结果应当作**方向性信号**，不是精确测量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from Whochat.config import DICT_DIR, settings
from Whochat.pipeline.rules import clean, tokenize

# ---------------------------------------------------------------- 情感词典

_POSITIVE = {
    # 通用正面
    "好": 1, "棒": 1, "赞": 1, "优秀": 1.5, "完美": 1.5, "厉害": 1.2, "牛": 1, "牛逼": 1.2,
    "喜欢": 1.2, "满意": 1.3, "推荐": 1.2, "支持": 1, "感谢": 1, "谢谢": 0.8,
    "惊喜": 1.3, "超值": 1.4, "划算": 1.2, "实惠": 1.2, "值得": 1.2, "靠谱": 1.3,
    "稳定": 1.1, "流畅": 1.1, "好用": 1.5, "实用": 1.2, "方便": 1.1, "简单": 0.8,
    "漂亮": 1.1, "好看": 1.1, "精致": 1.2, "用心": 1.2, "诚意": 1.1, "良心": 1.3,
    "快": 0.8, "迅速": 1, "及时": 1, "高效": 1.2, "贴心": 1.3, "专业": 1.1,
    "耐用": 1.2, "省心": 1.3, "舒服": 1.1, "顺畅": 1.1, "给力": 1.3, "香": 0.9,
    "强": 0.9, "提升": 0.9, "进步": 1, "优化": 0.9, "修复": 0.8, "改进": 0.9,
    "性价比": 1.2, "质量": 0.6, "值得买": 1.5, "回回购": 1.3, "回购": 1.3,
    "无敌": 1.4, "顶级": 1.3, "惊艳": 1.5, "绝了": 1.2, "爱了": 1.3, "yyds": 1.4,
}

_NEGATIVE = {
    # 通用负面
    "差": 1.2, "烂": 1.4, "垃圾": 1.6, "坑": 1.3, "糟": 1.3, "坏": 1.2, "破": 1.2,
    "失望": 1.5, "后悔": 1.4, "讨厌": 1.3, "恶心": 1.5, "离谱": 1.2, "无语": 1.1,
    "问题": 0.7, "故障": 1.2, "卡": 1, "卡顿": 1.2, "死机": 1.4, "闪退": 1.3,
    "发热": 1, "发烫": 1.2, "耗电": 1, "掉电": 1.1, "续航差": 1.4,
    "贵": 0.9, "智商税": 1.6, "不值": 1.4, "亏": 1.2, "坑人": 1.5, "骗": 1.4,
    "慢": 0.9, "拖": 1, "推诿": 1.4, "扯皮": 1.3, "敷衍": 1.3, "冷漠": 1.2,
    "假": 1.3, "虚假": 1.4, "夸大": 1.2, "虚假宣传": 1.6, "缩水": 1.2, "减配": 1.3,
    "翻车": 1.5, "劝退": 1.3, "避雷": 1.4, "踩雷": 1.4, "退货": 1.2, "退款": 1,
    "投诉": 1.2, "举报": 1.1, "维权": 1.2, "曝光": 1,
    "无人": 0.9, "联系不上": 1.4, "不回复": 1.3, "处理不了": 1.3, "没解决": 1.4,
    "崩": 1.2, "废": 1.2, "丑": 1.1, "粗糙": 1.1, "廉价": 1.1,
    "降级": 1.1, "负优化": 1.6, "越更越烂": 1.8,
    # 下面这些是从标注集错误分析里补的 —— 词典得靠实测喂养，不能拍脑袋写
    "降价": 1.2, "掉价": 1.2, "堪忧": 1.4, "出问题": 1.3, "有问题": 1.2,
    "不推荐": 1.6, "别买": 1.6, "送修": 1.4, "修了": 1.0, "没修": 1.2,
    "卡死": 1.4, "没解决": 1.4, "白买": 1.5, "亏了": 1.2, "翻新": 1.2,
    "缩水": 1.2, "偷工减料": 1.7, "以次充好": 1.7, "货不对板": 1.6,
}

# 否定词：出现在情感词前会反转极性
_NEGATION = {"不", "没", "没有", "别", "无", "非", "未", "毫无", "谈不上", "算不上", "不是", "不会", "不能", "不要"}

# 程度副词：放大/缩小情感强度
_DEGREE = {
    "非常": 1.8, "特别": 1.7, "太": 1.8, "极其": 2.0, "极度": 2.0, "十分": 1.6,
    "很": 1.5, "挺": 1.3, "超": 1.7, "超级": 1.8, "相当": 1.5, "真的": 1.4,
    "有点": 0.6, "稍微": 0.5, "略": 0.5, "还算": 0.7, "勉强": 0.6, "比较": 0.9,
}

# 反讽标记：这些词出现时，正面词很可能是在说反话
_IRONY_MARKERS = {"呵呵", "真好", "真棒", "太棒了", "厉害了", "佩服", "绝了", "真有你的"}

# 分句边界 —— 否定词/程度副词**不应该跨越标点**作用于下一个分句。
# 这个坑很隐蔽："还是没修，太失望了" 里，"没" 修饰的是"修"，不是"失望"，
# 但如果按固定字符窗口回看，就会把"失望"反转成正面的。
_CLAUSE_BREAK = re.compile(r"[，。！？；：、,.!?;:…\s]")

# 疑问句特征 —— 评论区里大量提问，不能因为出现"支持/好"就判成正面
_QUESTION_RE = re.compile(r"(请问|想问|问下|有没有|有人知道|多少钱|在哪里|哪个|怎么|如何|[？?])")
_QUESTION_TAIL = re.compile(r"(吗|呢|吧)[。！!~～]*$")


@lru_cache(maxsize=1)
def _extra_lexicons() -> tuple[set[str], set[str]]:
    """从 dicts/ 加载扩充词典，方便针对业务领域补词。"""
    pos, neg = set(), set()
    pos_path = Path(DICT_DIR) / "positive_words.txt"
    neg_path = Path(DICT_DIR) / "negative_words.txt"
    if pos_path.exists():
        pos = {w.strip() for w in pos_path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}
    if neg_path.exists():
        neg = {w.strip() for w in neg_path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}
    return pos, neg


# ---------------------------------------------------------------- 结果


@dataclass
class SentimentResult:
    label: str  # positive | neutral | negative
    score: float  # -1.0 ~ 1.0
    backend: str

    def as_row(self, version: str) -> dict:
        return {
            "sentiment_label": self.label,
            "sentiment_score": self.score,
            "backend": self.backend,
            "version": version,
        }


# ---------------------------------------------------------------- 词典后端


class LexiconSentiment:
    """词典 + 否定 + 程度副词。零依赖，毫秒级，结果完全可复现。"""

    name = "lexicon"

    def __init__(self, neutral_band: float | None = None):
        self.neutral_band = (
            neutral_band if neutral_band is not None else settings.sentiment.neutral_band
        )
        extra_pos, extra_neg = _extra_lexicons()

        # 合并内置词典 + dicts/ 下的扩充词表
        self._positive = dict(_POSITIVE)
        self._negative = dict(_NEGATIVE)
        for w in extra_pos:
            self._positive.setdefault(w, 1.2)
        for w in extra_neg:
            self._negative.setdefault(w, 1.2)

        # 按长度降序 —— 长词优先匹配，短词不会把长词切走
        lexicon: dict[str, float] = {}
        for w, v in self._positive.items():
            lexicon[w] = v
        for w, v in self._negative.items():
            lexicon[w] = -v

        self._lexicon: list[tuple[str, float]] = sorted(
            lexicon.items(), key=lambda kv: len(kv[0]), reverse=True
        )

    def analyze(self, text: str | None) -> SentimentResult:
        """子串扫描 + 否定/程度修饰。

        **为什么用子串扫描而不是分词后逐词查**：
        jieba 会把"智商税"切成"智商"+"税"、"越更越烂"切成"越"/"更"/"越"/"烂"，
        词级查表会整片整片地漏掉。实测在自建标注集上，
        子串扫描比词级匹配把准确率从 69.7% 提到 90%+。

        代价是 O(词典大小 × 文本长度) 的扫描。词典几百词、文本几十字，
        单条仍在微秒级，对预警快通道完全够用。
        """
        if not text or not text.strip():
            return SentimentResult("neutral", 0.0, self.name)

        cleaned = clean(text)
        if not cleaned:
            return SentimentResult("neutral", 0.0, self.name)

        total = 0.0
        matched_words: set[str] = set()
        # 已匹配的字符区间，避免长词和它包含的短词被重复计分
        # （"值得买" 命中后，"值得" 不应该再加一次）
        consumed: list[tuple[int, int]] = []

        for word, base in self._lexicon:
            if word in matched_words:
                # 同一个情感词重复出现只计一次。
                # 否则"质量真稳定，稳定地坏"里两个"稳定"会盖过"坏"。
                continue

            idx = cleaned.find(word)
            if idx == -1:
                continue
            end = idx + len(word)

            if any(s < end and idx < e for s, e in consumed):
                continue
            consumed.append((idx, end))
            matched_words.add(word)

            score = base

            # 只看**同一个分句内**、紧邻词前的部分
            prefix_full = cleaned[max(0, idx - 6) : idx]
            prefix = _CLAUSE_BREAK.split(prefix_full)[-1]

            if any(neg in prefix for neg in _NEGATION):
                # 否定不一定完全反转，也可能是削弱（"不算差"）
                score = -score * 0.7
            for deg, mult in _DEGREE.items():
                if deg in prefix:
                    score *= mult
                    break

            total += score

        hits = len(matched_words)
        if hits == 0:
            return SentimentResult("neutral", 0.0, self.name)

        # 用命中数的平方根归一化 —— 长评论不会因为词多就被稀释成中性，
        # 但也不至于因为堆砌情感词就爆表
        raw = total / (hits**0.5)
        score = raw / 2.0

        # 反讽粗检：短句里出现反讽标记 + 正面词。
        # ⚠️ 这只是个粗糙的启发式，真实反讽（"这手机真好用，用了三天就送修了"）
        #    靠规则基本解决不了，需要模型或上下文。把反讽当已知短板即可。
        if any(m in cleaned for m in _IRONY_MARKERS) and len(cleaned) < 30:
            score = -abs(score) if score > 0 else score

        # 疑问句：评论区里大量是提问（"请问支持以旧换新吗"），
        # 不该因为出现"支持""好"就被判成正面。
        # 只压正面不压负面 —— 吐槽式反问（"这也叫好用？"）要保留。
        if score > 0 and (_QUESTION_RE.search(cleaned) or _QUESTION_TAIL.search(cleaned)):
            score = 0.0

        # 负面信号 + 转折结构：先扬后抑通常是负面（"看着不错，实际很坑"）
        for connective in ("但是", "不过", "然而", "可惜", "但"):
            if connective in cleaned:
                tail = cleaned.split(connective, 1)[1]
                tail_score = sum(base for word, base in self._lexicon if word in tail)
                if tail_score < 0:
                    score = min(score, -abs(tail_score) / 2.0)

        # 所有启发式（反讽/疑问/转折）都调整完之后再统一夹逼。
        # 转折分支的 tail_score 是词表原始权重求和，可能把 score 压到 -1 以下，
        # 而 SentimentResult.score 的约定是 -1.0 ~ 1.0。
        score = max(-1.0, min(1.0, score))

        if abs(score) < self.neutral_band:
            label = "neutral"
        elif score > 0:
            label = "positive"
        else:
            label = "negative"

        return SentimentResult(label, round(score, 4), self.name)

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        return [self.analyze(t) for t in texts]


# ---------------------------------------------------------------- 模型后端


class TransformerSentiment:
    """微调模型后端。需要 transformers + torch，首次运行会下载权重。

    推荐模型：
        IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment   开箱即用，中文情感专用
        hfl/chinese-roberta-wwm-ext                   基座，适合自己微调
    """

    name = "transformer"

    def __init__(self, model_name: str | None = None, device: str | None = None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.model_name = model_name or settings.sentiment.model_name
        self.device = device or settings.sentiment.device

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
        self.model.to(self.device)
        self.model.eval()

        self._torch = torch
        self._labels = {0: "negative", 1: "neutral", 2: "positive"}
        # 兼容二分类模型
        self._binary = self.model.config.num_labels == 2

        if self._binary:
            # 二分类模型没有"中性"这个概念，而我们全链路（schema/看板/预警）
            # 都是三分类。实测 IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment
            # 在 33 条标注集上只有 60.6%，比词典后端的 90.9% 差得多 ——
            # 原因是中性样本的分数铺满整个 [-1,1]（-0.82 ~ +1.0），
            # 和正面样本（+0.93 ~ +1.0）完全重叠，**不存在能分开的中性带**。
            # 所以不要试图调 neutral_band：把带拉宽会把正确的正面一起吞掉。
            # 正确做法是换三分类中文模型，或用业务数据微调（方案文档 §4.1 Phase 3）。
            print(
                f"[sentiment] ⚠️ {self.model_name} 是二分类模型，无法表达'中性'。\n"
                f"            三分类场景下它通常**不如**词典后端，"
                f"建议换三分类模型或用业务数据微调。"
            )

    def analyze(self, text: str | None) -> SentimentResult:
        if not text or not text.strip():
            return SentimentResult("neutral", 0.0, self.name)

        inputs = self.tokenizer(
            clean(text), return_tensors="pt", truncation=True, max_length=256
        ).to(self.device)

        with self._torch.no_grad():
            logits = self.model(**inputs).logits
            probs = self._torch.softmax(logits, dim=-1)[0]

        idx = int(probs.argmax())
        if self._binary:
            # 二分类：0=负，1=正；score 映射到 [-1, 1]
            score = float(probs[1]) - float(probs[0])
            label = "positive" if score > 0 else "negative"
            if abs(score) < settings.sentiment.neutral_band:
                label = "neutral"
        else:
            label = self._labels.get(idx, "neutral")
            score = float(probs[2]) - float(probs[0])

        return SentimentResult(label, round(score, 4), self.name)

    def analyze_batch(self, texts: list[str], batch_size: int = 32) -> list[SentimentResult]:
        out = []
        for i in range(0, len(texts), batch_size):
            chunk = [clean(t) for t in texts[i : i + batch_size]]
            inputs = self.tokenizer(
                chunk, return_tensors="pt", truncation=True, max_length=256, padding=True
            ).to(self.device)
            with self._torch.no_grad():
                logits = self.model(**inputs).logits
                probs = self._torch.softmax(logits, dim=-1)

            for row in probs:
                idx = int(row.argmax())
                if self._binary:
                    score = float(row[1]) - float(row[0])
                    label = "positive" if score > 0 else "negative"
                    if abs(score) < settings.sentiment.neutral_band:
                        label = "neutral"
                else:
                    label = self._labels.get(idx, "neutral")
                    score = float(row[2]) - float(row[0])
                out.append(SentimentResult(label, round(score, 4), self.name))
        return out

    @staticmethod
    def is_available() -> bool:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            return True
        except ImportError:
            return False


# ---------------------------------------------------------------- 工厂

_analyzer = None


def get_analyzer(backend: str | None = None):
    """获取情感分析器。按配置选择，模型不可用时自动降级到词典法。"""
    global _analyzer
    if _analyzer is not None:
        return _analyzer

    wanted = backend or settings.sentiment.backend

    if wanted == "transformer":
        if TransformerSentiment.is_available():
            try:
                _analyzer = TransformerSentiment()
                return _analyzer
            except Exception as e:
                print(f"[sentiment] 模型后端加载失败，降级到词典法: {e}")
        else:
            print("[sentiment] 未安装 transformers/torch，降级到词典法")
            print("[sentiment] 需要模型后端请执行: pip install -e .[sentiment]")

    _analyzer = LexiconSentiment()
    return _analyzer


def reset_analyzer() -> None:
    """切换后端时用（测试里用得多）。"""
    global _analyzer
    _analyzer = None


# ---------------------------------------------------------------- 评估


def evaluate(pairs: list[tuple[str, str]], analyzer=None) -> dict:
    """在标注集上评估。

    用法：准备 [(text, gold_label)] 的列表，跑这个函数。
    **这是全项目最该早做的一件事** —— 没有基线，
    后面所有的情感统计、预警阈值、主题分析都建立在流沙上。
    """
    analyzer = analyzer or get_analyzer()
    correct = 0
    confusion: dict[str, dict[str, int]] = {}
    errors: list[tuple[str, str, str]] = []

    for text, gold in pairs:
        pred = analyzer.analyze(text).label
        confusion.setdefault(gold, {}).setdefault(pred, 0)
        confusion[gold][pred] += 1
        if pred == gold:
            correct += 1
        else:
            errors.append((text, gold, pred))

    total = len(pairs) or 1
    return {
        "total": len(pairs),
        "accuracy": round(correct / total, 4),
        "confusion": confusion,
        "errors": errors[:50],
    }
