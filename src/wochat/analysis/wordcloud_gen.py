"""词云生成。

中文词云有两个必踩的坑（方案文档 §4.3）：

1. **必须指定中文字体路径** —— 否则渲染出来全是方框（默认字体没有中文字形）
2. **必须加载领域自定义词典** —— 否则"降本增效""以旧换新"这类词会被 jieba 切碎
   （词典在 dicts/user_dict.txt，由 pipeline/rules.py 加载）

优雅降级：装了 wordcloud 就渲染 PNG；没装就退化为词频列表，
看板仍然能出图（用 pyecharts 或纯表格），链路不断。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from wochat.config import EXPORT_DIR
from wochat.pipeline.rules import tokenize

# Windows / macOS / Linux 常见中文字体，按优先级找
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def find_cjk_font() -> str | None:
    """找一个能渲染中文的字体。找不到返回 None（调用方需降级）。"""
    import os

    custom = os.getenv("WOCHAT_CJK_FONT")
    if custom and Path(custom).exists():
        return custom
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


@dataclass
class WordCloudResult:
    ok: bool
    path: Path | None = None
    frequencies: dict[str, int] | None = None
    message: str = ""


def word_frequencies(
    texts: list[str],
    *,
    top_n: int = 150,
    min_len: int = 2,
    extra_stopwords: set[str] | None = None,
) -> dict[str, int]:
    """统计词频 —— 词云和主题分析共用的基础。"""
    counter: Counter = Counter()
    for text in texts:
        tokens = tokenize(text, min_len=min_len)
        if extra_stopwords:
            tokens = [t for t in tokens if t not in extra_stopwords]
        counter.update(tokens)
    return dict(counter.most_common(top_n))


def generate(
    texts: list[str],
    out_path: Path | None = None,
    *,
    top_n: int = 150,
    width: int = 1200,
    height: int = 800,
    background: str = "white",
    mask_image: Path | None = None,
) -> WordCloudResult:
    """生成词云 PNG。失败时返回词频，调用方可以降级展示。"""
    freqs = word_frequencies(texts, top_n=top_n)
    if not freqs:
        return WordCloudResult(False, frequencies={}, message="没有足够的文本生成词云")

    try:
        from wordcloud import WordCloud
    except ImportError:
        return WordCloudResult(
            False,
            frequencies=freqs,
            message="未安装 wordcloud，已返回词频。安装: pip install -e .[viz]",
        )

    font = find_cjk_font()
    if font is None:
        return WordCloudResult(
            False,
            frequencies=freqs,
            message="未找到中文字体，渲染会出现方框。请设置环境变量 WOCHAT_CJK_FONT 指向字体文件",
        )

    out_path = Path(out_path or (EXPORT_DIR / "wordcloud.png"))

    kwargs: dict = {
        "font_path": font,
        "width": width,
        "height": height,
        "background_color": background,
        "max_words": top_n,
        "prefer_horizontal": 0.9,
        "collocations": False,  # 中文已经分好词，不要再做二元组搭配
    }

    if mask_image and Path(mask_image).exists():
        import numpy as np
        from PIL import Image

        kwargs["mask"] = np.array(Image.open(mask_image))

    try:
        wc = WordCloud(**kwargs).generate_from_frequencies(freqs)
        wc.to_file(str(out_path))
        return WordCloudResult(True, path=out_path, frequencies=freqs, message=f"已生成 {out_path}")
    except Exception as e:
        return WordCloudResult(False, frequencies=freqs, message=f"词云渲染失败: {e}")


def export_frequencies(freqs: dict[str, int], out_path: Path | None = None) -> Path:
    """导出词频 JSON —— 让前端可以自己用 ECharts 画交互式词云。"""
    out_path = Path(out_path or (EXPORT_DIR / "word_frequencies.json"))
    out_path.write_text(json.dumps(freqs, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
