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
| L6 看板 | Streamlit :8501 | ⚠️ 端口已从 :6666 改为 :8501（:6666 是浏览器禁用端口，见 §十四）；**图表渲染仍待本人用浏览器过目** |

代码量：**约 5900 行**（`src/` 下 30 个 Python 文件）；**测试 130 个用例**（第二轮新增）

---

## 二、已验证的产出

```
python -m Whochat.cli init          # 环境自检 6 项全通过
python -m Whochat.cli demo          # 端到端：1988 评论 → 过滤 774 → 分析 1214
python -m Whochat.cli topics        # 离线方案 6 个主题
python -m Whochat.cli wordcloud     # wordcloud.png 1200×800
python -m Whochat.cli evaluate      # 情感准确率 90.9%
python -m Whochat.cli import ...    # JSONL 导入 + 字段归一化
python -m Whochat.cli status        # 数据统计
python -m Whochat.cli dashboard     # localhost:8501 → HTTP 200
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
- [ ] **企微推送真发** —— 当前只跑过 dry-run。需在 `.env` 填 `WHOCHAT_WECOM_WEBHOOK`
- [ ] **本地 LLM 清洗** —— 需 `ollama pull qwen2.5:14b-instruct-q4_K_M`
- [ ] **Transformer 情感后端** —— 需下载 Erlangshen 权重
- [ ] **调度器实跑** —— `python -m Whochat.scheduler.jobs` 未启动过
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

1. **确认 BERTopic 下载结果** —— 联网后重跑 `python -m Whochat.cli topics`
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

src/Whochat/
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
| 12 | `config.py` | 相对 `WHOCHAT_DB_URL` 按 CWD 解析 | 不从项目根启动就 `unable to open database file` |
| 13 | `config.py` | 布尔只认字符串 `"true"` | `WHOCHAT_LLM_ENABLED=1` 被静默当 False |
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
| **真实采集** | `python -m Whochat.cli crawl --platform weibo --keyword "你的词"` —— 首次需扫码登录 |
| **企微真发** | 在 `.env` 填 `WHOCHAT_WECOM_WEBHOOK`（当前只跑过 dry-run） |
| **本地 LLM 清洗** | `ollama pull qwen2.5:14b-instruct-q4_K_M` 且 `.env` 设 `WHOCHAT_LLM_ENABLED=true`（`ollama` 包本身也没装） |
| **Transformer 情感后端** | 权重已下、链路已通，但实测只有 60.6%（模型是二分类，见 §9.4）—— **要换三分类模型才值得用** |
| **建业务标注集 300~500 条** | 33 条只证明链路可用，**不是**准确率有 90.91% |
| **看板图表人眼确认** | `python -m Whochat.cli dashboard` → localhost:8501 |

> ⚠️ 顺手记一条**踩坑记录**：`python -m Whochat.cli init` 在 Git Bash 里
> 看着是乱码，但那是管道按 UTF-8 解码造成的假象 —— 实测 Python 输出的是
> `cafd bedd bfe2`（GBK 的"数据库"），**在真实 Windows 控制台显示正常**，
> 不是 bug，别去"修"它。

---

## 十、第三轮加固（2026-09-12，接力）

第二轮把测试补到 130 个，这一轮**接着往下挖**：分两路审计各层真实缺陷，
逐条复现后修复，并把"只能靠人眼确认"的看板渲染变成可回归的测试。

**测试：130 → 171 个（+2 xfail 不变）**；`cli demo` 端到端退出 0，
落库/告警数字与第二轮基线一致；情感标注集仍 **90.91%**，无回归。

### 10.1 三个最严重的发现

**① README 的头号命令 `cli demo` 在本机默认控制台根本跑不完**

```
UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f534'
  at notifier.flush() → print(content)
EXIT=1
```

Windows 控制台默认编码是 **GBK**，编码不了 emoji；而 `notifier.flush()` 的
dry-run 分支把带 emoji 的企微 markdown 直接 `print` 出来，于是 demo 在
"快通道预警"那一步崩溃，后面的词云端与汇总全没跑（退出码 1）。
第二轮是在 UTF-8 终端里跑的，所以没暴露。

修法不是删 emoji（那是发给企微群的，UTF-8 下完全正常），而是新增
`Whochat/console.py::configure_console()`，把 stdout 的 `errors` 从 strict
改成 replace：GBK 能编码的中文照常，emoji 退化成 `?`，不再崩。
在 `cli.main()` 与 `scheduler.main()` 两个入口调用。

**② 发送失败的告警等不到重试，6 小时后被 expire 永久丢警**

`job_fast_alert` 把 `notifier.flush()` 包在 `if alert_ids:` 里 —— 只有本轮
产生了新告警才推送。但 `flush()` 的设计是"发送失败保持 pending，下个周期
自动重试"；下个周期若没有新告警，flush 根本不被调用，那批 pending 一直
搁着，直到 `expire_stale_alerts`（6h）把它标成 `failed`。企微返回
`45009 限流` 或一次网络抖动就足以触发。对预警系统来说这是最糟的故障。

修复：flush **无条件**每个周期执行，与是否产生新告警解耦。

**③ upsert 用 None 覆盖已有值，重复采集把 `publish_time` 清成 NULL**

`upsert_comments/upsert_contents` 的 update 分支对整个 payload `setattr`，
而归一化记录**始终带 `publish_time` 键**（别名没命中时为 None）。于是某次
采集缺这个字段，就会把库里原本有效的时间戳清空 —— 该评论从此掉出所有
按时间窗的查询（`comments_in_window` 要求 `publish_time IS NOT NULL`），
快通道漏警、趋势图失真，且再也回不来。注释里"publish_time 等不变"与实际
行为相悖。修复：update 时 **None 不覆盖已有值**。

### 10.2 其余缺陷（按严重度）

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| 1 | `crawler/mediacrawler_source.py` | MediaCrawler 按天复用文件名，同一天是**追加**；旧代码用"文件名集合差"判新文件，差集为空就回退读本平台**所有日期**的文件 | 每次采集把全量历史重灌：旧内容 `search_keyword` 被当前关键词改写、快照表灌水。改为按 **(mtime, size)** 判断本次是否被写过；没写入就不产出，而不是重读历史 |
| 2 | `crawler/normalize.py` | 别名表缺 MediaCrawler 的真实作者字段 `creator_hash` / `user_nickname` | **真实采集的作者 ID 恒为 NULL**，KOL 识别/作者聚合完全失效。已对照 `vendor/MediaCrawler` 各平台 store 逐一确认 |
| 3 | `crawler/normalize.py` | 缺各平台真实字段别名：bilibili `video_comment`/`video_share_count`/`video_favorite_count`/`video_type`、weibo `comment_like_count`、zhihu `content_text`/`created_time`/`content_url`、douyin `aweme_type`、kuaishou `video_type` | 真实采集的正文/互动量/发布时间静默为 NULL |
| 4 | `crawler/normalize.py` | bilibili 顶层评论固定写 `parent_comment_id="0"`，被当成有父评论 | 一级评论全量误标 `level=2` 并挂到不存在的父 "0"，线程/传播统计失真 |
| 5 | `pipeline/rules.py` | `is_spam` 的裸关键词正则（刷单/带货/推广…）出现即判广告 | "商家刷单太明显了，太失望了"这类**最该被分析的负面舆情**被当广告删掉（`is_valid=False`），永久排除出情感/主题统计。改为要求业务后缀作第二信号 |
| 6 | `pipeline/dedup.py` | 去重指纹走 `clean()`，把 emoji 抹成空格 | "这个产品真的很好用😀" 与 "…😡" 指纹相同 → 后者被标 duplicate、永不进入情感分析。emoji 是中文社媒主要极性信号。新增 `clean(keep_emoji=True)` 专供指纹，emoji 范围抽成 `EMOJI_CLASS_BODY` 常量复用 |
| 7 | `alert/rules_engine.py` | 实时模式 `until=None`，查询只有下界 | 未来时间戳被计入。`parse_time` 对无时区字符串按 UTC 解析，平台本地时间（+8h）会"落在未来 8 小时"，冷却一到就反复误报同一批。改为 `until = until or now` |
| 8 | `alert/notifier.py` | 日报版本号写死 `"v1"`，实际是 `lexicon-v1` | 日报情感分布恒为空（"声量 0 条"），恰好给出"风平浪静"的错误结论。新增 `Repository.latest_analysis_version()` 自动解析 |
| 9 | `analysis/timeseries.py` | 只抓到 `parent_content_id`、没抓粉丝数时，`available=True` 后按全 0 排序输出 KOL 榜 | 伪造一个"看起来有模有样"的榜单。改为按字段分别判定能力，缺就跳过并声明缺失 |
| 10 | `store/repository.py` | 归因 `search_keyword` 每次采集都被后来者覆盖 | 同一内容被多个监控词命中时，"这条舆情是哪个词发现的"永久错乱。改为**首次带值后不再改写** |
| 11 | `web/app.py` | 侧边栏选了平台/时间，趋势图过滤了，但"讨论区情绪分布""负面占比""负面 TOP"仍是全量 | 同一屏两个数字互相矛盾。筛选参数贯穿全部查询 |
| 12 | `crawler/base.py` | `comment_record` 缺 `content_record` 已有的防御性 `author_id` 丢弃 | 调用方传 `author_id=` 会覆盖脱敏值，"原始 ID 永不落库"不是无条件成立。当前调用方都合规，属潜伏缺陷 |
| 13 | `pipeline/dedup.py` | `dedupe(threshold=…)` 在装了 datasketch 时被静默忽略（MinHash 固定 0.8） | 两种阈值量纲不同不能通用。新增 `jaccard_threshold` 显式入口 |
| 14 | `crawler/normalize.py` | `parse_count` 正则非锚定：`"1.2.3"→1`、`"1e3"→1` | 静默产生错误数字，比返回 None 危险。改为整串 fullmatch，并容忍"约/近/超过"前缀 |

### 10.3 看板渲染：从"靠人眼"变成可回归

第二轮遗留"看板图表仍需人眼确认"。这一轮加了 `tests/test_dashboard.py`，
用 Streamlit 官方 `AppTest` **真正执行** `app.py`：6 个 tab 的查询函数、
图表组装、筛选参数全部跑一遍，任何异常都会被捕获。

> HTTP 200 其实只证明 Streamlit 起得来（返回的是静态外壳），脚本要等会话
> 连接才执行 —— 所以之前那个"HTTP 200"并不构成渲染验证。

### 10.4 已知取舍（未改，但记录在案）

| 项 | 说明 |
|---|---|
| dry-run 把告警标 `skipped` | 未配 webhook 时，`flush` 打印后标记 `skipped` 而非保留 pending。这意味着先无 webhook 跑一段时间、之后才配 webhook 的话，那些历史告警不会补发。**这是有意的**：否则首配 webhook 时会被积压告警刷屏。真实部署应在启动前配好 webhook |
| 人工导入的 `{id, content, note_id}` | 无 title、同时含通用 `id` 与 `content` 的记录，内容/评论二义性无法可靠区分。当前按评论处理（`note_id` 作为归属），可能丢失内容的 title/url 等字段。人工整理的数据建议显式带 type 字段 |
| `raw_json` 保留平台原始 `user_id` | 与"原始 ID 永不落库"字面冲突，但这是"采集不可逆、raw_json 是唯一后悔药"的已知取舍（有测试固化） |

### 10.5 本轮验证过的命令

```bash
python -m pytest -q                     # 182 passed, 2 xfailed
python -m Whochat.cli demo               # 退出 0，内容 60 / 评论 1928 / 分析 1164
python -m Whochat.cli evaluate tests/annotated_sample.json   # 90.91%
python -m Whochat.scheduler.jobs --once daily_report         # 日报有真实数字
python -m streamlit run src/Whochat/web/app.py               # AppTest 6 tab 无异常
```

---

## 十一、传播曲线 + 传播路径可视化（2026-09-12，Phase 3）

方案文档 Phase 3 的明确条目。**数据早就在采了，只是展示层一直没用起来**：
`metric_snapshots` 每次采集都写，看板却只看得到条数；`parent_content_id`
是 ADR#4 专门强调"不可逆、必须抓"的字段，但页面上只显示一个覆盖率。

### 11.1 新增

| 位置 | 内容 |
|---|---|
| `analysis/propagation.py` | `build_curve()` 曲线点、`analyze_growth()` 起爆点/峰值/增速拐点/平均增速、`build_graph()` 传播路径图、`to_dot()` 生成 Graphviz DOT |
| `store/repository.py` | `snapshot_series(content_id)`、`contents_with_snapshots()`（读接口此前完全缺失） |
| `crawler/mock_source.py` | **造转发/引用链**。此前 `parent_content_id` 恒为 `None`，传播路径这条链路在 demo 里从未被跑过 —— 做出来也没数据可验证 |
| `web/app.py` | 传播 tab 拆成「传播曲线」与「传播路径 / KOL」两段；曲线用 `st.line_chart`，路径用 `st.graphviz_chart`（DOT 字符串前端渲染，不依赖本地 graphviz） |

### 11.2 诚实边界（延续项目一贯口径）

- **只有 1 个快照点就不画曲线** —— 那只是一张快照，不是传播。明确提示需要至少 2 次采集。
- **悬空父引用不入图** —— 真实采集里父内容可能不在同批数据中，画成孤立节点会误导；悬空数量单独列出。
- **没有粉丝数就不排 KOL 榜**（第三轮修的 #9）。

### 11.3 验证

- 测试 **171 → 182**（新增 `tests/test_propagation.py` 11 条，并强化看板 AppTest 的种子数据，让曲线与关系图分支真的被执行）。
- `demo` 退出 0，落库/告警数字与基线一致（转发链不改变记录条数）。
- 看板 AppTest：6 个 tab 无异常，"选择内容"下拉出现，无 warning。

---

## 十二、双入口可视化：看板端 + 操作端（2026-09-12）

原来只有一个 Streamlit 页面，既是看板又没有任何控制能力 —— 调预警规则、
跑分析全得回命令行。这一轮拆成**两个入口**：

```
web/app.py        入口（st.navigation）
  ├── dashboard.py   📊 看板端 —— 只读
  └── console.py     🎛️ 操作端 —— 可写
```

**为什么要分开**：看板是长时间开着、"随手看一眼"的页面；操作端是改参数、
触发动作的地方。混在一起的话一个误点就可能重跑分析或改掉预警规则。
这也正好落在方案文档 §5.1 那条"存储层 / 展示层"边界上。

### 12.1 操作端（L1~L5）

| 分区 | 能做什么 |
|---|---|
| L1 采集 | 选后端/平台/模式/关键词/上限/二级评论触发采集（mock 或 MediaCrawler）；导入本地文件；查看采集任务队列；一键跑 demo 自检 |
| L2 清洗 | 展示当前生效的采集参数、LLM 就绪状态、去重后端；**广告规则试跑**（输入一条评论，看 `is_spam` / 清洗后文本 / 分词）；脱敏试跑 |
| L3 分析 | 触发情感分析（可指定版本与条数）、主题建模、词云；标注集评估（可切后端）；查看各版本情感分布 |
| L4 存储 | 各表统计、平台/情感分布、数据库连接串 |
| L5 预警 | **规则 CRUD**（等级/窗口/冷却/数量阈值/负面占比/关键词/情感过滤/敏感词开关，改完立即生效，无需重启）；实时跑或回放（since/until/跳过冷却）；实时/日报/仅预览三种推送；最近告警与推送错误 |

动作全部走 `python -m Whochat.cli ...` 子进程执行（复用已编排好的流程，
且崩了不会带走看板），输出原样回显在页面上。长耗时动作会阻塞页面，
页面已提示"终端里跑更直观"。

### 12.2 顺带补的接口

- `Repository.all_rules()`（含禁用规则，管理列表要用）与 `delete_rule()`
- 规则编辑采用"合并回 conditions"的策略：**未知键保留**，将来加规则类型不会
  被编辑器吞掉

### 12.3 验证

- 测试 **182 → 183**：新增 `tests/test_console.py`（操作端 5 个分层 tab +
  规则编辑器渲染无异常）；看板端 AppTest 在 `st.navigation` 下仍为 6 tab。
- 真实服务：`/`、`/dashboard`、`/console` 三个路由均 HTTP 200，启动日志无异常。
  > 更正（§十五）：`/dashboard` **不是**有效路由，当时的 200 是 SPA 外壳的假象。
  > 本次声称的"三路由"实为两条：`/` 与 `/console`。
- `tests/test_dashboard.py` 无需改动 —— 默认页仍是看板端，6 tab 断言继续成立。

---

## 十三、全项目改名 wochat → Whochat（2026-09-12）

内部包名从项目一开始就少了一个 `h`（`wochat`），而仓库名是 `Whochat` ——
**正确的拼写在整个代码库里只出现过 1 次**（README 的命名说明），其余 229 处
`wochat`、`WOCHAT_*` 环境变量、`Wochat` 全是被带偏的写法。这次统一更正。

改动范围（47 个文件）：

| 类别 | 变更 |
|---|---|
| 包目录 | `src/wochat/` → `src/Whochat/`（`git mv`，保留历史） |
| 导入 | `from wochat.x import ...` → `from Whochat.x import ...` |
| 命令 | `python -m wochat.cli ...` → `python -m Whochat.cli ...` |
| 环境变量 | `WOCHAT_*` → `WHOCHAT_*`（`.env` 需同步改名） |
| 默认库文件 | `data/db/wochat.db` → `data/db/Whochat.db` |
| 分发名 | `pyproject.toml` `name = "Whochat"` |
| 文档 | README / WORKLOG / 方案文档同步 |

配套操作：本地库文件已**改名保留**（不丢数据）；卸载旧 `wochat` editable 安装、
清掉旧 `src/wochat.egg-info` 与 `__pycache__`，重新 `pip install -e .`。

验证：`pytest -q` 183 passed / 2 xfailed；`init` / `status` 正常且读到的仍是
原有数据（60 内容 / 1928 评论 / 1164 分析）；`WHOCHAT_DATA_DIR` 隔离 demo 退出 0；
`/`、`/dashboard`、`/console` 三路由 HTTP 200（其中 `/dashboard` 的 200 是假象，见 §十五）。

> ⚠️ 破坏性提示：若你在别处有 `.env` 或外部脚本，`WOCHAT_*` 环境变量名和
> `python -m wochat.cli` 命令都需要一起改。

---

## 十四、看板默认端口 6666 → 8501（2026-09-12）

第一次**真的用浏览器**打开看板时暴露的：`http://localhost:6666/dashboard`
显示"无法访问此页面，网页似乎有问题"，而同一时刻 `curl` 返回 200、
`netstat` 显示正常监听。

### 14.1 原因：6666 是浏览器禁用端口

Chrome / Edge / Firefox 都内置一份**受限端口清单**，6666 在列
（`6665~6669` 原 IRC 端口段，浏览器为防止跨协议攻击而拒绝）。

关键区别：**`curl` / `requests` 不检查这份清单**。所以服务端从头到尾都是好的 ——
`init`、`status`、`pytest`、HTTP 200 全部正常，**唯独人打不开**。

这也说明本轮之前所有"看板 HTTP 200"的验证都不构成"页面可用"的证据：
默认端口从一开始就选错了，只是从来没人用浏览器试过。方案文档 §7.1 那句
"localhost:6666 这个端口可以给 FastAPI"是随手定的，已一并更正。

> 排查时也怀疑过 Clash 代理（实测走 7897 访问 6666 返回 502），但系统代理
> `ProxyEnable=0` 且绕过列表已含 `localhost;127.*`，**代理不是本次原因**。

### 14.2 改动

| 位置 | 变更 |
|---|---|
| `config.py` | 新增 `WebConfig`（`WHOCHAT_DASHBOARD_PORT`，默认 `8501`）+ `dashboard_url` 属性 |
| `cli.py` | `dashboard --port` 默认值改为 `settings.web.port` |
| `alert/notifier.py` | 告警/日报里的"打开看板"链接不再硬编码，改用 `settings.web.dashboard_url` |
| `.env.example` | 新增看板端口段，并写明禁用端口的坑 |
| `web/app.py` | 启动注释 |
| `tests/test_config.py` | 新增 4 条回归用例（含禁用端口清单断言） |
| README / WORKLOG / 方案文档 | 全部 `:6666` 引用 |

新增的回归用例钉住的不是"端口等于 8501"，而是**"默认端口不能落在浏览器禁用清单里"** ——
这样将来有人改成 6667 之类的同样会被拦下。`WebConfig` 用 `default_factory` 而非裸默认值，
否则 dataclass 默认值在导入时就固定了，环境变量改了也不生效（`StoreConfig` 同理）。

### 14.3 验证

- `python -m Whochat.cli dashboard`（不带 `--port`）默认起在 8501，
  `netstat` 显示监听，`/` `/console` 均 `curl` 200
  （当时也测了 `/dashboard` 并拿到 200，但那是假象，见 §十五）
- 8501 不在浏览器禁用端口清单内 —— 这是本次修复的依据
- `pytest -q` **187 passed / 2 xfailed**（183 → 187，新增 4 条），无回归

---

## 十五、`/dashboard` 是一条不存在的路由（2026-09-12）

紧接着 §十四，端口修好后第一次真正用浏览器访问。访问
`http://127.0.0.1:8501/dashboard` 看到：

```
Page not found
The page that you have requested does not seem to exist.
Running the app's main page.
```

然后页面自己"刷新"出来。**这次是代码 bug，不是 Streamlit 的怪癖。**

### 15.1 原因：`url_path` 与 `default=True` 不能并存

`web/app.py` 原来这么写：

```python
st.Page("dashboard.py", title="看板端", icon="📊", url_path="dashboard", default=True)
```

Streamlit 的规定是 **默认页的 `url_path` 恒为空字符串**，实现就一行
（`streamlit/navigation/page.py:428`）：

```python
return "" if self._default else self._url_path
```

官方文档的措辞是 "If you set `default=True`, `url_path` is ignored."
—— **静默忽略，不报错、不警告**。

实测 `.url_path` 拿到的是 `''`，不是 `'dashboard'`。于是看板端实际注册在 `/`，
`/dashboard` 这条路由压根不存在。访问它 → 前端匹配不到任何 page → 弹
"Page not found" → 回退到主页面 → 而主页面**恰好就是看板端自己** →
表现为"报错一下又刷新出来了"。看板端是默认页，所以它总能兜住，看起来像自动恢复。

注册路由实测（`AppTest._registered_pages`）：修复前后都是 `{'', 'console'}` ——
**集合从来没变过**，这恰恰说明问题：写的人以为注册了 `/dashboard`，实际没有。

### 15.2 与 §十四 是同一类错误

两轮踩的是同一个坑：**"服务端 200" 不等于 "这个地址真的能打开"**。

| | §十四 | §十五 |
|---|---|---|
| 现象 | "无法访问此页面" | "Page not found" 后自动恢复 |
| 假证据 | `curl :6666` 返回 200 | `curl /dashboard` 返回 200 |
| 真原因 | 6666 是浏览器禁用端口 | `/dashboard` 从未被注册 |
| 为什么 200 骗人 | curl 不查禁用端口清单 | SPA 任何路径都返回同一个外壳 |

所以 §12.3 / §13 里"`/`、`/dashboard`、`/console` 三路由均 HTTP 200"这句
**从写下的那一刻就是错的** —— 当时并没有三条路由，只有两条。已在原处标注更正。

### 15.3 改动

| 位置 | 变更 |
|---|---|
| `web/app.py` | 去掉被忽略的 `url_path="dashboard"`，加注释说明为何不能写 |
| `web/app.py` | `st.navigation(...).run()` 拆成 `navigator.run()`，便于加注释 |
| `tests/test_web_routes.py` | **新增**（5 条）：AST 检查 + 实跑 AppTest 核对注册路由 |
| README | `/dashboard` → `/`，并写明这个坑 |
| WORKLOG | §12.3 / §13 / §14.3 三处 200 声称就地更正 |

对外承诺的入口现在只有两个，且都真实存在：**`/`（看板端）** 和 **`/console`（操作端）**。

### 15.4 回归测试为什么这么写

关键教训：**「注册路由集合」这个断言在修复前也会通过**（集合一直是
`{'', 'console'}`）。所以钉住它没用。真正要钉的是那个**静默失效的组合**：

- `test_default_page_does_not_pass_url_path` —— 用 AST 检查 app.py，
  禁止同一个 `st.Page` 同时出现 `url_path` 与 `default`
- `TestRegisteredRoutes` —— 实跑一遍，确认注册路由与文档承诺一致
  （防的是反向情况：将来真加了页面却没更新文档）

已实测验证：把旧写法注入回去，AST 那条**立刻失败**并打印出问题调用，
其余 4 条仍通过 —— 与上面的分析一致。

### 15.5 验证

- `pytest -q` **192 passed / 2 xfailed**（187 → 192，新增 5 条），无回归
- 注册路由实测为 `{'', 'console'}`，与 README 承诺一致
- ⚠️ 仍需本人在浏览器里确认 `/` 与 `/console` 均能正常打开、图表渲染正确
- ⚠️ **图表渲染仍待本人用浏览器过目** —— 端口通了不等于图画对了

