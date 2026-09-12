"""操作端 —— L1~L5 的控制与微调（双入口之一，见 web/app.py）。

和「看板端」的分工：

    看板端  只读。只通过 Repository 查询，不写库、不触发任务。
    操作端  可写。触发采集/分析/预警，编辑预警规则，跑回放与推送。

**为什么把两者分开**：看板是要长时间开着、"随手看一眼"的页面；操作端是
改参数、触发动作的地方。混在一起的话，一个误点就可能改了规则或重跑分析。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from Whochat.config import ROOT, settings  # noqa: E402
from Whochat.store.models import Alert, AnalysisResult  # noqa: E402
from Whochat.store.repository import Repository, init_db  # noqa: E402


# ================================================================ 工具


def _run_cli(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    """调用 CLI 子进程并捕获输出。

    走子进程而不是直接调函数：采集/分析这些命令本来就编排好了一整套流程和
    打印，复用 CLI 比在这里重写一遍可靠；而且它们跑在独立进程里，
    崩了不会把看板一起带走。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"  # 否则 GBK 控制台会把 emoji 打死
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "Whochat.cli", *args],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, f"超时（>{timeout}s）。采集/主题建模可能确实很久，去终端看更直观。"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out[-20000:]


def _show_result(code: int, output: str, ok_msg: str = "完成") -> None:
    if code == 0:
        st.success(ok_msg)
    else:
        st.error(f"退出码 {code} —— 命令没有正常完成")
    st.code(output or "（无输出）", language="text")


def _query(fn):
    """开一个短命的 Repository 执行查询 —— 操作端不做缓存，要看到最新状态。"""
    with Repository() as repo:
        return fn(repo)


# ================================================================ 顶部状态


def status_strip() -> None:
    stats = _query(lambda r: r.stats())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("内容", f"{stats['contents']:,}")
    c2.metric("评论", f"{stats['comments']:,}")
    c3.metric("已分析", f"{stats['analyses']:,}")
    c4.metric("快照", f"{stats['snapshots']:,}")
    c5.metric("预警", f"{stats['alerts']:,}")

    bits = [
        f"数据库 `{settings.store.url}`",
        f"采集后端 MediaCrawler {'就绪' if (settings.crawl.mediacrawler_dir / 'main.py').exists() else '未安装'}",
        f"企微 {'已配置' if settings.alert.wecom_webhook else '未配置（dry-run）'}",
        f"情感后端 `{settings.sentiment.backend}`",
        # 用 is_enabled 而不是 enabled：后者是三态（None = 没显式设过，
        # 此时按"配齐了就自动开"判定）。直接读 enabled 会把自动开启误显示成"关"。
        f"LLM 分析 {'开' if settings.llm.is_enabled else '关'}",
    ]
    st.caption(" · ".join(bits))


# ================================================================ L1 采集


def _llm_config_panel() -> None:
    """L2 里的 LLM 配置面板 —— 直接填，不用去手改 .env。

    配置写进 `.env`（项目唯一的配置来源，且已在 .gitignore 里），
    同时刷新当前进程，所以"保存并测试"能立即生效，不必重启看板。
    """
    from Whochat.config import mask_secret, save_llm_settings

    st.markdown("#### LLM 分析（可选）")
    cfg = settings.llm

    if cfg.configured:
        state = "已开启" if cfg.is_enabled else "已配置但被关闭"
        st.caption(
            f"当前：**{state}** · 接口 `{cfg.base_url}` · "
            f"密钥 `{mask_secret(cfg.api_key)}` · 模型 `{cfg.model or '自动选择'}`"
        )
    else:
        st.caption(
            "当前：**未配置** —— 填下面两项就能用。任何 OpenAI 兼容接口都行"
            "（DeepSeek / 通义 / Moonshot / 智谱 / OpenAI / 本地 Ollama 的 `/v1`）。"
        )

    with st.form("llm_config_form"):
        base_url = st.text_input(
            "请求地址 base_url",
            value=cfg.base_url,
            placeholder="https://api.deepseek.com/v1",
            help="只写到域名也行（https://api.deepseek.com），会自动探测 /v1",
        )
        # 密钥永远不回显：value 固定为空，留空即"不修改"
        api_key = st.text_input(
            "API Key",
            value="",
            type="password",
            placeholder=(
                f"已保存 {mask_secret(cfg.api_key)}，留空则不修改"
                if cfg.api_key
                else "sk-xxxxxxxx"
            ),
        )
        model = st.text_input(
            "模型名（可留空）",
            value=cfg.model,
            placeholder="留空 = 自动从 /models 里挑一个对话模型",
        )

        c1, c2, _ = st.columns([1, 1, 2])
        do_save = c1.form_submit_button("保存", type="primary")
        do_test = c2.form_submit_button("保存并测试连接")

    if not (do_save or do_test):
        return

    if not base_url.strip():
        st.error("请求地址不能为空。")
        return
    if not api_key.strip() and not cfg.api_key:
        st.error("首次配置需要填 API Key。")
        return

    path = save_llm_settings(
        base_url, api_key if api_key.strip() else None, model
    )
    st.success(f"已写入 `{path}`（该文件在 .gitignore 里，不会进版本库）")

    if do_test:
        from Whochat.pipeline.llm_client import LLMClient

        with st.spinner("正在请求…（首次会自动探测端点与模型）"):
            ok, msg = LLMClient().available()
        (st.success if ok else st.error)(msg)
        if not ok:
            st.caption(
                "排查顺序：① 地址是否含正确的路径（多数服务端在 `/v1` 下）"
                "② key 是否有权限 ③ 访问境外接口需要代理，"
                "在本机设 `HTTPS_PROXY=http://127.0.0.1:7897` 后重启看板。"
            )


def tab_crawl() -> None:
    st.markdown("### L1 采集")
    st.warning(
        "MediaCrawler 首次运行需要**扫码登录**，浏览器会弹在跑服务的那台机器上。"
        "在远程/无头环境里跑不通 —— 那种情况请在终端执行 `python -m Whochat.cli crawl ...`。"
        "只想验证链路就用 `mock` 后端。"
    )

    with st.form("crawl_form"):
        c1, c2, c3 = st.columns(3)
        source = c1.selectbox("后端", ["mock", "mediacrawler"])
        platform = c2.selectbox(
            "平台",
            ["mock", "douyin", "xhs", "kuaishou", "bilibili", "weibo", "tieba", "zhihu"],
        )
        mode = c3.selectbox("模式", ["keyword", "content_id", "creator"])

        c4, c5, c6 = st.columns(3)
        keyword = c4.text_input("关键词 / ID / 主页", value="某品牌")
        max_items = c5.number_input("最多抓取", min_value=10, max_value=50000, value=500, step=50)
        include_sub = c6.checkbox("抓二级评论", value=True)

        headless = st.checkbox("无头模式（首次登录不能开）", value=False)
        submitted = st.form_submit_button("开始采集", type="primary")

    if submitted:
        args = [
            "crawl",
            "--platform", platform,
            "--keyword", keyword,
            "--source", source,
            "--mode", mode,
            "--max-items", str(int(max_items)),
        ]
        if not include_sub:
            args.append("--no-sub-comments")
        if headless:
            args.append("--headless")
        with st.spinner("采集中…（首次会等扫码登录）"):
            code, out = _run_cli(args)
        _show_result(code, out, "采集完成")

    st.divider()
    st.markdown("#### 导入本地文件")
    with st.form("import_form"):
        c1, c2 = st.columns([3, 1])
        path = c1.text_input("文件路径", placeholder="data/raw/xxx.jsonl")
        platform_i = c2.text_input("平台", value="xhs")
        keyword_i = st.text_input("归因关键词（可空）", value="")
        submitted_i = st.form_submit_button("导入")
    if submitted_i:
        args = ["import", path, "--platform", platform_i]
        if keyword_i.strip():
            args += ["--keyword", keyword_i.strip()]
        with st.spinner("导入中…"):
            code, out = _run_cli(args)
        _show_result(code, out, "导入完成")

    st.divider()
    st.markdown("#### 关键词监控（长期监测）")
    st.caption(
        "一行一个关键词，可多选平台。加入后调度器按间隔**持续**采集 —— "
        "任务跑完会自动排下一次，不像一次性 `crawl` 那样跑完即止。"
    )
    with st.form("watch_form"):
        c1, c2 = st.columns([3, 2])
        watch_text = c1.text_area(
            "关键词（一行一个，# 开头为注释）",
            height=140,
            placeholder="某品牌\n某型号\n竞品名",
        )
        watch_platforms = c2.multiselect(
            "平台",
            ["xhs", "douyin", "kuaishou", "bilibili", "weibo", "tieba", "zhihu"],
            default=["xhs"],
        )
        c3, c4, c5 = st.columns(3)
        watch_interval = c3.number_input(
            "采集间隔（秒）",
            min_value=60,
            max_value=86400,
            value=int(settings.crawl.default_interval_seconds),
            step=60,
        )
        watch_enabled = c4.checkbox("加入后启用", value=True)
        watch_submit = c5.form_submit_button("加入监控", type="primary")

    if watch_submit:
        kws = [
            ln.strip()
            for ln in (watch_text or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not kws:
            st.error("请至少填一个关键词。")
        elif not watch_platforms:
            st.error("请至少选一个平台。")
        else:
            created = updated = 0
            for p in watch_platforms:
                c, u = _query(
                    lambda r, p=p: r.add_watch_tasks(
                        p, kws, interval_seconds=int(watch_interval), enabled=watch_enabled
                    )
                )
                created += c
                updated += u
            st.success(
                f"已加入 {len(kws)} 个词 × {len(watch_platforms)} 个平台："
                f"新建 {created}，更新 {updated}"
            )
            st.rerun()

    summary = _query(lambda r: r.watch_summary())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("监控任务", summary["total"])
    m2.metric("启用", summary["enabled"])
    m3.metric("待跑", summary["due"])
    m4.metric("连续失败", summary["failing"])
    if summary["failing"]:
        st.warning(
            "有任务在连续失败。采集被风控是常态，系统会自动退避重试；"
            "若持续多天零产出，检查登录态或换个采集时段。"
        )
    if summary["due"] > 0 and summary["enabled"] > 0:
        st.caption(
            f"待跑 {summary['due']} 个：说明上一轮没跑完/间隔偏短。"
            "提高 `WHOCHAT_CRAWL_CONCURRENCY` 或调大间隔，否则监测频率会走低。"
        )

    tasks = _query(lambda r: r.list_crawl_tasks(limit=1000))
    if tasks:
        import pandas as pd

        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "task_id": t.task_id,
                        "平台": t.platform,
                        "目标": t.target,
                        "状态": t.status,
                        "启用": bool(t.enabled),
                        "间隔(s)": t.interval_seconds,
                        "失败": t.consecutive_failures,
                        "下次": t.next_run_at.strftime("%m-%d %H:%M") if t.next_run_at else "立即",
                        "错误": (t.error or "")[:40],
                    }
                    for t in tasks
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        labels = {t.task_id: f"{t.platform} / {t.target}" for t in tasks}
        sel = st.selectbox(
            "选择任务进行启停 / 删除", options=list(labels),
            format_func=lambda x: labels.get(x, x),
        )
        b1, b2, _ = st.columns([1, 1, 3])
        if b1.button("启用 / 停用"):
            current = next(t for t in tasks if t.task_id == sel)
            _query(lambda r: r.set_task_enabled(sel, not bool(current.enabled)))
            st.rerun()
        if b2.button("删除该任务"):
            _query(lambda r: r.delete_crawl_tasks(task_ids=[sel]))
            st.warning(f"已删除 {labels.get(sel, sel)}")
            st.rerun()
    else:
        st.caption(
            "还没有监控任务。上面填关键词加入，或用 CLI："
            "`python -m Whochat.cli keywords add --platform xhs --file kws.txt`"
        )

    st.divider()
    st.markdown("#### 链路自检")
    st.caption("造一批确定性 mock 数据跑通「采集→清洗→分析→预警→词云」，不需要爬虫和模型。")
    if st.button("跑一遍 demo"):
        with st.spinner("跑 demo 中…"):
            code, out = _run_cli(["demo"])
        _show_result(code, out, "demo 完成")
        st.cache_data.clear()


# ================================================================ L2 清洗


def tab_clean() -> None:
    st.markdown("### L2 清洗")
    st.caption("清洗参数来自 `.env`，改完需要重启看板。这里展示**当前生效值**，并提供规则试跑。")

    from Whochat.crawler.base import anonymize_id
    from Whochat.pipeline import dedup as dedup_mod
    from Whochat.pipeline.rules import clean, is_spam, tokenize

    c1, c2, c3 = st.columns(3)
    c1.metric("采集间隔（秒）", settings.crawl.min_interval)
    c2.metric("单次上限", settings.crawl.max_items)
    c3.metric("二级评论", "开" if settings.crawl.include_sub_comments else "关")

    has_minhash = dedup_mod.minhash_dedupe(["文本"]) is not None
    st.caption(
        f"近重复去重：{'MinHash/LSH' if has_minhash else 'SimHash 回退（未装 datasketch）'}"
    )

    _llm_config_panel()

    st.divider()
    st.markdown("#### 规则试跑")
    text = st.text_area("输入一条评论", value="加V信 abc12345 领取优惠券", height=80)
    if text:
        spam = is_spam(text)
        c1, c2 = st.columns(2)
        c1.metric("判定广告", "是" if spam else "否")
        c1.caption("广告评论会被写 is_valid=False，排除出情感/主题统计。")
        c2.markdown("**清洗后文本**")
        c2.code(clean(text) or "（空）")
        st.markdown("**分词**")
        st.code(" / ".join(tokenize(text)) or "（无）")

    st.divider()
    st.markdown("#### 脱敏试跑")
    raw = st.text_input("原始平台用户 ID", value="RAW_USER_123")
    st.code(f"{anonymize_id(raw)}", language="text")
    st.caption("采集层只落这个哈希，原始 ID 不写库（raw_json 除外，那是留档的明知取舍）。")


# ================================================================ L3 分析


def tab_analyze() -> None:
    st.markdown("### L3 分析")
    st.caption(
        f"当前情感后端 `{settings.sentiment.backend}`（改后端要改 `.env` 再重启）。"
        "分析结果按 `analysis_version` 多版本并存，重跑不会覆盖旧版本。"
    )

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 情感分析")
        with st.form("analyze_form"):
            version = st.text_input("版本号（留空按后端自动生成）", value="")
            limit = st.number_input("最多分析条数", min_value=1, max_value=1000000, value=100000)
            sub = st.form_submit_button("跑分析", type="primary")
        if sub:
            args = ["analyze", "--limit", str(int(limit))]
            if version.strip():
                args += ["--version", version.strip()]
            with st.spinner("清洗 + 去重 + 情感分析中…"):
                code, out = _run_cli(args)
            _show_result(code, out, "分析完成")
            st.cache_data.clear()

        st.markdown("#### 标注集评估")
        with st.form("eval_form"):
            ev_path = st.text_input("标注集", value="tests/annotated_sample.json")
            backend = st.selectbox("后端", ["", "lexicon", "transformer"], format_func=lambda x: x or "跟随 .env")
            sub_e = st.form_submit_button("评估准确率")
        if sub_e:
            args = ["evaluate", ev_path]
            if backend:
                args += ["--backend", backend]
            with st.spinner("评估中…"):
                code, out = _run_cli(args)
            _show_result(code, out, "评估完成")

    with c2:
        st.markdown("#### 主题建模")
        with st.form("topics_form"):
            t_keyword = st.text_input("只看某个监控词（可空）", value="")
            min_topic = st.number_input("最小主题规模", min_value=2, max_value=100, value=5)
            sub_t = st.form_submit_button("跑主题建模")
        if sub_t:
            args = ["topics", "--min-topic-size", str(int(min_topic))]
            if t_keyword.strip():
                args += ["--keyword", t_keyword.strip()]
            with st.spinner("BERTopic 建模中（可能较慢）…"):
                code, out = _run_cli(args)
            _show_result(code, out, "主题建模完成")
            st.cache_data.clear()

        st.markdown("#### 词云")
        with st.form("wc_form"):
            top_n = st.number_input("词数", min_value=20, max_value=1000, value=150)
            sub_w = st.form_submit_button("生成词云")
        if sub_w:
            with st.spinner("生成中…"):
                code, out = _run_cli(["wordcloud", "--top-n", str(int(top_n))])
            _show_result(code, out, "词云完成")

    st.divider()
    st.markdown("#### 分析版本")
    versions = _query(
        lambda r: sorted({v for (v,) in r.session.execute(select(AnalysisResult.analysis_version))})
    )
    if versions:
        import pandas as pd

        data = []
        for v in versions:
            dist = _query(lambda r, v=v: r.sentiment_distribution(v))
            data.append({"版本": v, "有效分析": sum(dist.values()), **{k: dist.get(k, 0) for k in ("positive", "neutral", "negative")}})
        st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True)
    else:
        st.info("还没有分析结果。先跑 demo 或采集后跑分析。")


# ================================================================ L4 存储


def tab_store() -> None:
    st.markdown("### L4 存储")
    stats = _query(lambda r: r.stats())
    st.json(stats)
    st.caption(f"数据库：`{settings.store.url}`　|　迁移 PostgreSQL 只需改 `WHOCHAT_DB_URL`。")

    st.divider()
    st.markdown("#### 数据分布")
    c1, c2 = st.columns(2)
    versions = _query(
        lambda r: sorted({v for (v,) in r.session.execute(select(AnalysisResult.analysis_version))})
    )
    if versions:
        import pandas as pd

        latest = versions[-1]
        with c1:
            st.markdown(f"**平台分布**（{latest}）")
            pdist = _query(lambda r, v=latest: r.platform_distribution(v))
            if pdist:
                st.bar_chart(pd.DataFrame({"平台": list(pdist), "数量": list(pdist.values())}).set_index("平台"))
        with c2:
            st.markdown(f"**情感分布**（{latest}）")
            dist = _query(lambda r, v=latest: r.sentiment_distribution(v))
            if dist:
                st.bar_chart(pd.DataFrame({"情感": list(dist), "数量": list(dist.values())}).set_index("情感"))
    else:
        st.caption("暂无分析数据。")


# ================================================================ L5 预警


def _conditions_from_form(cond: dict, prefix: str) -> dict:
    """把表单值合并回 conditions —— 未知键保留，便于将来扩展规则类型。"""
    out = dict(cond)

    threshold = st.number_input(
        "数量阈值（0 表示不启用）", min_value=0, max_value=100000,
        value=int(cond.get("threshold") or 0), key=f"{prefix}_th",
    )
    if threshold > 0:
        out["threshold"] = int(threshold)
    else:
        out.pop("threshold", None)

    ratio = st.number_input(
        "负面占比阈值（0 表示不启用）", min_value=0.0, max_value=1.0,
        value=float(cond.get("negative_ratio") or 0.0), step=0.05, key=f"{prefix}_ratio",
    )
    if ratio > 0:
        out["negative_ratio"] = round(float(ratio), 4)
    else:
        out.pop("negative_ratio", None)

    keywords = st.text_input(
        "关键词（逗号分隔，可空）", value="，".join(cond.get("keywords") or []).replace("，", ","),
        key=f"{prefix}_kw",
    )
    kws = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
    if kws:
        out["keywords"] = kws
    else:
        out.pop("keywords", None)

    sentiments = st.multiselect(
        "情感过滤", ["negative", "neutral", "positive"],
        default=[s for s in (cond.get("sentiments") or []) if s in ("negative", "neutral", "positive")],
        key=f"{prefix}_sent",
    )
    if sentiments:
        out["sentiments"] = sentiments
    else:
        out.pop("sentiments", None)

    use_sensitive = st.checkbox(
        "使用 dicts/sensitive_words.txt 作为关键词", value=bool(cond.get("use_sensitive_words")),
        key=f"{prefix}_sw",
    )
    if use_sensitive:
        out["use_sensitive_words"] = True
    else:
        out.pop("use_sensitive_words", None)

    return out


def tab_alert() -> None:
    from Whochat.alert.notifier import WeComNotifier, build_daily_digest
    from Whochat.alert.rules_engine import RuleEngine, seed_default_rules

    st.markdown("### L5 预警")
    st.caption(
        "快通道规则引擎无模型、秒级。规则存在 `alert_rules` 表里，**改完立即生效**，"
        "不需要重启（词表是评估时读文件的）。"
    )

    if st.button("恢复/补齐默认规则"):
        n = _query(lambda r: seed_default_rules(r))
        st.success(f"已写入 {n} 条默认规则")
        st.rerun()

    rules = _query(lambda r: r.all_rules())
    for rule in rules:
        cond = rule.conditions or {}
        flag = "🟢" if rule.enabled else "⚪"
        with st.expander(f"{flag} [{rule.level.upper()}] {rule.name}　`{rule.rule_id}`"):
            with st.form(f"rule_{rule.rule_id}"):
                name = st.text_input("名称", value=rule.name)
                c1, c2, c3 = st.columns(3)
                level = c1.selectbox(
                    "等级", ["red", "orange", "yellow", "blue"],
                    index=["red", "orange", "yellow", "blue"].index(rule.level)
                    if rule.level in ("red", "orange", "yellow", "blue") else 2,
                )
                window = c2.number_input("窗口（秒）", min_value=10, max_value=86400, value=int(cond.get("window_seconds", 300)), step=30)
                cooldown = c3.number_input("冷却（秒）", min_value=0, max_value=86400, value=int(rule.cooldown_seconds or 0), step=30)
                new_cond = _conditions_from_form(cond, rule.rule_id)
                new_cond["window_seconds"] = int(window)
                enabled = st.checkbox("启用", value=rule.enabled)

                s1, s2 = st.columns([1, 1])
                save = s1.form_submit_button("保存", type="primary")
                delete = s2.form_submit_button("删除")
            if save:
                _query(lambda r: r.upsert_rule(
                    rule.rule_id, name, new_cond,
                    level=level, cooldown_seconds=int(cooldown),
                    channels=rule.channels or ["wecom"], enabled=enabled,
                ))
                st.success("已保存")
                st.rerun()
            if delete:
                _query(lambda r: r.delete_rule(rule.rule_id))
                st.warning(f"已删除规则 {rule.rule_id}")
                st.rerun()

    with st.expander("➕ 新增规则"):
        with st.form("new_rule"):
            c1, c2, c3 = st.columns(3)
            rid = c1.text_input("rule_id（英文/下划线）", value="my_rule")
            rname = c2.text_input("名称", value="我的规则")
            rlevel = c3.selectbox("等级", ["red", "orange", "yellow", "blue"], index=2)
            new_cond = _conditions_from_form({}, "new")
            c4, c5 = st.columns(2)
            window = c4.number_input("窗口（秒）", min_value=10, max_value=86400, value=3600, step=30)
            cooldown = c5.number_input("冷却（秒）", min_value=0, max_value=86400, value=1800, step=30)
            ok = st.form_submit_button("创建", type="primary")
        if ok:
            if not rid.strip():
                st.error("rule_id 不能为空")
            else:
                new_cond["window_seconds"] = int(window)
                _query(lambda r: r.upsert_rule(
                    rid.strip(), rname, new_cond,
                    level=rlevel, cooldown_seconds=int(cooldown),
                    channels=["wecom"], enabled=True,
                ))
                st.success(f"已创建 {rid}")
                st.rerun()

    st.divider()
    st.markdown("#### 跑一遍 / 回放")
    c1, c2, c3, c4 = st.columns(4)
    mode = c1.radio("模式", ["实时", "回放"], horizontal=True)
    since_h = c2.number_input("回放起点（小时前）", min_value=1, max_value=8760, value=24, disabled=mode != "回放")
    until_h = c3.number_input("回放终点（小时前）", min_value=0, max_value=8760, value=0, disabled=mode != "回放")
    skip_cd = c4.checkbox("跳过冷却", value=True, disabled=mode != "回放")

    if st.button("执行规则引擎", type="primary"):
        from datetime import timedelta

        from Whochat.store.models import utcnow

        def _run(repo):
            engine = RuleEngine(repo)
            kwargs = {}
            if mode == "回放":
                kwargs = {
                    "since": utcnow() - timedelta(hours=int(since_h)),
                    "until": utcnow() - timedelta(hours=int(until_h)),
                    "skip_cooldown": skip_cd,
                }
            return engine.run_and_record(**kwargs)

        ids = _query(_run)
        st.success(f"触发 {len(ids)} 条告警")
        if ids:
            st.code("\n".join(ids))

    st.divider()
    st.markdown("#### 推送")
    notifier_state = "已配置 webhook" if settings.alert.wecom_webhook else "未配置 webhook（dry-run，只打印到服务端日志）"
    st.caption(f"企微：{notifier_state}。发送失败的告警会保持 pending 自动重试，不会丢。")

    p1, p2, p3 = st.columns(3)
    if p1.button("实时通道 flush"):
        result = _query(lambda r: WeComNotifier(repo=r).flush())
        st.info(f"{result.status} — {result.message}")
    if p2.button("合并日报 flush"):
        result = _query(lambda r: WeComNotifier(repo=r).flush(digest=True))
        st.info(f"{result.status} — {result.message}")
    if p3.button("只看日报内容"):
        st.code(_query(lambda r: build_daily_digest(r)), language="markdown")

    st.divider()
    st.markdown("#### 最近告警")
    alerts = _query(
        lambda r: [
            {
                "时间": a.trigger_time.strftime("%m-%d %H:%M") if a.trigger_time else "",
                "等级": a.level,
                "规则": a.rule_id,
                "原因": a.reason,
                "命中": a.match_count,
                "推送": a.push_status or "-",
                "错误": (a.push_error or "")[:40],
            }
            for a in r.session.scalars(
                select(Alert).order_by(Alert.trigger_time.desc()).limit(50)
            )
        ]
    )
    if alerts:
        import pandas as pd

        st.dataframe(pd.DataFrame(alerts), width="stretch", hide_index=True)
    else:
        st.caption("暂无告警。")


# ================================================================ 主流程


def main() -> None:
    init_db()

    st.title("🎛️ 操作端")
    st.caption(
        "**这个页面会写库、触发任务。** 只想看结果请切到左侧「看板端」。"
        "长耗时动作（采集/主题建模）会阻塞页面，终端里跑更直观。"
    )

    status_strip()
    st.divider()

    tabs = st.tabs(["L1 采集", "L2 清洗", "L3 分析", "L4 存储", "L5 预警"])
    with tabs[0]:
        tab_crawl()
    with tabs[1]:
        tab_clean()
    with tabs[2]:
        tab_analyze()
    with tabs[3]:
        tab_store()
    with tabs[4]:
        tab_alert()


main()
