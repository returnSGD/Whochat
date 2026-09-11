# 开发日志

> 记录时间：2026-09-12
> 阶段：MVP 骨架完成，链路已端到端跑通
> **最新一轮（第二轮）接手加固见 §九 —— 修掉 27 个真实缺陷、补上 130 个单元测试，
> 并解决了 BERTopic 联网与 demo 不可复现两个遗留问题。**

---

## 一、当前状态总览

| 层 | 模块 | 状态 |
|---|---|---|
| L0 编排 | APScheduler（快通道/采集/分析/主题/日报） | ✅ 已写，**已实跑**（§9.4；Ctrl+C 与连接泄漏已修） |
| L1 采集 | `CrawlerSource` 协议 | ✅ 已验证 |
| L1 采集 | MediaCrawler 适配器 | ✅ 已写，**未跑真实采集**（需扫码登录） |
| L1 采集 | MockSource（造数据） | ✅ 已验证 |
| L1 采集 | ManualImport（JSONL/JSON/CSV） | ✅ 已验证 |
| L2 清洗 | 规则清洗 + 广告识别 | ✅ 已验证（14/14 用例） |
| L2 清洗 | 去重（SHA256 + SimHash/MinHash） | ✅ 已验证 |
| L2 清洗 | 本地 LLM 结构化（Ollama） | ✅ 已写，**未测**（需 `ollama pull`，且 `ollama` 包未装） |
| L3 分析 | 情感分析（词典后端） | ✅ 已验证，标注集 **90.9%** |
| L3 分析 | 情感分析（Transformer 后端） | ⚠️ 已测，**60.6% 不如词典**（模型是二分类，见 §9.4） |
| L3 分析 | 主题建模 BERTopic | ✅ **在线路径已跑通**（§9.4），离线降级仍在 |
| L3 分析 | 主题建模离线降级方案 | ✅ 已验证（6 主题） |
| L3 分析 | 词云 | ✅ 已验证（1200×800 PNG） |
| L3 分析 | 时序 / 爆发检测 / 传播 | ✅ 已验证 |
| L4 存储 | 8 张表 + Repository 接口 | ✅ 已验证 |
| L5 预警 | 快通道规则引擎 | ✅ 已验证 |
| L5 预警 | 企微推送（聚合/分级/冷却/限流） | ✅ 已写，**只跑过 dry-run** |
| L6 看板 | Streamlit :6666 | ⚠️ HTTP 200、连接泄漏已修，**图表渲染仍未经人眼确认** |

代码量：**约 5900 行**（`src/` 下 30 个 Python 文件）；**测试 130 个用例**（第二轮新增）

---

## 二、已验证的产出

```
python -m wochat.cli init          # 环境自检 6 项全通过
python -m wochat.cli demo          # 端到端：1988 评论 → 过滤 774 → 分析 1214
python -m wochat.cli topics        # 离线方案 6 个主题
python -m wochat.cli wordcloud     # wordcloud.png 1200×800
python -m wochat.cli evaluate      # 情感准确率 90.9%
python -m wochat.cli import ...    # JSONL 导入 + 字段归一化
python -m wochat.cli status        # 数据统计
python -m wochat.cli dashboard     # localhost:6666 → HTTP 200
```

**关键验证点**：

- 字段归一化 `"1.2万"` → `12000`、`"3,456"` → `3456` ✓
- 用户 ID 哈希脱敏（原始 ID 不落库）✓
- unix 时间戳 → aware datetime ✓
- 预警触发并输出完整企微 Markdown ✓
- 词云中文正常渲染（非白像素 44.8%，无方框）✓

---

## 三、过程中发现并修复的缺陷（18 项）

这部分是本次最有价值的产出 —— 大多是**只在真跑数据时才会暴露**的问题。

### 严重（会导致功能失效或误导）

| # | 问题 | 修复 |
|---|---|---|
| 1 | **预警引擎只看"最近 N 分钟"** —— 爬虫断线补数、历史回放时数据 `publish_time` 在过去，**整批静默漏警** | `evaluate()` 加 `since`/`until` + `skip_cooldown`，CLI 加 `--since-hours` 回放模式 |
| 2 | **规则误触发**：`负面占比异常` 只配了 `negative_ratio` 没配 `threshold`，却因默认阈值 10 触发，且命中列表混入中性/正面评论，**严重误导严重程度判断** | `threshold` 不再设默认值；占比规则触发时命中列表只装负面评论 |
| 3 | **情感分析词级匹配大面积漏词** —— jieba 把「智商税」切成「智商」+「税」 | 改为**子串扫描**。<br>准确率 **69.7% → 78.8%** |
| 4 | **否定词跨越标点作用** —— "还是没修，太失望了" 里"没"修饰的是"修"，却把"失望"反转成正面 | 加**分句边界**，否定词只在同分句内生效。<br>准确率 **78.8% → 90.9%** |
| 5 | **去重统计漏报** —— `exact_dropped` 算出来但没往外传，上层永远显示"重复 0" | `DedupResult` 加 `exact_dropped` 字段和 `total_dropped` 属性 |
| 6 | **广告正则漏最典型的引流格式** —— `加V信 abc12345` 因中间有空格匹配失败 | 重写正则允许中间空白。补 4 条典型用例，**14/14 通过** |

### 中等（影响可用性 / 结果质量）

| # | 问题 | 修复 |
|---|---|---|
| 7 | 主题建模 SVD 取 50 个主成分 / 57 篇文档，解释方差 **0.999**（等于没降维），HDBSCAN 把所有点判成离群 → **0 个主题** | 主成分降到 10~15；离线方案单独调 `min_cluster_size` / `min_samples` |
| 8 | `aggregate_by_content` **没用上正文** —— 文档只由评论拼成，同一次舆情里评论高度同质，所有文档向量几乎相同 | 文档 = 标题 + 正文 + 最多 20 条评论。正文才是区分"讨论质量/讨论售后"的信号 |
| 9 | `text2vec-base-chinese` 模型名不全 → 解析成不存在的仓库，401 | 改为 `shibing624/text2vec-base-chinese`，并加三级降级链 |
| 10 | 嵌入模型降级只在构造时 try，**错误实际发生在 `fit_transform`** | 把 fit 一起放进重试循环 |
| 11 | **mock 没有真正的爆发点** —— 评论各自聚在所属内容发布后 6 小时内，靠随机碰运气，趋势图看不出峰值 | 显式构造爆发窗口：40% 内容压在爆发点 ±4h，评论量放大 5~10 倍 |
| 12 | 同一情感词重复出现重复计分 —— "质量真稳定，稳定地坏" 里两个"稳定"盖过"坏" | 每个情感词只计一次 |
| 13 | **疑问句被判成正面** —— "请问这个支持以旧换新吗" 因含"支持"判 positive | 加疑问句检测，只压正面不压负面（保留吐槽式反问） |
| 14 | `settings.DATA_DIR` —— **模块级常量当成了 Settings 属性** | 改为直接 import |

### 轻微（语法/报告）

| # | 问题 |
|---|---|
| 15 | `rules_engine.py` 手滑写成 `matched: list[dict] =]` 语法错误 |
| 16 | `sentiment.py` 写成 `for转折 in` 缺空格 |
| 17 | `jobs.py` 留了一行 `repo.session.query_failed_tasks if False else None` 占位垃圾 |
| 18 | demo 的 `stats.report()` 里采集/落库数恒为 0（没累计到同一个对象） |

---

## 四、环境与网络（实测结论）

### 依赖

conda base 环境**已含** `torch 2.6` / `transformers 5.9` / `wordcloud` / `pandas 3.0` / `jieba`，省掉数 GB 下载。只需补装：

```bash
pip install sqlalchemy streamlit datasketch json-repair apscheduler
pip install bertopic umap-learn hdbscan
pip install -e .
```

### HuggingFace 下载（**与网上常见建议相反**）

| 方案 | 结果 |
|---|---|
| `hf-mirror.com` + `HF_ENDPOINT` | ❌ 持续 `LocalEntryNotFoundError`，四个嵌入模型候选全部失败（curl 能通，但 hub 客户端不行） |
| **HF 官方 + 代理 7897** | ✅ 可用 |

正确用法：

```bash
export HTTPS_PROXY=http://127.0.0.1:7897
export HTTP_PROXY=http://127.0.0.1:7897
unset HF_ENDPOINT                    # 不要设成镜像
```

### 网络可达性

**间歇性**。同一天内多次测同一主机结果不同：

| 主机 | 观察 |
|---|---|
| `github.com` | 走代理有时 200 有时 000 |
| `huggingface.co` | 走代理时通时不通 |
| `pypi.org` | 直连/代理**始终可用**（装包没受影响） |

→ 这直接催生了**主题建模的零下载离线降级方案**（TF-IDF + SVD + HDBSCAN）。

---

## 五、未完成 / 待验证

> ⚠️ **本节的清单已被 §九 取代**，保留作为第一轮的历史记录。
> 当前真实待办见 §9.6。

### 进行中

- [ ] **BERTopic 在线方案** —— 后台任务正在下载嵌入模型，**结果未知**。
      离线降级方案已可用（6 个主题），联网成功后重跑 `topics` 会自动用 BERTopic

### 未测（需用户操作或外部条件）

- [ ] **真实采集** —— MediaCrawler 首次运行需**扫码登录**，需要人工介入。
      建议第一步先跑 `--platform weibo`（反爬最松）验证链路
- [ ] **企微推送真发** —— 当前只跑过 dry-run。需在 `.env` 填 `WOCHAT_WECOM_WEBHOOK`
- [ ] **本地 LLM 清洗** —— 需 `ollama pull qwen2.5:14b-instruct-q4_K_M`
- [ ] **Transformer 情感后端** —— 需下载 Erlangshen 权重
- [ ] **调度器实跑** —— `python -m wochat.scheduler.jobs` 未启动过
- [ ] **看板页面目视确认** —— HTTP 200 + 数据层单测通过，但没逐个人眼看过图表渲染

### 待补

- [ ] `dicts/` 下只有 `user_dict.txt` 和 `negative_words.txt`，
      `stopwords.txt` / `sensitive_words.txt` / `positive_words.txt` 未创建（代码有内置默认值，不影响运行）
- [ ] `tests/` 下只有情感标注集，**没有单元测试**
- [ ] 预警规则只有 3 条默认规则，未按业务场景扩充

---

## 六、已知限制（诚实清单）

| 限制 | 说明 |
|---|---|
| **MediaCrawler 是 NON-COMMERCIAL 许可** | 明确禁止商业用途。商业交付必须实现 `OfficialAPISource` 替换 `mediacrawler_source.py` —— 这正是 `CrawlerSource` 协议存在的意义 |
| 情感标注集只有 33 条 | 90.9% 证明的是**链路可用**，不是**准确率有 90.9%**。务必用业务数据建 300~500 条重测 |
| 反讽识别 | "这手机真好用，用了三天就送修了" —— 规则法基本无解，需模型或上下文 |
| 评论区样本偏斜 | 只代表"愿意评论的人"，天然偏极端情绪。**看板措辞必须限定"讨论区"** |
| 采集成功率 70~90% | 403/滑块/登录态失效是常态，**这是健康水平不是 bug** |
| 传播分析依赖采集字段 | 没抓 `parent_content_id` / `follower_count` 就做不了，且**事后补不回来** |

---

## 七、下一步建议（第一轮版，已被 §9.6 取代）

按优先级：

1. **确认 BERTopic 下载结果** —— 联网后重跑 `python -m wochat.cli topics`
2. **跑一次真实采集**（`--platform weibo` 最稳），验证 MediaCrawler 适配器端到端
3. **建业务标注集**（300~500 条）重测情感准确率 —— 这是后面所有分析结论的地基
4. **配企微 webhook**，验证真实推送
5. **补单元测试** —— 尤其是 `normalize.py` / `dedup.py` / `sentiment.py` 这三个纯函数模块
6. **扩充预警规则** —— 按业务关键词和敏感词定制

---

## 八、文件清单

```
舆情分析Agent_整体方案.md    架构设计与决策记录（14 节）
README.md                    使用说明
WORKLOG.md                   本文档
.env.example                 配置模板（含网络实测结论）
pyproject.toml

src/wochat/
├── config.py                157 行   配置
├── cli.py                   569 行   命令行入口
├── crawler/
│   ├── base.py              187 行   ★ CrawlerSource 协议（核心解耦点）
│   ├── mediacrawler_source.py 211 行  MediaCrawler 适配器
│   ├── normalize.py         190 行   字段归一化（别名表 + 回退）
│   ├── mock_source.py       296 行   造数据（确定性）
│   └── manual_source.py      90 行   JSONL/JSON/CSV 导入
├── pipeline/
│   ├── rules.py             193 行   清洗 / 广告识别 / 分词
│   ├── dedup.py             285 行   精确 + 近重复去重
│   ├── llm_clean.py         248 行   Ollama 结构化（优雅降级）
│   └── runner.py            203 行   流水线编排
├── analysis/
│   ├── sentiment.py         407 行   词典/Transformer 双后端
│   ├── topics.py            339 行   BERTopic + 离线降级
│   ├── timeseries.py        207 行   爆发检测 / 阶段 / 传播
│   └── wordcloud_gen.py     136 行   词云（中文字体探测）
├── store/
│   ├── models.py            263 行   8 张表
│   └── repository.py        479 行   ★ 统一读写接口
├── alert/
│   ├── rules_engine.py      262 行   快通道（支持回放）
│   └── notifier.py          299 行   企微（聚合/分级/冷却/限流）
├── scheduler/jobs.py        285 行   5 个周期任务
└── web/app.py               409 行   看板（6 个页面）

dicts/
├── user_dict.txt             32 行   jieba 自定义词典
└── negative_words.txt        87 行   负面词表（预警用）

tests/annotated_sample.json  133 行   情感标注集（33 条）
vendor/MediaCrawler/                 采集基座（git clone，未修改）
```

---

## 九、第二轮接手加固（2026-09-12）

第一轮把链路跑通了，但没有单元测试、demo 不可复现，且若干缺陷只在
"重跑/换环境/真发消息"时才暴露。这一轮做了三件事：**修缺陷、补测试、填欠账**。

### 9.1 最要紧的三个发现

**① demo 根本不是可复现的（这是最严重的一个）**

`mock_source.py` 用内置 `hash()` 做种子：

```python
seed = hash((task.platform, task.mode, task.target)) & 0xFFFFFFFF
```

内置 `hash()` 对 str/tuple 有**进程级随机盐**（PYTHONHASHSEED），所以
"固定 seed、结果可复现"这句 docstring 是假的 —— 实测同一个 task 跑三次
分别得到 2068 / 1915 / 2015 条记录，comment_id 全不一样。
而 `upsert_comments` 只增不改，于是**每跑一次 demo，SQLite 里就多囤一批**。

后果不是"数字难看"，而是**预警数字系统性失真**：告警引擎按全库扫时间窗，
却和"本次分析量"一起打印出来，于是出现了

```
12.0 小时窗口内命中 3296 条     ← 历史所有运行的总和
分析 1315 条                    ← 本次真正分析的量
```

这种自相矛盾的输出。换成 `zlib.crc32` 后，连跑三次 demo：
落库恒为 1928 条、DB 总量恒为 1928（不再增长）、告警数恒为 622/1718，
第二次起 `analyze` 为 0 条（幂等）。

**② 发送失败的告警会被永久丢弃**

`notifier.flush()` 在发送失败时把整批标记成 `push_status="failed"`，
而 `pending_alerts()` 只查 `"pending"` —— 这批告警**再也不会被重试**，
日报也看不到它们。企微返回 `45009 限流`、或一次网络抖动，就能让一批
告警无声消失。对预警系统来说这是最糟的故障：**漏警比重复推送严重得多**。

改成失败时保持 `pending` 并记录 `push_error`，下个周期自动重试；
配套加了 `expire_stale_alerts()`：只有"失败过且超过 6 小时"的才标记
`failed`，避免 webhook 配错时无限重试刷日志。

顺带修了截断：`_truncate_bytes` 预留 20 字节，但追加的后缀
`"\n…（消息过长已截断）"` 是 **31 字节**，截断后反而变成 2059 字节，
超出企微 2048 字节硬上限被拒收 —— 再叠加上面的丢警逻辑就是双重打击。
现在按后缀真实字节数预留（实测 2047 / 2048 边界刚好）。

**③ 内容发布者的原始用户 ID 直接落库了**

`comment_record()` 里做了 `anonymize_id()`，但 `content_record()` 没有，
`normalize_content()` 又把 `author_id=pick(raw, ["user_id", ...])` 原样传进去。
实测 `normalize_content({'user_id':'RAW_USER_123'}, 'xhs')['author_id']`
就是 `'RAW_USER_123'`。

这既违反方案文档 §12 的合规红线，也和 README 里
"用户 ID 哈希脱敏（原始 ID 不落库）✓" 的声明直接矛盾。
现在 `content_record()` 统一脱敏，并且**丢弃调用方传入的 `author_id`**，
让"原始 ID 永不落库"成为无条件保证，而不是靠调用方自觉。

### 9.2 其余缺陷（按严重度）

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| 1 | `analysis/sentiment.py` | 转折分支（先扬后抑）算完不再夹逼，分数可到 **-3.5**，越出 `[-1,1]` 约定 | 阈值比较/排序/展示全部失真 |
| 2 | `pipeline/runner.py` | 被过滤的广告/重复评论不写任何记录，下一轮又被当"待分析"取出；且其孪生兄弟已入库、不在去重池里，这次反而被当正常评论分析 | **每重跑一次统计就更脏一点** |
| 3 | `web/app.py` | 5 个缓存查询函数 `Repository()` 用完不关，每次缓存过期漏一条池化连接 | 池 5+10，刷新几轮就 `TimeoutError` 卡死看板 |
| 4 | `crawler/mock_source.py` | 二级评论 `parent_comment_id` 少了 `:03d` 补零（`_cm5` vs `_cm005`），且会引用被稀疏采样跳过的评论 | 456 条悬空父引用，传播/线程分析无法验证（现为 0） |
| 5 | `pipeline/dedup.py` | 3-gram 对 ≤2 字文本产生 0 个 shingle，MinHash 全空 → 所有短评论互相判为重复 | "支持""谢谢""关注"整批静默消失 |
| 6 | `pipeline/rules.py` | `_CONTACT` 里的裸 `v\|V` 匹配英文单词首字母 | `"这个version真的很稳定"`、`"看了video"` 被误判成广告 |
| 7 | `crawler/mediacrawler_source.py` | 兜底路径 glob `*/jsonl/*.jsonl` 跨平台，却按当前 task 的平台打标 | 微博任务会把残留的小红书数据存成微博 |
| 8 | `crawler/mediacrawler_source.py` | `seen` 集合内容/评论共用，贴吧 `tid` 撞号 | 第二条（评论）被静默丢弃 |
| 9 | `crawler/manual_source.py` | 分类只认字面量 `comment_id` | `{id:…}` 被当内容、`{cid:…}`/`{rpid:…}` 被静默丢弃 |
| 10 | `crawler/manual_source.py` | CSV 写死 `utf-8-sig` | 中文 Windows 的 Excel 默认导出 GBK，**直接崩** |
| 11 | `cli.py` → `runner._dump_raw` | `task.target` 为空时迭代 `None` | **README 演示的 `import xxx.jsonl --platform xhs`（不带 --keyword）必崩** |
| 12 | `config.py` | 相对 `WOCHAT_DB_URL` 按 CWD 解析 | 不从项目根启动就 `unable to open database file` |
| 13 | `config.py` | 布尔只认字符串 `"true"` | `WOCHAT_LLM_ENABLED=1` 被静默当 False |
| 14 | `scheduler/jobs.py` | 自定义 SIGINT 处理器替换了默认处理器，`_running` 标志位无人读 | **Ctrl+C 完全没反应**，只能杀进程 |
| 15 | `scheduler/jobs.py` | 4 个 job 的 `repo.close()` 不在 `finally` 里 | 异常路径漏连接，每分钟一次的 job 很快耗光连接池 |
| 16 | `analysis/topics.py` | `aggregate_by_content` 收下 `version` 却不用 | 主题建模吃进了广告和未分析的评论 |
| 17 | `analysis/timeseries.py` | `sigma==0` 分支只报 surge | 基线平稳时"声量暴跌/爬虫掉线"的信号被静默丢弃 |
| 18 | `pipeline/llm_clean.py` | `available()` 用子串匹配模型名 | 只装了 7B 时配置 14B 也报"就绪"，之后每条都失败且静默吞掉 |
| 19 | `pipeline/dedup.py` | `exact_dedupe` 用 `not fp` 判空是死代码（sha256 摘要永远非空） | 第一条空文本被保留、其余被当重复丢弃 |
| 20 | `crawler/normalize.py` | `parse_bool` 对无法识别的字符串返回 `False` | "未知"和"明确为假"混为一谈 |

### 9.3 补上的欠账

**单元测试：0 → 130 个（+2 个 xfail）**

`pytest` 配置写进 `pyproject.toml`，并排除 `vendor/`（MediaCrawler 自带的
测试依赖第三方环境，收集它们只会刷一堆无关报错）。

| 文件 | 覆盖 |
|---|---|
| `conftest.py` | 每个用例一个全新临时库（Repository 的 engine 是模块级单例，不重置会串数据） |
| `test_normalize.py` | `"1.2万"→12000`、别名回退、**作者 ID 脱敏** |
| `test_dedup.py` | 精确/近重复、短文本、空文本、`total_dropped` |
| `test_rules.py` | 清洗、分词、**广告误判双向用例** |
| `test_sentiment.py` | 分数区间、否定跨分句、疑问句、重复计分 |
| `test_alert_engine.py` | 敏感词、负面占比命中列表、阈值缺失不误触发、回放、冷却 |
| `test_manual_import.py` | 别名分类、GBK 编码、无 keyword 导入 |
| `test_config.py` | 布尔解析、相对路径锚定 |

新增用例里有一半是**回归用例**：把上面每一个缺陷都钉住，改坏了会立刻红。

**词典：补 3 个文件**

- `stopwords.txt` —— 追加到内置表之上；平台名/口水词/通用商业套话
- `positive_words.txt` —— 只收语义明确的强正面词（"可以""还行"这类
  模棱两可的一律不收，否则会把中性评论大批误判成正面）；
  加完重跑基线仍是 **90.91%**，没有回归
- `sensitive_words.txt` —— **风险词**（监管/法律/安全/隐私红线），
  和 `negative_words.txt` 的**情绪词**分工不同：
  一条冷静的"已向12315投诉并准备起诉"情感分很低，但风险等级最高

顺带发现 `sensitive_words()` 是**死代码**（定义了但全项目没人调用，
docstring 却声称"快通道预警用"）。已在 `RuleEngine` 里接通
（`conditions.use_sensitive_words`，评估时读文件所以改词表立刻生效），
并加了默认规则 `sensitive_hit`（orange，窗口 1h，阈值 3 条）。

### 9.4 遗留问题解决

| 问题 | 结论 |
|---|---|
| **BERTopic 在线方案**（第一轮"结果未知"） | ✅ **已跑通**。`text2vec-base-chinese` 之前只下到 config 就断了（权重还是 `.incomplete`）；补下后 `cli topics` 走在线路径，57 篇文档 → 3 个主题、5 条未归类。**离线降级仍在**，断网时自动回退 |
| **看板页面目视确认** | ⚠️ 部分确认：HTTP 200、无异常日志、连接泄漏已修；**图表渲染仍需人眼过一遍** |
| **调度器实跑** | ✅ `--list`、触发器注册、`job_fast_alert` 实跑、信号处理均验证通过（此前从未启动过） |
| **Transformer 情感后端** | ⚠️ **跑通了，但结论是"别用这个模型"** —— 见下 |

#### Transformer 后端的实测结论（重要）

补下 `IDEA-CCNL/Erlangshen-Roberta-110M-Sentiment` 权重后实测：

| 后端 | 准确率（同 33 条标注集） |
|---|---|
| `lexicon` | **90.9%** |
| `transformer` | **60.6%** |

**不是代码 bug，是模型选型错了**：Erlangshen 这款是**二分类**（正/负），
没有"中性"这一档，而本链路的 schema / 看板 / 预警全是三分类。
逐条看分数分布就很清楚了：

```
negative  [-0.998, +0.603]
neutral   [-0.824, +0.997]   ← 铺满整个区间
positive  [+0.928, +0.999]
```

中性样本（+0.97~+1.0）与正面样本（+0.93~+1.0）**完全重叠**，
**不存在能分开的中性带** —— 所以调 `neutral_band` 是徒劳的
（拉宽会把正确的正面一起吞掉）。混淆矩阵里 10 条中性有 8 条被判成正面，
就是 60.6% 的全部来源。

已在 `TransformerSentiment.__init__` 里加了显式警告，并更正了
README / `.env.example` 里"transformer 更准"的旧说法。
**要走模型路线必须换三分类模型，或用业务数据微调**（方案文档 §4.1 Phase 3）。

### 9.5 已知短板（诚实清单，未修）

两个 `xfail` 用例，都是**规则法固有短板**，不是实现 bug：

| 用例 | 现状 | 为什么先不修 |
|---|---|---|
| `"这也叫好用？垃圾死了"` | 判 neutral | "垃圾"(-1.6) 被"好用"(+1.5) 抵消后净 -0.035，落在中性带内。代码注释说"只压正面不压负面"，但把净值清零并不能保留负面信号。真修要改成只扣正面贡献，会牵动 90.91% 基线 |
| `"质量真稳定，稳定地坏"` | 判 neutral（+0.14） | WORKLOG §三-12 声称这个例子已修，**实际只修了一半**：重复计分确实只算一次了，但"真"是程度副词，把"稳定"(+1.1) 放大到约 +1.65，仍压过"坏"(-1.2) |

> 这两条都标成了 `xfail` 而不是删掉 —— 哪天修好了会变成 XPASS 提醒你。

### 9.6 仍然需要你本人操作的事

| 事项 | 命令 / 前置条件 |
|---|---|
| **真实采集** | `python -m wochat.cli crawl --platform weibo --keyword "你的词"` —— 首次需扫码登录 |
| **企微真发** | 在 `.env` 填 `WOCHAT_WECOM_WEBHOOK`（当前只跑过 dry-run） |
| **本地 LLM 清洗** | `ollama pull qwen2.5:14b-instruct-q4_K_M` 且 `.env` 设 `WOCHAT_LLM_ENABLED=true`（`ollama` 包本身也没装） |
| **Transformer 情感后端** | 权重已下、链路已通，但实测只有 60.6%（模型是二分类，见 §9.4）—— **要换三分类模型才值得用** |
| **建业务标注集 300~500 条** | 33 条只证明链路可用，**不是**准确率有 90.91% |
| **看板图表人眼确认** | `python -m wochat.cli dashboard` → localhost:6666 |

> ⚠️ 顺手记一条**踩坑记录**：`python -m wochat.cli init` 在 Git Bash 里
> 看着是乱码，但那是管道按 UTF-8 解码造成的假象 —— 实测 Python 输出的是
> `cafd bedd bfe2`（GBK 的"数据库"），**在真实 Windows 控制台显示正常**，
> 不是 bug，别去"修"它。

