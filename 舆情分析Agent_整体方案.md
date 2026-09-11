# 舆情分析 Agent —— 整体技术方案

> 版本：v1.0
> 日期：2026-09-11
> 目标：构建一套本地部署的舆情分析工具，覆盖「多平台采集 → 本地模型清洗 → 自动化分析 → 可视化看板 + 预警推送」全链路。

---

## 0. 摘要

本方案的核心判断：

1. **数据获取是整个项目最难、风险最高的环节**，占总工程量约 60%。方案以 **MediaCrawler** 为采集基座（浏览器自动化路线，零 JS 逆向）。
2. **"数据中台"不做真的中台**，落地为 `SQLite/PostgreSQL + FastAPI + Web 看板`。但要保留存储层与展示层的边界。
3. **预警走快慢双通道**，不等本地模型清洗完成。
4. **采集层抓全字段，分析层可重跑**——采集不可逆，分析可迭代。
5. 本地模型承担**清洗 + 结构化**，不承担核心情感判定（用小模型微调更划算）。

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│  L1 采集层                                                         │
│  MediaCrawler (Playwright/CDP) ── 抖音/小红书/B站/微博/快手/知乎/贴吧  │
│  代理池 · 限速器 · 断点续爬 · 原始JSON全字段落盘                        │
└───────────────────────────┬──────────────────────────────────────┘
                            │ raw JSONL
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  L2 清洗层                                                         │
│  规则清洗(去噪/繁简/URL/表情) → 精确去重(SHA256) → 近重复(MinHash+LSH)  │
│  → 本地LLM结构化(Ollama+Qwen, 受约束解码) → 主体识别/标签             │
└───────────────────────────┬──────────────────────────────────────┘
                            │ 结构化记录
              ┌─────────────┴─────────────┐
              ▼ (快通道 · 秒级)            ▼ (慢通道 · 分钟~小时)
┌──────────────────────────┐  ┌────────────────────────────────────┐
│ L3a 规则引擎              │  │ L3b 分析引擎                        │
│ 敏感词/情绪词/增速突变     │  │ 情感分类 · 主题建模(BERTopic)        │
│ 无模型，纯规则            │  │ 词云 · 时序聚合 · 传播指标           │
└──────────┬───────────────┘  └──────────────┬─────────────────────┘
           │                                 │
           └────────────┬────────────────────┘
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  L4 存储层 (事实上的"中台")                                        │
│  PostgreSQL / SQLite · 统一读写接口 · 分析版本号可重跑               │
└──────────┬────────────────────────────────────┬──────────────────┘
           ▼                                    ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ L5 预警分发               │   │ L6 可视化看板                     │
│ 聚合窗口·分级·冷却        │   │ FastAPI + 前端 · localhost:6666   │
│ → 企业微信机器人           │   │ 趋势/词云/主题/情感/事件流          │
└──────────────────────────┘   └──────────────────────────────────┘
                        ▲
┌──────────────────────────────────────────────────────────────────┐
│  L0 编排层: APScheduler (MVP) → Prefect (规模化后)                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. L1 采集层（重点）

### 2.1 核心选型：MediaCrawler

| 项 | 内容 |
|---|---|
| 仓库 | `NanmiCoder/MediaCrawler` |
| Star | 约 5.2 万（2026-06 统计），中文社媒爬虫事实标准 |
| 技术路线 | **Playwright + CDP 连接本机真实 Chrome**，在浏览器上下文内直接执行平台自身的 JS 签名函数 |
| 覆盖平台 | 小红书、抖音、快手、B站、微博、贴吧、知乎 |
| 采集模式 | 关键词搜索 / 指定内容ID / 创作者主页 |
| 评论 | 支持一级 + **二级评论**（`ENABLE_GET_SUB_COMMENTS`，默认可能关闭） |
| 登录 | 二维码 / Cookie / 手机号，登录态缓存 |
| 存储 | CSV / JSON / JSONL / Excel / SQLite / MySQL / PostgreSQL / MongoDB |
| 附加 | IP 代理池、词云生成、FastAPI WebUI（默认 8080） |

**为什么选它而不是自己写爬虫**：抖音 `a_bogus` 是 JSVMP 混淆 + 严格环境检测 + 数月一次升级，小红书 `x-s` 是中等难度但涉及路径+参数的哈希组合与排序。纯 `requests` 抓评论只会拿到骨架 HTML。MediaCrawler 用浏览器执行平台自己的签名函数，**零逆向、不随加密更新而失效**。

#### 备选 / 补充项目

| 项目 | 用途 | 备注 |
|---|---|---|
| `666ghj/BettaFish` → `MindSpider` | 话题发现 + 7 平台自动化抓取 | 用 DeepSeek 做热点话题提取，Playwright 抓取；可作为"抓什么"的决策层与 MediaCrawler 互补 |
| `dataabc/weiboSpider` | 微博专项 | 老牌，适合作为微博的备份通道 |
| `NanmiCoder/MediaCrawler` Pro 版 | 断点续爬、多账号、验证码绕过、Linux 守护 | 付费。**如果长期跑，这个钱值得花** |
| `yt-dlp` | 视频元数据/字幕 | 若将来要补视频侧信息 |

### 2.2 平台反爬难度与应对

| 平台 | 核心签名 | 难度 | 更新频率 | 应对 |
|---|---|---|---|---|
| 抖音 | `a_bogus` + `mstoken` / `X-Bogus` / `X-SS-STUB` | **高**（JSVMP 混淆、环境检测严、与请求上下文强绑定） | 数月一升级 | 浏览器自动化 + CDP 复用登录态 |
| 小红书 | `x-s` / `x-t` / `x-mini-wua` / `x-sec-sdk-token` | 中（算法较清晰，但哈希组合 + 参数排序） | 中等 | 同上 |
| 快手 | `__NS_sig3` / `kpn_id` | 中高 | 中高 | 同上 |
| B站 | 相对宽松 | 低 | 低 | 常规请求即可，注意频率 |
| 微博 | Cookie 态 + 频控 | 中 | 中 | 登录态 + 限速 |
| 知乎 | `d_c0` 等 | 中 | 中 | 登录态 + 限速 |

**通用风控硬约束**（即使签名绕过也仍然会撞到）：
- User-Agent / TLS 指纹 / 设备特征校验
- 滑块验证码（403 / 412 是常态）
- IP 与账号限流、封禁

**结论**：预期管理要放低——**单平台单次采集成功率 70~90% 就算健康**，必须有断点续爬。

### 2.3 代理配置（重要，容易踩坑）

你开的系统代理 **7897**（Clash Verge / Mihomo 系列常见混合端口）——**它的用途和爬虫要分开看**：

```
7897 代理的真实用途：
  ✅ 访问 GitHub、HuggingFace 下载模型权重
  ✅ pip / npm 安装依赖
  ✅ Ollama 拉取模型
  ❌ 不要用于国内平台爬取
```

**关键判断：国内平台（抖音/小红书/微博/B站）爬取不要走代理。** 原因：

1. 平台风控对**机房 IP / 代理 IP 特征**识别很准，走代理反而更容易触发 403/滑块
2. 代理节点出口地域跳变（这次上海、下次洛杉矶）会与账号登录态冲突，直接触发风控
3. 浏览器自动化的核心优势是"像真人"，挂代理等于自己抹掉这个优势

正确分工：

| 流量类型 | 走不走 7897 | 说明 |
|---|---|---|
| 抖音/小红书/微博等采集 | ❌ 直连 | 用本机真实 IP + 真实 Chrome 登录态 |
| GitHub / HuggingFace / pip / Ollama | ✅ 走 7897 | 环境变量或工具配置 |
| 平台风控规避 | 用**国内住宅代理池**（付费） | 不是 Clash 这种境外节点 |

环境变量参考：

```bash
# ~/.bashrc 或项目 .env —— 仅给"下载/安装"类工具用
export HTTP_PROXY=http://127.0.0.1:7897
export HTTPS_PROXY=http://127.0.0.1:7897
export NO_PROXY=localhost,127.0.0.1,::1

# HuggingFace 国内镜像（比代理更快更稳）
export HF_ENDPOINT=https://hf-mirror.com

# Playwright 浏览器：显式关闭代理，避免继承系统代理
# （在 browser.new_context(proxy={"server": "direct://"}) 或启动参数中处理）
```

> ⚠️ 注意：Playwright 默认**不继承**系统代理设置，但如果你在环境变量里设了 `HTTP_PROXY`，部分库会读取。**采集进程和下载进程要用不同的环境变量作用域**，建议采集用独立的 venv / 启动脚本，显式 `unset HTTP_PROXY`。

#### 代理池（用于规模化后的反封禁）

| 项目 | 说明 |
|---|---|
| `jhao104/proxy_pool` | 14K+ star，Python，Redis 存储，提供 `/get` `/pop` `/all` `/count` `/delete` API，可扩展 `fetcher/sources/` |
| `Ronchy2000/Dynamic-Proxy-Pool` | 基于 Mihomo 的动态切换，泊松分布间隔 + 无头浏览器反检测 |

> ⚠️ **免费代理池的现实**：可用率低、速度慢、生命周期短，对严肃项目远远不够。若真的需要，买**国内住宅代理**。

### 2.4 采集字段清单（**一次定死，避免返工**）

> 原则：**采集是全量的、不可逆的；分析是增量、可重跑的。别在采集层做减法。**
> 社媒历史数据重爬成本极高，甚至不可能（内容已删）。

#### 表 `raw_content`（内容主体）

| 字段 | 类型 | 说明 |
|---|---|---|
| `content_id` | TEXT PK | 平台内唯一 ID |
| `platform` | TEXT | douyin / xhs / bilibili / weibo / kuaishou / zhihu / tieba |
| `content_type` | TEXT | video / note / post / article / answer |
| `title` | TEXT | 标题 |
| `body_text` | TEXT | 正文 / 视频文案 / 笔记正文 |
| `url` | TEXT | 原始链接 |
| `publish_time` | TIMESTAMP | **原始时间戳，保留时区** |
| `crawl_time` | TIMESTAMP | 采集时刻 |
| `author_id` | TEXT | **哈希脱敏后** |
| `author_name` | TEXT | 昵称 |
| `author_follower_count` | INTEGER | 粉丝数（判断影响力） |
| `author_verified` | BOOLEAN | 是否认证 |
| `like_count` | INTEGER | 抓取时刻快照 |
| `comment_count` | INTEGER | 抓取时刻快照 |
| `share_count` | INTEGER | 抓取时刻快照 |
| `collect_count` | INTEGER | 抓取时刻快照 |
| `parent_content_id` | TEXT | **转发/引用上游 ID ← 传播路径的命根子** |
| `search_keyword` | TEXT | 命中的监控词（归因用） |
| `raw_json` | JSONB | **原始 JSON 全量存档（兜底，字段漏了就靠它）** |

#### 表 `comments`（评论）

| 字段 | 类型 | 说明 |
|---|---|---|
| `comment_id` | TEXT PK | 评论 ID |
| `content_id` | TEXT FK | 所属内容 |
| `platform` | TEXT | |
| `parent_comment_id` | TEXT | 一级评论归属（二级评论用） |
| `reply_to_comment_id` | TEXT | 回复目标 ← 对话结构 |
| `level` | INTEGER | 1 = 一级，2 = 二级 |
| `text` | TEXT | 评论文本 |
| `publish_time` | TIMESTAMP | |
| `crawl_time` | TIMESTAMP | |
| `author_id` | TEXT | 哈希脱敏 |
| `author_follower_count` | INTEGER | |
| `like_count` | INTEGER | |
| `reply_count` | INTEGER | |
| `ip_location` | TEXT | 平台自带 IP 属地（若有） |
| `raw_json` | JSONB | 原始存档 |

#### 表 `metric_snapshots`（时序快照 ← 画传播曲线用）

| 字段 | 说明 |
|---|---|
| `content_id` / `snapshot_time` | 联合主键 |
| `like_count` / `comment_count` / `share_count` | 该时刻的指标值 |

> 有了这张表才能画**传播曲线**（何时起爆、何时到峰、增速拐点）。同一内容多次采集 = 多个快照点。**不采这张表，传播分析永远做不了。**

### 2.5 采集层接口抽象（成本几乎为零，收益极大）

```python
# crawler/base.py
from typing import Protocol, Iterator
from dataclasses import dataclass

@dataclass
class CrawlTask:
    platform: str
    mode: str              # keyword | content_id | creator
    target: str            # 关键词 / ID / 主页
    max_items: int = 1000
    include_sub_comments: bool = True

class CrawlerSource(Protocol):
    """所有采集后端实现此协议。
    将来从 爬虫 → 授权API → 采购数据源 时，分析层一行不用改。
    """
    def supports(self, platform: str) -> bool: ...
    def crawl(self, task: CrawlTask) -> Iterator[dict]: ...
```

实现：
- `MediaCrawlerSource` —— 主力
- `BettaFishiMindSpiderSource` —— 话题发现
- `OfficialAPISource` —— 占位，将来接抖音/微博开放平台
- `ManualImportSource` —— 人工导入 CSV（应急兜底）

---

## 3. L2 清洗层

### 3.1 清洗流水线（按"从廉价到昂贵"排序）

```
原始 JSONL
  │
  ├─ ① 规则清洗（无模型，毫秒级）
  │    去 URL / @提及 / 表情符号 / HTML标签 / 零宽字符
  │    繁→简、全角→半角、连续空白归一
  │    广告/水军启发式过滤（重复模板、无意义字符占比）
  │
  ├─ ② 精确去重 —— SHA256(规范化文本)
  │
  ├─ ③ 近重复检测 —— MinHash + LSH
  │    跨平台转载识别（同一内容在微博/抖音/小红书反复出现）
  │
  ├─ ④ 本地 LLM 结构化（Ollama + Qwen）
  │    输出受约束的 JSON：主体 / 情感初判 / 关键词 / 是否广告
  │
  └─ ⑤ 落库
```

### 3.2 去重组件

| 方案 | 项目 | 适用 |
|---|---|---|
| **MinHash + LSH** | `ekzhu/datasketch` | 近重复主力。`MinHashLSH(threshold=0.8, num_perm=128)` |
| **SimHash** | `1e0ng/simhash` / text-dedup 内置 | 位级、极快，适合网页模板/样板内容 |
| **全套去重脚本** | `ChenQianll/text-dedup` | 集合了 MinHash / SimHash / SuffixArray / BloomFilter / ExactHash，含 Spark 实现 |
| **大规模流水线** | HuggingFace `datatrove` | TB 级；四阶段：签名 → 分桶 → 并查集聚类 → 过滤 |

**推荐参数**（datasketch 路线）：

```python
from datasketch import MinHash, MinHashLSH

lsh = MinHashLSH(threshold=0.8, num_perm=128)   # Jaccard ≥ 0.8 判为近重复
# 中文用字符级 3-gram 做 shingle，比词级更适合短评论文本
```

> 网络抓取的数据通常含 **5~30% 的近重复**。不做去重，后面的情感统计和主题建模全部失真。

### 3.3 本地模型清洗

| 组件 | 选型 | 理由 |
|---|---|---|
| 推理引擎 | **Ollama**（MVP）→ **vLLM**（规模化） | Ollama 开箱即用；vLLM 有 PagedAttention + 连续批处理，高并发吞吐高 |
| 模型 | **Qwen2.5-14B-Instruct** | 中文强；**JSON 可靠性 Excellent**（专为结构化输出训练）。7B 为 Good，再小容易出非法语法 |
| 量化 | `q4_K_M` / `q5_K_M`（GGUF） | 14B q4_K_M 约 9GB，消费级显卡可跑 |
| 结构化保障 | **Outlines**（受约束解码）/ **Instructor**（校验重试） | Outlines 在采样阶段屏蔽非法 token，100% 合法；**但不支持 Ollama**，用 Ollama 时选 Instructor |
| 脏 JSON 兜底 | `json_repair` | 修复被截断/污染的 JSON |

**结构化输出方案对比**：

| 方法 | 可靠性 | 说明 |
|---|---|---|
| 原生 Function Calling | 高 | Qwen / Llama 3.1+ 支持 |
| **文法约束解码** | **最高** | Outlines / vLLM / llama.cpp，token 级屏蔽非法输出 |
| JSON Mode | 中 | Ollama `format: "json"`，保证合法但不约束结构 |
| 纯提示词 | 最低 | **<7B 模型在复杂 schema 下经常失败，别用** |

**工程稳定性要点**：
- system prompt 统一且位置固定
- 锁定 `temperature=0` / 固定 `top_p` / 传随机种子
- 落库时**保留原始输出**（便于排查）
- 增加"输出格式校验"中间层
- 维护一组固定回归测试题集，每次改 prompt 跑一遍

**清洗用的结构化 schema 示例**：

```json
{
  "is_ad": false,
  "is_valid": true,
  "subject": "某品牌X型号手机",
  "sentiment_hint": "negative",
  "keywords": ["发热", "续航"],
  "noise_reason": null
}
```

> ⚠️ **重要分工**：本地 LLM 只做**清洗 + 打标**，**不做最终情感判定**。情感判定交给专门微调的小模型（见 §4.1），又快又准。让 LLM 逐条判情感是"用大炮打蚊子"，成本高且不稳定。

**吞吐量预期**：Ollama + Qwen2.5-14B-q4 单卡 4090 约 **15~30 条/秒**（短文本）；vLLM 批处理可提升 5~10 倍。1 万条评论约 3~10 分钟。

---

## 4. L3 分析层

### 4.1 情感分析

**为什么不用 LLM**：情感分析是**封闭分类任务**，微调小模型在准确率、速度、成本上全面占优。

#### 基准数据（ChnSentiCorp 二分类，来自 `qhduan/Chinese-BERT-wwm`）

| 模型 | 开发集 | 测试集 |
|---|---|---|
| BERT | 94.7 | 95.0 |
| ERNIE | 95.4 | 95.4 |
| BERT-wwm | 95.1 | 95.4 |
| BERT-wwm-ext | 95.4 | 95.3 |
| RoBERTa-wwm-ext | 95.0 | 95.6 |
| **RoBERTa-wwm-ext-large** | **95.8** | **95.8** |

#### 微博口语场景（22,440 条统一测试集）

| 模型 | Accuracy | Macro-F1 |
|---|---|---|
| SVM (TF-IDF) | 82.31 | 81.95 |
| TextCNN | 87.65 | 87.32 |
| BERT-Base | 92.47 | 92.15 |
| ERNIE-1.0 | 93.02 | 92.78 |
| BERT-BiLSTM-Attn | **94.23** | **93.87** |

#### 可直接用的现成模型

| 模型 | 说明 |
|---|---|
| `hfl/chinese-roberta-wwm-ext` | 基座首选，中文通用最强 |
| `IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment` | 中文情感专用，开箱可用 |
| `foxyanuo/chinese-chat-sentiment-8class` | 对话场景**8 分类**，验证集 98.86%，提供 ONNX FP16 |
| `hellonlp/sentiment-analysis` | 多实现合集（词典/Bayes/ALBERT/TextCNN），适合快速对比 |
| `kayzhou/Guba-emotion` | 金融股吧情绪（若涉金融场景） |
| SnowNLP | 词典法，**仅作 baseline** |

#### 选型建议

```
MVP 阶段：直接调 Erlangshen-Roberta-110M-Sentiment（开箱即用）
进阶：用业务标注数据微调 hfl/chinese-roberta-wwm-ext
  - 冻结上游 + 仅训分类头 → ChnSentiCorp 上约 92.35%，便宜
  - 全量微调 → 可达 95%+
输出：三分类标签 + 概率分数（留分数便于后续排序和阈值调优）
```

> ⚠️ **所有宣传的 95%+ 准确率都是特定数据集上的**。**务必自建 300~500 条业务标注集做基线**，否则后面所有分析结论都建立在流沙上。
>
> ⚠️ **反讽识别**是公认难点（"这手机真好用，用了三天就送修了"）。情感结果应当作**方向性信号**，不是精确测量。

### 4.2 主题建模

**选型：BERTopic**（`MaartenGr/BERTopic`）

流程：语义向量编码（SBERT）→ UMAP 降维 → HDBSCAN 聚类 → c-TF-IDF 关键词抽取。

| 方案 | 优势 | 劣势 | 适用 |
|---|---|---|---|
| **BERTopic** | 语义强、自动定主题数、短文本友好、支持动态/引导式主题 | 慢（需 GPU 更佳）、每文档单主题 | **本项目首选** |
| Top2Vec | 主题词取质心近词，连贯性好、无需停用词 | 同上，功能变体少于 BERTopic | 备选 |
| LDA | 快、可混合主题、成熟 | 语义弱（识别不了"汽车/车辆"同义）、短文本差、需指定 K | 长文档 / 需混合主题分布时 |

**中文配置要点**：

```python
from bertopic import BERTopic
from sklearn.feature_extraction.text import CountVectorizer
import jieba

def jieba_tokenizer(text):
    return [w for w in jieba.cut(text) if w not in STOPWORDS and len(w) > 1]

vectorizer = CountVectorizer(tokenizer=jieba_tokenizer, ngram_range=(1, 1))

topic_model = BERTopic(
    embedding_model="text2vec-base-chinese",   # 或 paraphrase-multilingual-MiniLM-L12-v2
    vectorizer_model=vectorizer,
    language="chinese",
    calculate_probabilities=True,
)
topics, probs = topic_model.fit_transform(docs)
```

**注意事项**：
- `-1` 主题 = 离群点，评论场景下可能占比很高，需要调节 HDBSCAN 的 `min_cluster_size` / `min_samples`
- **文档数 < 1000 时效果可能不佳** —— 小样本场景建议改用**关键词规则 + 人工主题分类**
- 评论是短文本，建议先按 `content_id` **聚合**再建模，而不是逐条评论建模
- 中文参考：`Aidenzich/HelloBERTopic`

### 4.3 词云与可视化

| 库 | 用途 |
|---|---|
| `wordcloud` | 经典词云，**中文必须设 `font_path`**，支持 mask 形状、`generate_from_frequencies()` |
| `pyecharts` | 基于 ECharts，**交互式**词云（可缩放拖拽），同框架可画词云/热力图/关系图 |
| `stylecloud` | wordcloud 增强版：Font Awesome 图标形状、palettable 配色、渐变、命令行接口 |
| `jieba` | 中文分词标准选择，配合自定义词典 + 停用词表 |

**词云流水线**：`文本 → 规则清洗 → jieba 分词 → 去停用词 → Counter 词频 → 词云图`

> ⚠️ 中文词云两个必踩的坑：**必须指定中文字体路径**（否则全是方框）；**必须加载舆情领域自定义词典**（否则"降本增效""以旧换新"这类词会被切碎）。

### 4.4 时序与传播分析

| 分析 | 依赖 | 说明 |
|---|---|---|
| 趋势曲线 / 拐点 | `comments.publish_time` | 按小时/天分桶聚合 |
| 传播曲线 | `metric_snapshots` | 点赞/评论/转发随时间变化 |
| **传播路径 / KOL 识别** | `author_follower_count` + `parent_content_id` | ✅ 只要采集层抓了这两列，**现在就能做** |
| 事件阶段划分 | 时序 + LLM 解读 | 萌芽期 / 爆发期 / 平台期 / 衰退期 |
| 地域分布 | `comments.ip_location` | 平台自带的 IP 属地 |

> **时序标签是分析层的产物，传播结构依赖采集层的原始字段。** 上一版方案里"用时序标签代替传播演变"是对的取舍，但前提是**采集时把 `parent_content_id` 和 `author_follower_count` 抓下来**——否则将来想做传播路径时，只能重爬，而社媒历史数据重爬常常不可能。

---

## 5. L4 存储层（"数据中台"）

### 5.1 定位澄清

你要的 `localhost:6666` 页面，严格讲是**可视化看板（Dashboard）**，不是数据中台。真正的中台是数据资产管理 + 服务层。

**建议：不要真去建中台。** 单人/小团队本地工具，`SQLite + FastAPI + 前端页面`就是最优解，**过度工程化是这类项目最大的死因**。

但**保留一条边界**：

```
存储层（表 + 读写接口）  ←── 预警逻辑读这里，不读原始 JSON 文件
        ↑
展示层（FastAPI + 前端）  ←── 只调 API，不直连数据库文件
```

**理由**：预警转发、重跑分析、回溯查询、将来换 UI——都需要稳定的数据接口。如果看板直接读爬虫吐的原始 JSON，第二次改需求就得推倒重来。

### 5.2 选型

| 阶段 | 选型 | 理由 |
|---|---|---|
| MVP | **SQLite + SQLModel/SQLAlchemy** | 零运维，单文件，够用 |
| 进阶 | **PostgreSQL** | 并发写、JSONB 索引、全文检索 |
| 分析加速 | **DuckDB**（旁路） | 列存，直接查 Parquet，做聚合统计极快 |

> MVP 直接上 SQLite，**但 DDL 按 PostgreSQL 写**，迁移时改连接串即可。

### 5.3 完整表结构

```
raw_content        内容主体（§2.4）
comments           评论（§2.4）
metric_snapshots   指标时序快照（§2.4）
────────────────────────────────────────────
analysis_results   分析结果
  ├ item_id, item_type (content|comment)
  ├ analysis_version    ← 关键：可重跑，多版本并存
  ├ cleaned_text
  ├ sentiment_label, sentiment_score, emotion_type
  ├ topic_id, topic_prob
  ├ keywords (JSONB)
  └ processed_at

topics             主题表
  ├ topic_id, run_id, label, keywords(JSONB), doc_count, rep_docs(JSONB)

alerts             预警记录
  ├ alert_id, rule_id, level (red|orange|yellow|blue)
  ├ trigger_time, matched_items (JSONB), agg_window
  ├ pushed_at, push_status, push_channel

crawl_tasks        采集任务
  ├ task_id, platform, mode, target, status
  ├ last_cursor      ← 断点续爬游标
  └ created_at, updated_at

alert_rules        预警规则
  ├ rule_id, name, enabled, level
  ├ conditions (JSONB)   {keywords, sentiments, threshold, window}
  └ cooldown_seconds, channels (JSONB)
```

**`analysis_version` 的设计意图**：换了模型、改了 prompt、调了阈值，重跑一遍写入新版本号，**旧结果不删**。这样可以对比"v1 模型 vs v2 模型"的效果，也是 A/B 验证的基础。

---

## 6. L5 预警层

### 6.1 快慢双通道（核心设计）

```
采集完成
  │
  ├─→ 快通道（秒级，无模型）
  │     规则引擎：敏感词命中 / 情绪词 / 增速突变检测
  │     例：「负面词 5 分钟内增量 > 50 条」→ 立即告警
  │     ↓
  │   → 存储层 → 企微推送
  │
  └─→ 慢通道（分钟~小时级）
        本地模型清洗 → 情感/主题分析
        ↓
      → 存储层 → 看板 + 日报/周报推送
```

> **为什么必须双通道**：你的链路里最慢的是本地模型清洗。1 万条评论逐条跑 LLM 要几十分钟到几小时，而舆情预警的价值**全在时效**（行业标准 30 秒~分钟级）。等清洗完再告警，事情已经过去了。

快通道**不需要模型**，正则 + 敏感词表 + 增速阈值就够。

### 6.2 企业微信机器人（硬限制，必须设计进去）

| 限制 | 数值 | 来源 |
|---|---|---|
| 发送频率 | **20 条/分钟 / 每个机器人**（按 webhook 计，不是按群） | 官方社区 |
| 实际可用 | 建议压在 **15 条/分钟** 以内（实测第 6~8 条就可能丢） | 实践反馈 |
| 单条大小 | **≤ 2048 字节**，UTF-8 | 官方文档 |
| @人 | **Markdown 消息不支持 @**；@人还额外消耗配额（@10 人 = 消耗 10 条） | 官方文档 |
| 日/月上限 | 官方未明确 | — |
| 错误码 | 超频返回 `api freq out of limit` | — |

### 6.3 推送必须做四件事

```
① 时间窗聚合
   同一事件 5~10 分钟窗口内的告警合并为一条 Markdown 列表

② 分级路由
   红色 → 单独实时推送
   橙/黄/蓝 → 合并进日报

③ 冷却去重
   同一事件 5 分钟内只推一次（规则里配 cooldown_seconds）

④ 超量降级
   量大时切换到「企微自建应用消息 API」——不受 20 条/分钟限制
   （代价：需部署应用 + 获取 access_token）
```

> **反直觉但重要**：舆情爆发时负面评论是**成批来的**。如果不做聚合，按"每条都推"设计，会在爆发最需要被告知的那一刻，恰好被限流打爆——这是最典型的翻车方式。

### 6.4 预警规则示例

```json
{
  "name": "品牌负面激增",
  "level": "red",
  "conditions": {
    "keywords": ["品牌名", "产品名"],
    "sentiment": "negative",
    "threshold": 50,
    "window_seconds": 300
  },
  "cooldown_seconds": 300,
  "channels": ["wecom"]
}
```

### 6.5 推送通道扩展

| 通道 | 状态 | 说明 |
|---|---|---|
| 企业微信机器人 | **MVP** | Webhook，最简单 |
| 企微自建应用 | 二期 | 突破 20 条/分钟，量大时必需 |
| 钉钉 / 飞书机器人 | 二期 | 同样的 Webhook 模式 |
| 邮件 | 二期 | 日报/周报附件 |
| 短信 | 三期 | 仅红色告警 |

---

## 7. L6 可视化看板

### 7.1 选型

| 方案 | 说明 |
|---|---|
| **FastAPI + 前端（推荐）** | FastAPI 提供 `/api/*`，前端用 ECharts。**最灵活，可长期演进** |
| Streamlit | 纯 Python 快速出图，**适合 MVP 抢时间**，但交互能力有限 |
| Gradio | 更适合 Demo / 模型演示 |
| Metabase / Superset | 通用 BI，配置即用，但定制化难 |

**建议路径**：Streamlit 快速验证 → 迁到 FastAPI + ECharts。

`localhost:6666` 这个端口可以给 FastAPI；MediaCrawler 自带的 WebUI 占 8080，注意错开。

### 7.2 页面规划

```
├── 总览大盘
│   ├── 监控词声量趋势（折线，按小时/天）
│   ├── 情感分布（饼图 / 堆叠柱）
│   ├── 平台分布（条形图）
│   └── 实时告警流（滚动列表）
│
├── 事件详情
│   ├── 传播曲线（metric_snapshots 时序）
│   ├── 评论时间分布（拐点标注）
│   ├── 词云
│   ├── 主题分布（BERTopic）
│   └── 代表评论列表
│
├── 主题分析
│   ├── 主题列表 + 关键词
│   ├── 主题随时间演化（BERTopic 动态主题）
│   └── 主题间相似度热力图
│
├── 情感分析
│   ├── 情感趋势
│   ├── 细分情绪分布
│   └── 负面 TOP 内容
│
└── 数据管理
    ├── 采集任务状态 / 手动触发
    ├── 重跑分析（选 analysis_version）
    └── 预警规则配置
```

### 7.3 可视化组件

| 图表 | 库 |
|---|---|
| 词云（交互） | `pyecharts` WordCloud |
| 折线 / 柱状 / 饼图 | `pyecharts` / ECharts |
| 关系图（传播路径） | `pyecharts` Graph |
| 热力图（地域/时段） | `pyecharts` HeatMap |
| 主题层次树 | BERTopic 内置 `visualize_hierarchy()` |

> ⚠️ **报告措辞约束**：评论区只代表"愿意评论的人"，天然偏向极端情绪。**不能说"公众情绪偏负面"，只能说"讨论区情绪分布"**。这个措辞差异在对外交付时是要命的。

---

## 8. L0 编排层

### 8.1 选型对比

| 工具 | 定位 | 结论 |
|---|---|---|
| **APScheduler** | Python 调度器 | ✅ **MVP 选这个**。cron/间隔触发，单进程，零依赖 |
| Celery | 任务队列 | 需要异步任务 + worker 集群时才上 |
| Prefect | 轻量编排器 | ✅ **规模化后选**。`@flow`/`@task` 装饰器，改动代码极少，最小 2 容器 |
| Dagster | 资产导向编排 | 血缘追踪最强，适合 ML 管道，但偏重 |
| Airflow | 企业级编排 | ❌ **本项目不推荐**，重、静态 DAG、学习曲线陡，是杀鸡用牛刀 |

> ⚠️ **Prefect / Airflow 在 Windows 上需 Docker/WSL**。你在 Windows 本机开发，MVP 用 APScheduler 是正确选择。

### 8.2 调度任务

```python
# 伪代码
@scheduler.scheduled_job('interval', minutes=15)
def crawl_cycle(): ...

@scheduler.scheduled_job('interval', minutes=1)
def fast_alert_cycle(): ...      # 快通道

@scheduler.scheduled_job('interval', minutes=30)
def clean_and_analyze(): ...     # 慢通道

@scheduler.scheduled_job('cron', hour=8)
def daily_report(): ...
```

---

## 9. 技术栈汇总

| 层 | 组件 | 项目 / 包 |
|---|---|---|
| 采集 | 多平台爬虫 | `NanmiCoder/MediaCrawler` |
| 采集 | 话题发现 | `666ghj/BettaFish` → MindSpider |
| 采集 | 代理池 | `jhao104/proxy_pool` |
| 清洗 | 去重 | `ekzhu/datasketch`、`ChenQianll/text-dedup` |
| 清洗 | 本地 LLM | **Ollama** + **Qwen2.5-14B-Instruct** |
| 清洗 | 结构化 | `Instructor`、`json_repair`（vLLM 路线可用 `Outlines`） |
| 分析 | 情感 | `IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment` → 微调 `hfl/chinese-roberta-wwm-ext` |
| 分析 | 主题 | `MaartenGr/BERTopic` + `text2vec-base-chinese` |
| 分析 | 中文分词 | `jieba` |
| 分析 | 词云 | `wordcloud` / `pyecharts` / `stylecloud` |
| 存储 | DB | SQLite → PostgreSQL（+ DuckDB 旁路） |
| 存储 | ORM | SQLModel / SQLAlchemy |
| 服务 | API | FastAPI + Uvicorn |
| 看板 | 前端 | Streamlit（MVP）→ ECharts |
| 编排 | 调度 | APScheduler → Prefect |
| 推送 | 告警 | 企业微信 Webhook |
| 环境 | Python | 3.11+ |

---

## 10. 项目目录结构

```
E:\Wochat\
├── 舆情分析Agent_整体方案.md          # 本文档
├── README.md
├── pyproject.toml
├── .env                              # 代理、DB、企微 webhook
│
├── crawler/                          # L1 采集层
│   ├── base.py                       # CrawlerSource 协议
│   ├── mediacrawler_source.py        # MediaCrawler 封装
│   ├── mindspider_source.py          # 话题发现
│   ├── normalize.py                  # → 统一字段
│   └── throttle.py                   # 限速 / 退避
│
├── pipeline/                         # L2 清洗层
│   ├── rules.py                      # 规则清洗
│   ├── dedup.py                      # SHA256 + MinHash/LSH
│   ├── llm_clean.py                  # Ollama 结构化
│   └── schemas.py                    # 结构化输出 schema
│
├── analysis/                         # L3 分析层
│   ├── sentiment.py                  # 情感分类
│   ├── topics.py                     # BERTopic
│   ├── wordcloud_gen.py              # 词云
│   ├── timeseries.py                 # 时序聚合
│   └── propagation.py                # 传播分析
│
├── store/                            # L4 存储层
│   ├── models.py                     # SQLModel 表定义
│   ├── ddl.sql                       # 按 PostgreSQL 写
│   └── repository.py                 # 统一读写接口 ★
│
├── alert/                            # L5 预警层
│   ├── rules_engine.py               # 快通道
│   ├── aggregator.py                 # 窗口聚合 + 冷却
│   └── notifier.py                   # 企微推送
│
├── web/                              # L6 看板
│   ├── api.py                        # FastAPI
│   └── static/                       # 前端
│
├── scheduler/                        # L0 编排
│   └── jobs.py                       # APScheduler
│
├── models/                           # 本地模型权重 / 配置
├── data/                             # SQLite / 原始 JSONL
├── dicts/                            # 停用词、自定义词典、敏感词表
└── tests/
```

---

## 11. 实施路线

### Phase 0：环境准备（1~2 天）
- [ ] Python 3.11 venv；配置 7897 代理（仅下载用）
- [ ] 部署 MediaCrawler，跑通小红书或微博**单平台单关键词**采集
- [ ] 确认能拿到评论 + 二级评论
- [ ] Ollama + Qwen2.5-14B 拉取并跑通

### Phase 1：MVP 端到端（1~2 周）
**目标：一条链路跑通，跑通比跑好重要。**

- [ ] `CrawlerSource` 协议 + MediaCrawler 实现
- [ ] **完整落库**（§2.4 全部字段，含 `raw_json`）
- [ ] 规则清洗 + 精确去重
- [ ] 情感分析（直接调 Erlangshen 现成模型）
- [ ] jieba 词云 + 词频
- [ ] SQLite 落库 + `repository.py`
- [ ] **Streamlit 单页看板**（趋势 + 情感饼图 + 词云）
- [ ] 企微机器人推送（先不做聚合，跑通链路）

**验收标准**：输入一个关键词，30 分钟内能在看板上看到趋势图和词云。

### Phase 2：可用性（2~3 周）
- [ ] MinHash 近重复去重
- [ ] BERTopic 主题建模
- [ ] 快慢双通道预警
- [ ] 企微推送**聚合 + 分级 + 冷却**
- [ ] 多平台扩展（抖音 → B站 → 知乎）
- [ ] APScheduler 定时调度
- [ ] 断点续爬

### Phase 3：质量与规模化（1~2 月）
- [ ] **自建 300~500 条标注集**，测情感真实准确率
- [ ] 微调情感模型
- [ ] `metric_snapshots` 时序快照 + 传播曲线
- [ ] 传播路径 / KOL 识别
- [ ] 迁移 PostgreSQL
- [ ] vLLM 替换 Ollama（若吞吐不够）
- [ ] FastAPI + ECharts 替换 Streamlit
- [ ] 日报/周报自动生成

---

## 12. 风险清单

| 风险 | 等级 | 应对 |
|---|---|---|
| **平台反爬升级导致采集中断** | 🔴 高 | 用浏览器自动化而非逆向；保持 MediaCrawler 更新；多平台冗余；断点续爬 |
| **IP / 账号被封** | 🔴 高 | 限速（建议 ≥ 3~5 秒/请求）；不要走境外代理；准备备用账号 |
| **账号登录态失效** | 🟠 中 | 登录态缓存 + 失效告警 + 二维码快速重登 |
| **情感准确率不达预期** | 🟠 中 | **Phase 3 建标注集**；把情感当方向性信号，不当精确测量 |
| **反讽 / 网络用语识别失败** | 🟠 中 | 维护领域词典；接受这不是可完全解决的问题 |
| **评论区样本偏斜** | 🟠 中 | 结论措辞限定为"讨论区"；不要外推为"公众" |
| **本地模型吞吐不足** | 🟡 低 | 换 vLLM；模型降级到 7B；批处理 |
| **企微推送被限流** | 🟡 低 | 聚合 + 分级 + 冷却；超量换自建应用 API |
| **数据量增长导致 SQLite 变慢** | 🟡 低 | 迁 PostgreSQL；聚合结果预计算 |

### 合规提示

| 场景 | 风险 | 说明 |
|---|---|---|
| **内部自用 / 研究** | 🟢 低 | 当前定位。注意限速、不采个人信息 |
| **对外交付 / 商业化** | 🔴 高 | **爬虫方案必须换掉**——这不是技术问题，是资质问题 |

具体红线：
1. **优先走浏览器自动化，而非纯逆向破解签名**——走用户可见的正常流程，技术与法律风险都低得多
2. **不采集个人信息**：手机号、身份证、人脸一律不碰；用户 ID **哈希脱敏**后存储
3. **严格限速**，不造成服务压力
4. **现在就做 `CrawlerSource` 抽象**——将来换授权 API / 采购数据源时，分析层一行不用改
5. 头部厂商（新浪舆情通、蜜度）靠的是**平台数据授权**，不是爬虫

---

## 13. 关键决策记录（ADR）

| # | 决策 | 理由 |
|---|---|---|
| 1 | 采集用 MediaCrawler，不自己写 | 签名逆向维护成本极高，浏览器自动化零逆向 |
| 2 | 国内平台采集**不走** 7897 代理 | 代理 IP 特征反而触发风控；登录态与出口地域冲突 |
| 3 | 只取评论区文本，不处理视频 | 评论区是情绪最集中处，信噪比高于视频正文 |
| 4 | 采集层抓全字段（含 `parent_content_id`） | **采集不可逆，分析可重跑**。漏抓的字段将来补不回来 |
| 5 | 本地 LLM 只做清洗打标，不做情感判定 | 情感是封闭分类任务，小模型更快更准 |
| 6 | 预警走快慢双通道 | 等模型清洗完再告警，时效性已丧失 |
| 7 | 不建真"数据中台"，只做存储/展示分层 | 单人本地工具，过度工程化是最大死因 |
| 8 | 编排用 APScheduler，不用 Airflow | Windows 本机开发，Airflow 需 WSL/Docker 且过重 |
| 9 | MVP 用 Streamlit，二期换 FastAPI | 抢时间验证，再演进 |
| 10 | 看板结论措辞限定"讨论区" | 评论区样本天然偏向极端情绪，不可外推为"公众" |

---

## 14. 参考链接

**采集**
- [NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) · [镜像 automcn/MediaCrawler](https://github.com/automcn/MediaCrawler)
- [MediaCrawler 实战揭秘（30K star）](https://developer.aliyun.com/article/1674552)
- [666ghj/BettaFish（微舆）](https://github.com/666ghj/BettaFish) · [MindSpider](https://raw.githubusercontent.com/666ghj/BettaFish/main/MindSpider/README.md)
- [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool) · [ProxyPool 部署与维护指南](https://www.ipipgo.com/ipdaili/59930.html)
- [多平台爬虫逆向指南：a_bogus & mstoken](https://blog.csdn.net/9q8w7e6r5/article/details/151306831) · [小红书x-s & 抖音a-bogus 逆向](https://devlg.com/caseinfo/18971)

**清洗**
- [ChenQianll/text-dedup（All-in-one text de-duplication）](https://github.com/weiyx16/text-dedup)
- [ekzhu/datasketch（MinHash + LSH）](https://github.com/ekzhu/datasketch)
- [HuggingFace datatrove MinHash 流水线](https://raw.githubusercontent.com/leeroopedia/workflow-huggingface-datatrove-minhash-deduplication/refs/heads/main/README.md)
- [Outlines 结构化生成](https://www.aipuzi.cn/ai-news/outlines.html) · [结构化输出方案对比](https://ossaihub.com/learn/builder/i-04-function-calling-structured-output/)
- [luochang212/llm-deploy（Ollama/vLLM 部署教程）](https://github.com/luochang212/llm-deploy)

**分析**
- [qhduan/Chinese-BERT-wwm（ChnSentiCorp 榜单）](https://github.com/qhduan/Chinese-BERT-wwm)
- [BrainCloud-coder/BERT 中文情感分类微调](https://github.com/BrainCloud-coder/Chinese-text-sentiment-classification-based-on-BERT-fine-tuning)
- [kayzhou/Guba-emotion（金融情绪）](https://github.com/kayzhou/Guba-emotion)
- [MaartenGr/BERTopic](https://github.com/MaartenGr/BERTopic) · [BERTopic 中文范例 Aidenzich/HelloBERTopic](https://github.com/Aidenzich/HelloBERTopic)
- [LDA/Top2Vec/BERTopic 工具对比](https://zhuanlan.zhihu.com/p/587096188) · [BERTopic vs Top2Vec 讨论](https://github.com/MaartenGr/BERTopic/issues/372)
- [ToyoLiu/微博文本分析（jieba+pyecharts+LDA）](https://github.com/ToyoLiu/Text-Analysis-of-Chinese-Social-Media-Weibo)

**编排 / 推送**
- [Airflow vs Dagster vs Prefect 决策矩阵](https://pipecode.ai/blogs/airflow-vs-dagster-vs-prefect-vs-kestra-vs-mage-orchestrator-comparison)
- [企业微信机器人频率限制（官方社区）](https://developer.work.weixin.qq.com/community/question/detail?content_id=16766473678046456954)
- [企微 Webhook 限制说明（腾讯云文档）](https://www.tencentcloud.com/zh/document/product/248/38208?lang=zh)
- [企微机器人 Webhook 告警方案与限流应对](https://damodev.csdn.net/6a6b1e6110ee7a33f29482e3.html)

**行业参考**
- [2026舆情监测新格局：七大厂商产品能力全景拆解](https://www.zqbao.com.cn/news/19682.html)
- [国内优秀舆情服务厂商汇总介绍](https://www.eefung.com/company-news/20251024164715311)
- [2026年舆情监测系统怎么选](https://www.civiw.com/webyy/20260810175641260)
