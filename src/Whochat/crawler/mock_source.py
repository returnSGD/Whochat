"""造数据后端 —— 用于在爬虫跑通之前验证整条链路。

**为什么需要它**：爬虫是整个项目最不稳定的一环（403 / 滑块 / 登录态失效是常态）。
如果整条链路的验证都依赖爬虫，那你会把大量时间花在排查采集问题上，
而不是验证清洗 / 分析 / 看板 / 预警这些真正的业务逻辑。

有了 MockSource，你可以：
    1. 立刻端到端跑通，看到看板出图
    2. 调试情感阈值 / 预警规则 / 聚合窗口，不用等真数据
    3. 作为回归测试的固定数据集（seed 固定，结果可复现）

确定性：使用固定 seed，同样参数每次产出完全一样。
"""

from __future__ import annotations

import random
import zlib
from datetime import timedelta
from typing import Iterator

from Whochat.crawler.base import (
    CrawlResult,
    CrawlTask,
    clean_text_basic,
    comment_record,
    content_record,
)
from Whochat.store.models import utcnow

SUPPORTED = ["douyin", "xhs", "bilibili", "weibo", "kuaishou", "zhihu", "tieba", "mock"]

# ---------------------------------------------------------------- 语料

# 正面
_POSITIVE = [
    "这个功能真的太好用了，终于解决了我一直以来的痛点",
    "更新之后流畅多了，给开发团队点赞",
    "用了三个月，稳定性没得说，推荐",
    "性价比很高，比同价位的强不少",
    "客服响应很快，问题当天就解决了，体验很好",
    "外观设计很用心，细节到位，满意",
    "这次升级诚意满满，能看出是真的在听用户反馈",
    "续航比上一代提升明显，一天重度使用没问题",
    "价格合适，质量超出预期，会回购",
    "上手很简单，家里老人也会用",
]

# 负面
_NEGATIVE = [
    "用了三天就出问题了，质量堪忧",
    "客服根本联系不上，售后太差了",
    "这个价格配这个做工，纯属智商税",
    "更新完直接卡死，越更越烂",
    "宣传和实际差太多了，被坑了",
    "刚买一个月就降价，老用户不是人吗",
    "发热严重，玩游戏十分钟就烫手",
    "售后推诿扯皮，问题拖了半个月没解决",
    "这个bug反馈多少次了还是没修，太失望了",
    "同价位有更好的选择，不推荐买这个",
]

# 中性 / 询问 / 陈述
_NEUTRAL = [
    "请问这个支持以旧换新吗",
    "多少钱？在哪能买到",
    "和上一代相比主要区别在哪",
    "我上周买的，还在观察中",
    "看评测说还行，再观望一下",
    "有没有人知道什么时候补货",
    "已经下单了，等到了再说",
    "这个型号和那个型号哪个更合适",
    "路过看看，暂时没有需求",
    "有人对比过其他品牌吗",
]

# 反讽 —— 情感分析的经典难点，故意混进去用来暴露模型短板
_SARCASTIC = [
    "这手机真好用，用了三天就送修了",
    "售后服务太棒了，等了两周终于有人接电话",
    "质量真稳定，稳定地坏",
]

# 广告 / 水军 —— 清洗阶段应该被过滤掉
_SPAM = [
    "加V信 xxx888 有优惠 全场五折 详情私聊",
    "本公司专业代运营，需要的私信我，价格优惠",
    "点击链接领取优惠券 http://spam.example.com/abc",
]

_PLATFORMS = ["douyin", "xhs", "bilibili", "weibo", "kuaishou", "zhihu", "tieba"]

_TITLES = [
    "{kw}到底值不值得买？实测一周后说说真实感受",
    "关于{kw}，这几个问题你必须知道",
    "{kw}翻车了？我遇到的几个问题",
    "{kw}上手体验：优点和缺点都很明显",
    "为什么大家都在讨论{kw}",
    "{kw}用了半年的真实评价",
    "{kw}和同类产品对比，差距在哪",
    "{kw}售后经历分享，希望能帮到大家",
]

# 正文按"讨论维度"分池 —— 真实舆情里不同帖子讨论的侧面不同。
# 如果所有正文都是同一个模板，主题建模会得到 0 个簇
# （向量几乎相同，HDBSCAN 找不出结构）。
_BODY_TEMPLATES = [
    "{kw}的质量问题最近讨论很多，主要集中在做工细节和使用寿命上。我这台用了两个月，出现了明显的外观磨损。",
    "{kw}的价格波动让老用户很不满，刚买完就降价，而且没有任何补偿方案，这一点确实让人寒心。",
    "{kw}的售后服务体验分享：客服响应速度慢，问题转接多次仍未解决，希望官方能重视售后体系建设。",
    "{kw}的功能设计整体不错，日常使用场景覆盖得比较全，几个常用的功能做得很顺手。",
    "{kw}的性价比分析：对比同价位的几款产品，配置和做工都还算有竞争力，适合预算有限的用户。",
    "{kw}的物流速度快，包装完好，开箱体验不错，配件给得也算齐全。",
    "{kw}的系统更新这次改动挺大，界面更清爽了，但有几个常用功能的位置变了，需要适应一下。",
    "{kw}的续航表现是这次讨论的焦点，实测一天中度使用能坚持到晚上，重度使用撑不到下班。",
    "{kw}的发热问题在夏天比较明显，长时间玩游戏机身温度会升得比较高。",
    "{kw}的品控最近被吐槽较多，有用户反馈收到的产品存在缝隙不均的情况。",
    "{kw}的用户手册写得太简略，很多功能需要自己摸索，希望官方能出详细教程。",
    "{kw}的客服态度其实还可以，至少回复及时，但技术层面的问题解决能力有待提高。",
]

# 变体词缀 —— 让语料有足够的文本差异。
# 真实评论区不会只有十句话，如果 mock 全是重复模板，
# 去重会把 90% 的数据干掉，demo 就看不出趋势了。
#
# ⚠️ 前缀里刻意**不含否定词**（不/没/别/无）：词典情感分析有否定词窗口，
#    "不吹不黑，这个真好用"这种会被误判成负面，干扰 demo 的可读性。
_PREFIXES = [
    "", "", "",  # 多数不加前缀，符合真实分布
    "说实话，", "讲真，", "个人感觉，", "客观来讲，", "用了一周，",
    "刚收到货，", "总体来看，", "补充一句，", "给后来人提个醒，",
    "我是首发买的，", "对比了同价位之后，", "看了很多评测，",
]

_SUFFIXES = [
    "", "", "",
    "，希望厂家重视", "，仅供参考", "，大家怎么看", "，欢迎讨论",
    "，已经反馈给客服了", "，等后续处理", "，就这样吧", "，唉",
    "，有同样情况的吗", "，坐标上海", "，先观察观察",
]

_TAILS = ["", "", "", "", "~", "。", "！", "……", "（真实体验）", "（用了两周）", "（非水军）"]

# 平台偏置：不同平台的用户构成不同，造数据时体现差异，
# 避免下游误以为"各平台情绪分布应该一样"
_PLATFORM_BIAS = {
    "weibo": {"negative": 0.45, "positive": 0.30, "neutral": 0.25},
    "douyin": {"negative": 0.38, "positive": 0.37, "neutral": 0.25},
    "xhs": {"negative": 0.25, "positive": 0.55, "neutral": 0.20},
    "bilibili": {"negative": 0.30, "positive": 0.45, "neutral": 0.25},
    "zhihu": {"negative": 0.33, "positive": 0.34, "neutral": 0.33},
    "kuaishou": {"negative": 0.35, "positive": 0.40, "neutral": 0.25},
    "tieba": {"negative": 0.42, "positive": 0.28, "neutral": 0.30},
}


class MockSource:
    """确定性造数据后端。"""

    name = "mock"

    def supports(self, platform: str) -> bool:
        return platform in SUPPORTED

    def crawl(self, task: CrawlTask) -> Iterator[dict]:
        result = self._generate(task)
        yield from result.contents
        yield from result.comments

    # ------------------------------------------------------------

    def _generate(self, task: CrawlTask) -> CrawlResult:
        # 用 task 内容做种子 —— 同参数必同结果，可复现。
        #
        # ⚠️ 必须用 zlib.crc32 而不是内置 hash()：内置 hash() 对 str/bytes
        #    有**进程级随机盐**（PYTHONHASHSEED），同一个 task 在不同进程里
        #    种子不同 → 造出的 comment_id 也不同 → upsert 只增不改，
        #    反复跑 demo 会让 SQLite 里的数据不断累积成历次运行的并集。
        #    后果：预警按全库扫描，报出的命中数远超本次分析量（实测
        #    "12 小时窗口命中 3296 条"而本次只落库 1956 条），
        #    且数值每次运行都变，demo 失去回归验证的意义。
        seed_key = f"{task.platform}|{task.mode}|{task.target}".encode("utf-8")
        seed = zlib.crc32(seed_key) & 0xFFFFFFFF
        rng = random.Random(seed)

        result = CrawlResult()
        now = utcnow()
        kw = task.target

        # 一次舆情事件的时间跨度：默认造 72 小时的数据
        span_hours = task.extra.get("span_hours", 72)
        # 爆发点：距现在多少小时。默认 42 小时前
        # → "平时平静、第 30 小时突然爆发、之后回落"的典型形态
        burst_hours_ago = span_hours - task.extra.get("burst_hour", 30)

        # 爆发窗口宽度（小时）
        burst_width = task.extra.get("burst_width", 4)
        # 多大比例的内容落在爆发窗口内
        burst_share = task.extra.get("burst_share", 0.4)

        # 内容数决定主题建模的文档数（评论按 content_id 聚合后建模），
        # 所以不能太少 —— 30 篇文档跑 BERTopic 只会得到一堆碎片主题
        n_contents = max(10, min(80, task.max_items // 10 or 10))
        n_burst_contents = max(2, int(n_contents * burst_share))
        platforms = [task.platform] if task.platform != "mock" else _PLATFORMS

        # 传播链：记录已生成内容的发布时间，后面部分内容可以"转发"更早的内容。
        # 之前 mock 把 parent_content_id 恒置 None，导致传播路径这条链路
        # 在 demo 里从来没被跑过 —— 看板做出来也没有数据可验证。
        all_cids: list[str] = []
        content_publish: dict = {}

        for i in range(n_contents):
            platform = rng.choice(platforms)
            cid = f"{platform}_c{i:04d}"

            # 显式构造爆发：一部分内容压在爆发窗口内，其余均匀铺开。
            # 不能靠随机碰运气 —— 那样趋势图上看不出峰值，demo 就失去意义了。
            is_burst = i < n_burst_contents
            if is_burst:
                hours_ago = burst_hours_ago + rng.uniform(-burst_width, burst_width)
            else:
                hours_ago = rng.uniform(0, span_hours)
            hours_ago = max(0.0, hours_ago)
            publish = now - timedelta(hours=hours_ago)

            followers = rng.choice([120, 800, 3500, 25000, 180000, 1200000])
            verified = followers > 100000

            # 越靠近爆发点，内容热度越高（点赞/转发量随距爆发点的距离衰减）
            heat = max(1.0, 10.0 - abs(hours_ago - burst_hours_ago) / 3.0)

            # 约 1/3 的内容引用更早发布的内容（转发/引用）。只能引用已生成且
            # 发布时间更早的，保证 parent → child 方向正确、且没有悬空父引用。
            parent_cid = None
            if i >= 5 and i % 3 == 1:
                candidates = [c for c in all_cids if content_publish[c] < publish]
                if candidates:
                    parent_cid = candidates[-1]

            result.contents.append(
                content_record(
                    content_id=cid,
                    platform=platform,
                    search_keyword=kw,
                    content_type=rng.choice(["video", "note", "post"]),
                    title=rng.choice(_TITLES).format(kw=kw),
                    body_text=rng.choice(_BODY_TEMPLATES).format(kw=kw),
                    url=f"https://example.com/{platform}/{cid}",
                    publish_time=publish,
                    author_raw_id=f"author_{platform}_{i}",
                    author_name=f"用户{rng.randint(1000, 9999)}",
                    author_follower_count=followers,
                    author_verified=verified,
                    like_count=int(rng.randint(10, 5000) * heat),
                    comment_count=int(rng.randint(5, 800) * heat),
                    share_count=int(rng.randint(0, 300) * heat),
                    parent_content_id=parent_cid,
                    raw_json={"mock": True, "seed": seed},
                )
            )
            all_cids.append(cid)
            content_publish[cid] = publish

            # 爆发内容配的评论多得多 —— 这才是"爆发"该有的样子
            n_comments = rng.randint(40, 100) if is_burst else rng.randint(4, 15)
            bias = _PLATFORM_BIAS.get(platform, {"negative": 0.33, "positive": 0.34, "neutral": 0.33})
            # 本内容最近一条真实产出的评论 ID —— 二级评论挂它，保证不断链
            last_cid = None

            for j in range(n_comments):
                # 评论集中在内容发布后 4 小时内（真实舆情就是这个形态）
                comment_hours = max(0.0, hours_ago - rng.uniform(0, 4))
                if not is_burst and rng.random() < 0.45:
                    continue  # 非爆发期再稀疏采样一次

                roll = rng.random()
                if roll < 0.02:
                    text = rng.choice(_SPAM)
                elif roll < 0.05:
                    text = rng.choice(_SARCASTIC)
                elif roll < 0.05 + bias["negative"] * 0.95:
                    text = rng.choice(_NEGATIVE)
                elif roll < 0.05 + (bias["negative"] + bias["positive"]) * 0.95:
                    text = rng.choice(_POSITIVE)
                else:
                    text = rng.choice(_NEUTRAL)

                # 加变体词缀。情感词都在句子里，词缀不含否定词，极性不变
                text = rng.choice(_PREFIXES) + text + rng.choice(_SUFFIXES) + rng.choice(_TAILS)

                parent = None
                level = 1
                # 二级评论必须挂到**同内容上一条真实产出的评论**上。
                # 三点都要注意：
                #   1) 填充位数与下面 comment_id 的 :03d 一致，否则写成 "_cm5"
                #      而真实 ID 是 "_cm005"，传播/线程分析永远找不到父评论；
                #   2) 不能直接引用 j-1 —— 上面的稀疏采样会跳号，
                #      被跳过的评论不存在，会留下悬空父引用；
                #   3) 没有前序评论时留一级，避免自引用。
                if task.include_sub_comments and last_cid is not None and rng.random() < 0.25:
                    parent = last_cid
                    level = 2

                result.comments.append(
                    comment_record(
                        comment_id=f"{platform}_c{i:04d}_cm{j:03d}",
                        content_id=cid,
                        platform=platform,
                        text=text,
                        author_raw_id=f"u_{platform}_{i}_{j}",
                        parent_comment_id=parent,
                        level=level,
                        publish_time=now - timedelta(hours=comment_hours),
                        author_follower_count=rng.choice([3, 50, 400, 5000, 60000]),
                        like_count=rng.randint(0, 300),
                        reply_count=rng.randint(0, 20),
                        ip_location=rng.choice(
                            ["广东", "北京", "上海", "浙江", "江苏", "四川", "山东", "湖北", None]
                        ),
                        raw_json={"mock": True},
                    )
                )
                # 只有一级评论才更新锚点：二级评论挂到一级评论上（真实评论区的形态），
                # 否则会形成二级接二级的链，层级语义就乱了。
                if level == 1:
                    last_cid = f"{platform}_c{i:04d}_cm{j:03d}"

        return result


def register_mock() -> MockSource:
    from Whochat.crawler.base import register

    return register(MockSource())
