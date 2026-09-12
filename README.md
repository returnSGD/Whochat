# Whochat —— 舆情分析 Agent

> **🌐 在线介绍页：<https://whochatting.pages.dev/>**
>
> 本地部署的舆情分析工具链：**多平台采集 → 本地清洗去重 → 情感/主题分析 → 看板 + 企微预警**。
> 数据不出内网，零依赖即可跑通全链路，LLM 可选接入。

[在线介绍页](https://whochatting.pages.dev/) · [设计方案](舆情分析Agent_整体方案.md) · [GitHub](https://github.com/returnSGD/Whochat)

`Python 3.10+` · `7 个平台` · `6 层链路` · `308 个测试` · `词典法情感 90.9%`

---

## 目录

- [快速开始](#快速开始)
- [接入真实数据](#接入真实数据)
- [LLM 分析（可选）](#llm-分析可选)
- [架构](#架构)
- [关键模块](#关键模块)
- [情感分析基线](#情感分析基线)
- [十条设计决策](#十条设计决策)
- [已知限制（诚实清单）](#已知限制诚实清单)
- [目录与词典](#目录与词典)
- [环境说明](#环境说明)

- 介绍链接：https://whochatting.pages.dev/

---



## 快速开始

```bash
# 0. 依赖（conda base 已含 torch/transformers/wordcloud/pandas/jieba，只需补这几个）
pip install sqlalchemy streamlit datasketch json-repair
pip install -e .

# 1. 环境自检 —— 一次把"什么能跑、什么没装"说清楚
python -m Whochat.cli init

# 2. 零依赖跑通全链路（不需要爬虫、不需要模型）
python -m Whochat.cli demo

# 3. 可视化（双入口：看板端 / 操作端）
python -m Whochat.cli dashboard          # → http://localhost:8501
#   /           看板端 —— 只读：趋势/情感/主题/词云/传播/预警记录（默认页）
#   /console    操作端 —— 可写：L1~L5 触发与微调（采集/分析/规则/推送）

# 4. 回归测试（308 个用例，约 60 秒）
python -m pytest -q
```

`demo` 能跑通，说明整条业务链路是好的。之后逐步换成真实数据源。

> **demo 是幂等的**：造数据用固定种子，连跑多次落库量不变、告警数不变
> （第二次起 `分析 0 条`）。所以 `demo` 也可以当回归基线用 ——
> 数字变了就说明业务逻辑被改动了。

### 两个容易踩的坑

> ⚠️ **端口别选浏览器禁用端口**（`6665~6669` 等原 IRC 端口段在内）。
> 浏览器会在建连前直接拒绝，报"无法访问此页面"；而 `curl` / `requests`
> 不检查这份清单，服务端一切正常 —— 于是 HTTP 200 会给人"页面没问题"的错觉。
> 默认端口是 8501，改端口用 `--port` 或 `WHOCHAT_DASHBOARD_PORT`。

> ⚠️ **看板端在根路径 `/`，不是 `/dashboard`**。Streamlit 规定默认页的
> `url_path` 恒为空字符串，给它传 `url_path` 会被**静默忽略**（不报错）。
> 因此 `/dashboard` 是一条不存在的路由：访问它会先报 "Page not found"，
> 再回退到默认页（恰好就是看板端），表现为"报错一下又刷新出来"。
> 注意 `curl /dashboard` 仍返回 200 —— SPA 任何路径都发同一个外壳，
> **HTTP 200 证明不了路由存在**。

---

## 接入真实数据

```bash
# 采集（首次需扫码登录，浏览器会弹出）
python -m Whochat.cli crawl --platform xhs --keyword "你的品牌"
python -m Whochat.cli crawl --platform douyin --keyword "你的品牌"

# 或者导入已有的 JSONL/JSON/CSV
python -m Whochat.cli import path/to/comments.jsonl --platform xhs

# 分析 → 主题 → 词云
python -m Whochat.cli analyze
python -m Whochat.cli topics              # 需要 bertopic
python -m Whochat.cli wordcloud

# 预警
python -m Whochat.cli alert --seed-rules
python -m Whochat.cli alert --since-hours 24   # 回放模式：补数/复盘用

# 看看数据
python -m Whochat.cli status
```

支持平台：`douyin` `xhs` `kuaishou` `bilibili` `weibo` `tieba` `zhihu`

---

## LLM 分析（可选）

任何 **OpenAI 兼容**接口都能用。在 `.env` 里填这两行就生效，**不需要额外装包**
（直接走 HTTP 调，`requests` 本来就是核心依赖）：

```bash
WHOCHAT_LLM_BASE_URL=https://api.deepseek.com/v1
WHOCHAT_LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
```

**模型名可以留空** —— 会自动调 `GET /models` 挑一个对话模型（会跳过 embedding /
rerank / whisper 这类非对话模型），挑不出来会明确报错让你填 `WHOCHAT_LLM_MODEL`。
base_url 只写到域名也行（`https://api.deepseek.com`），会在
`{base}/chat/completions` 与 `{base}/v1/chat/completions` 之间自动探测一次并缓存。

配置完用 `python -m Whochat.cli init` 自检，应显示：

```
LLM 分析    : 就绪 · 模型 deepseek-chat（自动选择（候选 42 个））
```

### 它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| **广告识别** —— 判**意图**而不是关键词（"商家刷单太明显了"是在批评，不是广告；纯正则会误杀） | **情感判定** —— ADR#5。永远走词典法，实测比模型更准（90.9% vs 60.6%） |
| **主体识别** —— 这条在骂哪个产品/型号（`subject` 字段，规则完全做不到） | 声量统计 —— 必须是精确计数 |
| **关键词抽取** —— "这条在说什么"，比 TF-IDF 高频词有用 | 快通道预警 —— 那条链路的设计前提就是"无模型、秒级" |
| **主题命名** —— 把 `发热 / 续航 / 掉电` 归纳成「屏幕发热与续航」 | |
| **字段映射**（`import --llm-map`）—— 字段名陌生的数据集，学一次就能导入 | |

### 三个必须知道的行为

1. **版本隔离**：开启 LLM 后，分析结果写入 `lexicon-v1-llm`，与原有的
   `lexicon-v1` **并存**，不覆盖。事后能对比"用没用模型"的差异。
2. **demo 的幂等性只对未开启 LLM 时成立** —— 模型是概率性的。跑回归基线时
   请关掉 LLM。
3. **漏项不丢数据**：模型少返回几条时，那几条会**继续被正常分析**，只是不带
   LLM 标签。丢数据比少打一个标严重得多（有测试钉死）。

### 成本

批量请求（默认一次 10 条）。1 万条评论约 1000 次请求。可用
`WHOCHAT_LLM_BATCH` 调批大小、`WHOCHAT_LLM_RPM` 限速、`WHOCHAT_LLM_TIMEOUT` 调超时。
跑完会打印 token 用量。

> ⚠️ 访问境外 API（OpenAI 等）需要代理：设 `HTTPS_PROXY=http://127.0.0.1:7897`。
> 采集流量与它是分开的，不会互相影响（见 §2.3 的代理分工）。

### 也可以直接在页面上填

不想改文件的话，打开操作端 **<http://127.0.0.1:8501/console>** → **L2 清洗** →
「LLM 分析」，有三个输入框（地址 / 密钥 / 模型名），填完点「保存并测试连接」即可。
配置会写进 `.env`（已在 `.gitignore` 里），并**立即生效，不用重启看板**。

> 密钥框是 password 类型且永不回显，已保存时显示成 `sk-a***wxyz` 的掩码。

> ⚠️ **本机想跑本地模型的话**：RTX 3060 Laptop 只有 **6GB 显存**，
> Qwen2.5-14B q4 约 9GB **装不下**。要用 7B q4（~4.7GB，勉强）或 3B/4B。
> 本地 Ollama 也走同一套：`WHOCHAT_LLM_BASE_URL=http://127.0.0.1:11434/v1`，
> key 随便填（它不校验）。

---

## 架构

```
L1 采集    MediaCrawler(Playwright) · MockSource · ManualImport
              ↓  CrawlerSource 协议 ← 唯一的解耦点
L2 清洗    规则清洗 → 精确去重 → MinHash/SimHash 近重复 → LLM 打标(可选)
              ↓
       ┌──────┴──────┐
       ↓ 快通道       ↓ 慢通道
L3 分析  规则引擎        情感分析 · BERTopic · 词云 · 时序
       (秒级,无模型)     (分钟~小时)
       └──────┬──────┘
              ↓
L4 存储   Repository 统一读写接口  ← 事实上的"中台"边界
              ↓
       ┌──────┴──────┐
L5 预警   聚合·分级·冷却      L6 看板  FastAPI/Streamlit :8501
       → 企微机器人
```

---

## 关键模块

| 模块 | 位置 | 说明 |
|---|---|---|
| 采集协议 | `crawler/base.py` | `CrawlerSource` 协议 + 时间/ID 归一化工具 |
| MediaCrawler 适配 | `crawler/mediacrawler_source.py` | 子进程调用，读 JSONL 归一化 |
| 字段归一化 | `crawler/normalize.py` | 别名表 + 多候选回退，含 `"1.2万"` → `12000` 解析 |
| 规则清洗 | `pipeline/rules.py` | 清洗、广告识别、分词、关键词抽取 |
| 去重 | `pipeline/dedup.py` | 精确(SHA256) + 近重复(MinHash/SimHash 分桶) |
| LLM 客户端 | `pipeline/llm_client.py` | 任意 **OpenAI 兼容**接口（只填 base_url + api_key）；端点/模型自动探测、重试退避、JSON Mode 降级、用量统计 |
| LLM 打标 | `pipeline/llm_clean.py` | 广告识别（看意图不只看关键词）/ 主体识别 / 关键词抽取；**批量 + 序号对齐，漏项不丢数据** |
| LLM 字段映射 | `crawler/llm_map.py` | 字段名陌生的平台：学一次「原始字段 → schema」并落盘缓存，泛化别名表覆盖不到的情况 |
| 情感分析 | `analysis/sentiment.py` | 词典法(子串扫描+否定+程度+转折) / Transformer 双后端 |
| 主题建模 | `analysis/topics.py` | BERTopic（中文嵌入模型降级链）+ **零下载离线方案**兜底 |
| 时序 | `analysis/timeseries.py` | 爆发检测、阶段划分、情感漂移、KOL |
| 传播分析 | `analysis/propagation.py` | **传播曲线**（metric_snapshots → 起爆点/峰值/增速拐点）+ **传播路径**（parent_content_id → 转发链 DOT 图） |
| 存储 | `store/repository.py` | 统一读写接口，上层不碰 SQL |
| 预警规则 | `alert/rules_engine.py` | 快通道，支持 `since/until` **回放**；风险词与情绪词双线 |
| 企微推送 | `alert/notifier.py` | 窗口聚合 + 分级路由 + 冷却 + 限流 + dry-run；**失败保持 pending 自动重试**（不丢警） |

---

## 情感分析基线

自建标注集（`tests/annotated_sample.json`，33 条）：

```bash
python -m Whochat.cli evaluate tests/annotated_sample.json
```

**词典后端准确率 90.9%**（对比：子串扫描改造前是 69.7%）。

剩余错误集中在两类：

- **反讽**（"这手机真好用，用了三天就送修了"）
- **词典语境**（"问题当天就解决了" — "问题"是中性语境）

另外两条已知短板已写成 `xfail` 用例（`pytest -q` 会显示为 `2 xfailed`），
修好后会自动变成 XPASS 提醒：

- `"这也叫好用？垃圾死了"` → 判 neutral（负面词被正面词抵消后落在中性带内）
- `"质量真稳定，稳定地坏"` → 判 neutral（程度副词"真"把"稳定"放大后压过"坏"）

### ⚠️ Transformer 后端目前**不如**词典后端

实测（同一 33 条标注集）把 `WHOCHAT_SENTIMENT_BACKEND` 切成 `transformer`：

| 后端 | 准确率 | 说明 |
|---|---|---|
| `lexicon` | **90.9%** | 默认。零依赖、毫秒级、可复现 |
| `transformer` | **60.6%** | `IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment` |

**原因是模型选错了，不是代码问题**：Erlangshen 这款是**二分类**（正/负）模型，
没有"中性"这一档，而本链路（schema / 看板 / 预警）是完整三分类。
实测它的分数分布：

```
negative  [-0.998, +0.603]
neutral   [-0.824, +0.997]   ← 铺满整个区间
positive  [+0.928, +0.999]
```

中性样本（+0.97 ~ +1.0）和正面样本（+0.93 ~ +1.0）**完全重叠** ——
不存在任何一个能把它们分开的中性带，所以**调 `neutral_band` 是徒劳的**
（拉宽会把正确的正面一起吞掉）。

代码现在会在加载二分类模型时打印警告。**结论：在拿到三分类中文情感模型、
或用业务数据微调之前，保持 `lexicon`。** 这也正是方案文档 §4.1 Phase 3
把"微调情感模型"列为长期任务的原因。

> ⚠️ 33 条样本量很小，这个数字证明的是**链路可用**，不是**准确率有 90.9%**。
> **请务必用自己业务的数据建 300~500 条标注集重测** —— 没有基线，
> 后面所有的情感统计、预警阈值、主题分析都建立在流沙上。

---

## 十条设计决策

这些是踩过坑之后定下来的，改之前建议先看 [方案文档 §13](舆情分析Agent_整体方案.md)。

| # | 决策 | 为什么 |
|---|---|---|
| 1 | 采集用 MediaCrawler，不自己写 | 抖音 `a_bogus` 是 JSVMP 混淆+环境检测+数月一升级；浏览器自动化零逆向且不随加密更新失效 |
| 2 | **国内平台采集不走代理** | 代理 IP 特征反而触发风控；出口地域跳变与登录态冲突。7897 只用于下载 |
| 3 | 只取评论区文本 | 评论区是情绪最集中处，信噪比高于视频正文 |
| 4 | **采集层抓全字段** | 采集不可逆，分析可重跑。`parent_content_id`/`follower_count` 事后补不回来（内容已删） |
| 5 | LLM 只做打标，不判情感 | 情感是封闭分类任务，词典法更快更准更可复现。用 LLM 逐条判是拿大炮打蚊子（实测：transformer 60.6% vs 词典 90.9%） |
| 6 | 预警走**快慢双通道** | 等模型清洗完再告警，时效性已丧失（行业标准 30 秒~分钟级） |
| 7 | 不建真"数据中台" | 单人本地工具，过度工程化是最大死因。但要保留存储/展示边界 |
| 8 | 编排用 APScheduler，不用 Airflow | Windows 本机开发，Airflow 需 WSL/Docker 且过重 |
| 9 | MVP 用 Streamlit，二期换 FastAPI | 抢时间验证，再演进 |
| 10 | 看板措辞限定"讨论区" | 评论区样本天然偏向极端情绪，不可外推为"公众意见" |

---

## 已知限制（诚实清单）

| 限制 | 说明 |
|---|---|
| **MediaCrawler 是 NON-COMMERCIAL 许可** | 明确禁止商业用途。要商业交付必须实现 `OfficialAPISource` 替换 `mediacrawler_source.py` —— 这正是协议存在的意义 |
| 采集成功率 70~90% | 403/滑块/登录态失效是常态。**这是健康水平**，不是 bug |
| 反讽识别 | 规则法基本无解，需模型或上下文 |
| 评论区样本偏斜 | 只代表"愿意评论的人"，天然偏极端情绪 |
| 主题建模需 ≥100 文档 | 小样本 BERTopic 会退化成一堆碎片主题。文档数 <20 时直接拒绝并说明原因 |
| **HuggingFace 可达性不稳定** | 嵌入模型下载会失败。此时自动降级到 **TF-IDF+SVD+HDBSCAN 离线方案**（零下载），效果弱于 BERTopic 但功能不报废。联网后重跑即可提升。**现状**：`text2vec-base-chinese` 已缓存到本地，在线 BERTopic 路径已验证可用（断网时仍自动回退离线方案） |
| 传播分析依赖采集字段 | 没抓 `parent_content_id` 就做不了，且**事后补不回来** |
| LLM 输出是概率性的 | 同一条文本两次调用可能给不同标签。所以它只做**打标**（可容忍抖动），不参与情感判定与声量统计 |
| LLM 会自动挑模型 | 挑的是"符合偏好表的第一个对话模型"，**不代表最适合你**。不确定就显式填 `WHOCHAT_LLM_MODEL` |
| **LLM 打标结果未在真实数据上评估过** | 现有测试用的是本地假服务，验证的是**契约**（不漏项、不错位、不丢数据），**不是准确率**。广告识别比正则好在哪、差在哪，需要你自己的标注集来测 |
| 字段映射可能学错 | 已有防护（只认 schema 内字段、别名表优先、一个目标只认一次），但**学错仍会往库里写错数据**。缓存文件在 `data/llm_field_map.json`，可直接查看/删除重学 |
| 免费代理池不好用 | 可用率低、生命周期短。真要规模化得买国内住宅代理 |

---

## 目录与词典

```
src/Whochat/
├── config.py              配置（代理/采集/模型/情感/预警/存储）
├── cli.py                 命令行入口
├── console.py             控制台编码兼容（GBK 下 emoji 降级不崩）
├── crawler/               L1 采集
├── pipeline/              L2 清洗
├── analysis/              L3 分析
├── store/                 L4 存储
├── alert/                 L5 预警
├── web/                   L6 可视化（双入口）
│   ├── app.py             入口/导航：看板端 + 操作端
│   ├── dashboard.py       看板端（只读）
│   └── console.py         操作端（L1~L5 控制与微调）
└── scheduler/             L0 编排（APScheduler）
dicts/                     停用词 / 敏感词 / 情感词 / jieba 自定义词典
data/                      SQLite、原始 JSONL、导出物、LLM 字段映射缓存
vendor/MediaCrawler/       采集基座（git clone，未修改）
tests/                     pytest 用例（308 个）+ 情感标注集
```

词典分工（`dicts/`）：

| 文件 | 作用 | 改它会怎样 |
|---|---|---|
| `user_dict.txt` | jieba 分词自定义词典 | 影响词云/主题的词切分，**不影响情感** |
| `stopwords.txt` | 分词停用词（追加到内置表） | 影响词云/主题，**不影响情感** |
| `positive_words.txt` | 正面情感词（追加到内置表） | **直接改情感判定**，改完务必重跑 `evaluate` |
| `negative_words.txt` | 负面情绪词 | 影响情感判定 + 预警"情绪激增" |
| `sensitive_words.txt` | **风险**词（监管/法律/安全/隐私） | 影响预警"敏感词命中"规则 |

> 情绪词（失望/垃圾）衡量"骂得多凶"；风险词（起诉/12315/召回）衡量"事情闹多大"。
> 一条冷静的"已向12315投诉并准备起诉"情感分很低，但风险等级最高 —— 两者不能混。

---

## 环境说明

- Python 3.10+（实测 3.13）
- 系统代理 `7897`（Clash 混合端口）**仅用于下载**：GitHub / HuggingFace / pip / Ollama
- 采集**直连**，不走代理

```bash
cp .env.example .env   # 按需修改
```

### 关于 `vendor/MediaCrawler`

**它不在本仓库里**，两个原因：

1. 它是第三方项目且是 **NON-COMMERCIAL 许可**，不适合随本仓库一起分发；
2. 它自己就是个完整项目，体积不小。

所以 `pip install -e .` 之后，`demo` / `import` / 分析 / 看板 / 预警都能正常跑
（它们不依赖采集器）。**只有真实采集需要先补上它**：

```bash
git clone https://github.com/NanmiCoder/MediaCrawler.git vendor/MediaCrawler
# 再按它的 README 装依赖（Playwright + 浏览器），必要时在 .env 里指定解释器：
# WHOCHAT_MC_PYTHON=C:\path\to\mediacrawler\venv\Scripts\python.exe
```

> 本仓库的代码对 MediaCrawler **只做子进程调用 + 读它产出的 JSONL**，
> 没有修改它的源码。想验证整条链路而不装爬虫，直接用 `python -m Whochat.cli demo`。

### 命名说明

仓库、Python 包、命令、环境变量前缀**统一是 `Whochat`**（此前内部包名误拼成
`wochat`，少了一个 `h`，已全量更正）：

```bash
python -m Whochat.cli demo
```

> 改名影响三处：包目录 `src/Whochat/`、命令 `python -m Whochat.cli ...`、
> 环境变量前缀 `WHOCHAT_*`（旧的 `.env` 需同步改名）。本地默认库文件也从
> `data/db/wochat.db` 变为 `data/db/Whochat.db`。

---

<div align="center">

**🌐 在线介绍页：[https://whochatting.pages.dev/](https://whochatting.pages.dev/)**

**GitHub：[https://github.com/returnSGD/Whochat](https://github.com/returnSGD/Whochat)**

</div>
