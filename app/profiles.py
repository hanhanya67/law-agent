# -*- coding: utf-8 -*-
"""可插拔法律内核（v119）。

一句话说清这层在解决什么：v118 之前，引擎本身是领域无关的（检索按
`by_law` 分组、题型分流只认问法、条号解析只认「第X条」），但**答案链路上
有六处把劳动法写死了**——提示词、定性规则表、案情槽位、专用计算、
办案包五块、意图边界。所以「换一份文档」并不能换一个领域。

这里把那六处收敛成一个 `LawProfile`，每个内核是一份配置 + 一份数据。
三条硬规矩，都是被回归套件逼出来的：

1. **劳动法必须是默认内核，且默认路径逐字保持 v118 的行为。**
   现有 31 个套件里有 `set(extras) == 五块` 这种「恰好等于」断言，
   也有逐字比对提示词常量的断言。多加一个键、多换一个字都会红。
   所以非劳动法内核走的是**独立分支**，绝不回头改劳动法那条路。
2. **内核只能显式选，不能从提问里猜。** 和「不从语气猜身份」同一个道理：
   猜错内核等于拿劳动法条文去答食品安全问题，比答不上来更糟。
   非法取值一律回落劳动法，不「就近找一个」。
3. **会话绑定，不搞全局开关。** 全局切换会让两个浏览器标签互相改对方的内核；
   contextvar 在线程内隔离，chat 的 worker 线程里显式 `use()` 一次即可。

为什么内核切换顺便收窄工具表：`tools.py` 开头写着「超过 10 个模型选错率
显著上升」。v118 是 9 个工具挤在一个表里，其中 4 个是劳动争议专用。
按内核只挂 5 个通用工具，既解决了串味，也把这个老约束真正落地了。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

DEFAULT_ID = "labor"

# v118 全量语料里的法律简称。劳动法内核**故意不做过滤**：民法典 / 合同法
# 一直在库里参与检索（只是被 GENERIC_BOOST 降权），版本题「合同法第114条
# 还有效吗」必须能检到那部已废止的法。一刀切过滤会把这个功能直接砍掉。
LABOR_LAWS = frozenset({
    "劳动法", "劳动合同法", "实施条例", "调解仲裁法",
    "劳动争议解释一", "劳动争议解释二", "工伤保险条例",
    "民法典", "合同法",
})

_GENERIC_TOOLS = (
    "search_statute", "get_article", "check_validity",
    "expand_citation", "ask_user",
)
_LABOR_ONLY_TOOLS = (
    "calculate_compensation", "query_region_params",
    "calculate_deadline", "draft_document",
)

_STANCES_LABOR = (
    {"value": "employee", "label": "我是职工", "hint": "以劳动者为申请人视角给参考"},
    {"value": "employer", "label": "我是单位 HR", "hint": "以用人单位视角看合规与风险"},
    {"value": "agent", "label": "我是代理人", "hint": "代他人咨询，仍按劳动者视角给参考"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则与案例"},
)
_STANCES_CONSUMER = (
    {"value": "buyer", "label": "我是消费者", "hint": "以购买者视角看能主张什么"},
    {"value": "seller", "label": "我是经营者", "hint": "以商家视角看合规与风险"},
    {"value": "agent", "label": "我是代理人", "hint": "代他人咨询，仍按消费者视角给参考"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则"},
)
_STANCES_CIVIL = (
    {"value": "party_a", "label": "我是签约一方", "hint": "以合同当事人视角看权利与义务"},
    {"value": "party_b", "label": "我是相对方", "hint": "以另一方视角看抗辩与风险"},
    {"value": "agent", "label": "我是代理人", "hint": "代他人咨询，仍按当事人视角给参考"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则"},
)
_STANCES_TRAFFIC = (
    {"value": "injured", "label": "我是受害方", "hint": "以伤者或家属视角看赔偿项目"},
    {"value": "driver", "label": "我是驾驶人", "hint": "以驾驶人视角看责任与保险"},
    {"value": "insurer", "label": "我是保险方", "hint": "以保险人视角看赔付顺序"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则"},
)
_STANCES_FAMILY = (
    {"value": "spouse_a", "label": "我是当事人一方", "hint": "以本人视角看财产与子女安排"},
    {"value": "spouse_b", "label": "我是另一方", "hint": "以相对方视角看抗辩与风险"},
    {"value": "heir", "label": "我是继承人", "hint": "以继承人视角看顺序与份额"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则"},
)


@dataclass(frozen=True)
class LawProfile:
    """一个法律内核。**只读**，切换靠换对象，不靠改字段。"""

    id: str
    name: str
    short: str
    desc: str
    domain: str                     # 专属能力 id，空串表示沿用劳动法五块
    # None = 不按法律名过滤（劳动法内核）。接入内核给一个 frozenset。
    corpus_laws: frozenset[str] | None
    # 需要降权的法律；劳动法内核沿用 0.55，接入内核为 1.0（不压制）。
    boost_laws: frozenset[str]
    boost: float
    needs_domain_corpus: bool        # 是否要额外挂载 data/domain_statutes.jsonl
    scope_hint: str                  # 意图分类里的「本内核管什么」
    out_of_scope: str                # 必须判 out_of_scope 的例子
    in_scope: str                    # 看着像别的领域、其实归本内核管的例子
    slot_labels: dict[str, str]
    tools: tuple[str, ...]
    stances: tuple[dict[str, str], ...]
    disclaimer: str
    version_demo: str = ""           # 版本感知的演示点，写进文档与首页
    # ---- 界面层（v121）：每个内核在页面上要「看起来不一样」 ----
    # 只给数据和短句，配色由前端按 id 换一套 --accent（见 index.html）。
    # 为什么不把颜色也放这里：颜色是呈现，不是领域知识；而且暗色模式需要
    # 第二套值，塞进配置会让「一个内核 = 一份法条范围 + 一组工具」变浑。
    seal: str = "法"                 # 索引条与首屏的印章单字，必须一字
    capability: str = ""             # 专属能力的短名，显示在索引条上
    tagline: str = ""                # 首屏一句话：这个内核怎么答你的问题
    facts: tuple[tuple[str, str], ...] = ()       # 首屏三格事实（键, 值）
    examples: tuple[tuple[str, str], ...] = ()    # 首屏示例（标签, 问句）
    # 首屏大字与引导语。v121 之前这两句是写死的「把情况说清楚，我帮你找依据」
    # ——五个内核共用一句话，等于界面上最先看到的那行字证明不了任何差异。
    # 换法要换的不只是主色，还包括「你该说清什么」。
    headline: str = ""
    lede: str = ""
    # ---- v133：clarify 时问什么 ----
    # 为什么需要这个字段：`intent == "clarify"`（「对方出轨」「离婚」这种短句）
    # 原来一律走 `LLM.ask(missing_critical())`，而 `CaseSlots` / `SLOT_PRIORITY`
    # 是劳动法专用的四项（离职方式 / 月工资 / 入职时间 / 地区），接入内核当时
    # 又故意不抽槽位（`_domain_slots` 恒返回 {}）——于是每个内核的「缺什么」
    # 永远是那四项，问什么都会回一句「是公司辞退、自己辞职，还是被迫离职？」。
    # 这一句放在配置里，是因为「本内核该让用户补什么」是领域知识，不是通用文案。
    # 劳动法内核留空 = 沿用原来的缺失槽位追问，保证旧链路逐字不变。
    #
    # v138 起它的位置变了：接入内核有了自己的字段表（见下面的 slot_priority /
    # slot_asks），追问改问**具体缺的那一项**；这一句降为「内核还没有字段表」
    # 或「要素已经问全了」时的兜底。劳动法内核仍然只走原来那条链路。
    clarify_question: str = ""
    # 越界拒答的一句话。留空 = 用 agent.py 里那句劳动法原文（逐字不变）。
    # 为什么也要按内核写：回放模式或模型判越界时，拒答文案原来一律是
    # 「这个系统只处理劳动法相关的问题」——婚家分区里问刑事，也会被告知
    # 本系统只处理劳动合同，等于自己否认了分区。
    refuse_text: str = ""
    # 回放模式的意图粗分用词。
    # 为什么还要一张词表：离线时没有模型读 `scope_hint`，只能靠字符串判断
    # 「这句话是不是本领域的事」。没有它，「买到过期食品」「离婚」这类
    # 六个字的完整问题会被当成信息不足，先去追问一句——追问没错，但用户
    # 明明已经说清了。劳动法内核不用这张表（它有自己的 `_DOMAIN_WORDS`，
    # 逐字保持不变），这里只服务四个接入内核。
    keywords: tuple[str, ...] = ()
    # 「检索不到任何条文」时那句拒答里，让用户补充什么的提示。
    # 留空 = 用 agent.py 里那段劳动法原文（逐字不变）。原来这段话对不同内核也
    # 一律写「是否签了合同、干了多久、怎么离职的、月工资大概多少」——检索一次
    # 没命中，婚家用户又会被问一遍离职方式，和追问那条是同一个病。
    empty_hint: str = ""
    # ---- v138：接入内核的案情槽位 ----
    # `slot_labels` 在 v121–v137 里是**死配置**（零处读取，只当界面文案留着）；
    # v138 起它是本内核的**字段表**：键就是抽取与存储的键，值就是提示词里给
    # 模型看的字段说明。劳动法内核不用它——它有自己那八个字段（CaseSlots），
    # 一个字都不能动。
    #
    # 追问顺序单独一支，不靠字段表的书写顺序：字段表按界面可读性排（婚家把
    # 「结婚时间」写在最前），而真要追问时第一句该问的是「你想办哪件事」。
    # 混成一个次序就只能二选一：要么界面顺序怪，要么开口先问结婚日期。
    slot_priority: tuple[str, ...] = ()
    # 每个字段的追问说法：一句能直接念出来的问法。不复用 `label` 是因为
    # label 是名词——「主要财产」拼进句子变成「您主要财产？」，不像人说话。
    slot_asks: dict[str, str] = field(default_factory=dict)


PROFILES: dict[str, LawProfile] = {
    "labor": LawProfile(
        id="labor",
        name="劳动用工",
        short="劳动法",
        desc="劳动争议、工资报酬、解除终止、社保工伤、经济补偿与仲裁时效",
        domain="",
        corpus_laws=None,
        boost_laws=frozenset({"民法典", "合同法"}),
        boost=0.55,
        needs_domain_corpus=False,
        scope_hint="劳动用工与劳动争议",
        out_of_scope=(
            "民间借贷、欠款不还、婚姻家事继承、房屋买卖租赁、交通事故、"
            "人身损害（被打、被狗咬）、刑事、行政处罚、与劳动无关的商事合同、"
            "消费购物纠纷、食品安全、合同效力与违约金"
        ),
        in_scope=(
            "劳动合同违约金、服务期、竞业限制、培训费分摊、保密义务、"
            "工资/加班费/年终奖/提成、社保公积金、工伤、解除与终止、"
            "经济补偿与赔偿金、劳务派遣、非全日制、试用期、女职工三期、年休假、"
            "法条时效与版本（某条还有没有效、被哪部法替代、旧条号还能不能引）"
        ),
        slot_labels={
            "region": "所在地区", "employment_type": "用工形式",
            "has_contract": "是否签书面合同", "start_date": "入职时间",
            "end_date": "离职时间", "monthly_wage": "月工资",
            "leave_reason": "离职方式", "social_insurance": "社保缴纳情况",
        },
        tools=_GENERIC_TOOLS + _LABOR_ONLY_TOOLS,
        stances=_STANCES_LABOR,
        disclaimer="仅供参考，不构成法律意见，不替代执业律师。",
        version_demo="原《合同法》第 114 条随《民法典》施行废止，现对应第 585 条",
        seal="劳",
        headline="出了什么事，我按现行法条给你找依据",
        lede="说清「什么时候入职、什么用工关系、发生了什么、你想要什么」，答案会落到具体条文上。<br>点下面的例子可以直接试。",
        capability="补偿与时效计算",
        tagline="说清什么时候入职、什么用工关系、发生了什么，我按现行法条给你找依据",
        facts=(
            ("语料", "劳动法系 8 部"),
            ("工具", "9 个 · 含补偿计算与时效"),
            ("演示", "合同法 114 → 民法典 585"),
        ),
        examples=(
            ("欠薪", "公司没签合同还拖欠我工资，我能要什么"),
            ("算赔偿", "我月薪 8000，今年 3 月被辞退，能拿多少赔偿"),
            ("时效", "离职一年半了，还能申请劳动仲裁吗"),
            ("能力边界", "公司食堂的饭很难吃，我能起诉吗"),
        ),
    ),
    "consumer": LawProfile(
        id="consumer",
        name="消费与食品安全",
        short="消保·食安",
        desc="欺诈退一赔三、不符合安全标准食品的十倍赔偿、举证与投诉渠道",
        domain="punitive_damages",
        corpus_laws=frozenset({"消费者权益保护法", "食品安全法"}),
        boost_laws=frozenset(),
        boost=1.0,
        needs_domain_corpus=True,
        scope_hint="消费购物与食品安全",
        out_of_scope=(
            "劳动用工与工资社保、刑事、婚姻家事、交通事故、"
            "与消费无关的公司间商事争议、行政处罚的复议程序"
        ),
        in_scope=(
            "商品或服务欺诈、假货与虚假宣传、七日无理由退货、预付卡退款、"
            "过期/变质/异物食品、标签与说明书瑕疵、价款十倍或损失三倍赔偿、"
            "经营者明知缺陷仍提供、消费者协会投诉与协商和解"
        ),
        slot_labels={
            "goods": "商品或服务", "price": "实际支付金额",
            "is_food": "是否食品", "problem": "发现的问题",
            "has_voucher": "是否留有凭证",
        },
        # 先问「出了什么问题」——它决定走三倍（欺诈）还是十倍（不符合安全
        # 标准），是三个字段里唯一会改变请求权基础的一个；商品与金额只决定
        # 基数大小。顺序反过来会先问一堆背景，用户答完还不知道能主张什么。
        slot_priority=("problem", "goods", "price"),
        slot_asks={
            "goods": "买的是什么、在哪买的",
            "price": "这件东西实际花了多少钱",
            "is_food": "这是不是食品",
            "problem": "商品出了什么问题——过期、变质、有异物，还是和宣传不符",
            "has_voucher": "还留着订单、小票或发票吗",
        },
        tools=_GENERIC_TOOLS,
        stances=_STANCES_CONSUMER,
        disclaimer=(
            "仅供参考，不构成法律意见。赔偿倍数以法条原文与个案认定为准；"
            "本系统不预测结果、不建议是否起诉，也不代填任何金额。"
        ),
        version_demo="《消费者权益保护法》2013 年修正后第 55 条把三倍赔偿的保底定为 500 元",
        seal="食",
        headline="买了什么、多少钱、对方怎么处理，先说这三件",
        lede="说清「买了什么、花了多少、商家怎么说、你想要退还是赔」，答案是「哪一条、几倍、保底多少」。<br>点下面的例子可以直接试。",
        clarify_question=(
            "您买的是什么、在哪买的、出了什么问题？比如生产日期、"
            "有没有吃出异物、商家怎么答复的。"),
        refuse_text=(
            "这个系统在当前分区只处理消费购物与食品安全的问题——买到过期或"
            "不合格商品、退货退款、惩罚性赔偿这些。您问的方向不在本分区范围内。"),
        keywords=("过期", "变质", "退货", "退款", "假货", "食品", "商家",
                  "预付卡", "消费者", "三包", "质量问题", "十倍",
                  # v135：本领域但措辞口语化的说法，回放粗分不该再当「说不清」
                  "外卖", "网购", "假一赔十", "三倍", "直播间", "充值",
                  "办卡", "赠品", "标签", "保质期", "投诉", "美容院"),
        empty_hint=("什么时候在哪买的、商品出了什么问题、您想要退货还是赔偿"),
        capability="惩罚性赔偿核对",
        tagline="先看这东西出了什么问题，再看能退、能赔几倍，最后才是依据哪一条",
        facts=(
            ("语料", "消保法 63 + 食安法 154 条全文"),
            ("工具", "5 个通用工具"),
            ("演示", "退一赔三 / 十倍 / 三倍择一"),
        ),
        examples=(
            ("过期食品", "买到过期面包，能要求十倍赔偿吗"),
            ("预付卡", "健身房跑路了，预付卡里的钱能退吗"),
            ("网购退货", "网购的衣服剪了吊牌，还能七天无理由退吗"),
            ("能力边界", "公司拖欠工资，能按消法要三倍赔偿吗"),
        ),
    ),
    "civil": LawProfile(
        id="civil",
        name="民法典·合同",
        short="民法典",
        desc="合同效力、格式条款、违约金调整、诉讼时效与新旧法衔接",
        domain="contract_check",
        corpus_laws=frozenset({"民法典", "合同法"}),
        boost_laws=frozenset(),
        boost=1.0,
        needs_domain_corpus=False,
        scope_hint="民事法律行为与合同",
        out_of_scope=(
            "劳动用工与工资社保、刑事、婚姻家事继承的具体分割、"
            "消费购物惩罚性赔偿、食品安全、行政处罚"
        ),
        in_scope=(
            "合同成立与效力、格式条款、违约金过高或过低的调整、"
            "定金与损害赔偿、诉讼时效三年、民法典与合同法的条文衔接、"
            "旧条号还能不能引、被哪部法替代"
        ),
        slot_labels={
            "contract_type": "合同类型", "signed_date": "签订时间",
            "breach": "争议情形", "agreed_penalty": "约定违约金",
            "has_written": "是否有书面合同",
        },
        # 先问「卡在哪一条」：合同类型与签订时间只决定引哪一部法、适用哪一版，
        # 而「对方哪一步没做到」直接决定是违约、解除还是条款效力问题。
        slot_priority=("breach", "contract_type", "agreed_penalty"),
        slot_asks={
            "contract_type": "这是什么合同——买卖、租赁、借款，还是服务",
            "signed_date": "合同是什么时候签的",
            "breach": "对方哪一步没做到，或者你们现在卡在哪一条上",
            "agreed_penalty": "合同里约定的违约金是多少",
            "has_written": "有没有书面合同",
        },
        tools=_GENERIC_TOOLS,
        stances=_STANCES_CIVIL,
        disclaimer=(
            "仅供参考，不构成法律意见。合同效力须结合完整条款与案情判断；"
            "本系统不预测裁判结果、不建议是否起诉。"
        ),
        version_demo="原《合同法》第 114 条随《民法典》2021-01-01 施行废止，现对应第 585 条",
        seal="民",
        headline="签了什么、怎么约定、争议卡在哪一条",
        lede="说清「合同怎么约定、履行到哪一步、对方哪里没做到」，答案会告诉你条款还有没有效、该引第几条。<br>点下面的例子可以直接试。",
        clarify_question=(
            "这件事是怎么约定的、对方哪里没做到、您想主张什么？"
            "比如有没有书面合同、约定的期限和价款、已经付了多少。"),
        refuse_text=(
            "这个系统在当前分区只处理民事合同与民事法律行为的问题——合同效力、"
            "违约与解除、定金与赔偿这些。您问的方向不在本分区范围内。"),
        keywords=("合同", "违约", "定金", "押金", "借款", "欠款", "效力",
                  "条款", "履行", "解除", "签字", "盖章",
                  # v135：本领域但措辞口语化的说法
                  "借条", "欠条", "房东", "租房", "租金", "保证人",
                  "转账", "收条", "违约金", "中介", "二手房", "赠与"),
        empty_hint=("有没有书面合同、怎么约定的、对方哪一步没做到"),
        capability="新旧条号衔接",
        tagline="合同有没有效、违约金能不能调，先查版本再查条文——旧条号可能已经不能引了",
        facts=(
            ("语料", "民法典 1260 + 合同法 428 条"),
            ("工具", "5 个通用工具"),
            ("演示", "114 条已废止 → 585 条"),
        ),
        examples=(
            ("废止条文", "合同法第 114 条关于违约金过高的规定，这条还有效吗"),
            ("违约金", "约定违约金是合同金额的 30%，能被调低吗"),
            ("合同效力", "合同只签字没盖章，算成立吗"),
            ("能力边界", "离婚时房子怎么分，按合同法算吗"),
        ),
    ),
    "traffic": LawProfile(
        id="traffic",
        name="交通事故与人身损害",
        short="道交·人损",
        desc="责任划分、交强险赔付顺序、赔偿项目逐项核对、伤残与时效",
        domain="traffic_compensation",
        # 民法典侵权责任编（1208-1217 机动车交通事故责任 + 1179 赔偿项目）
        # 已经全量在库，只需补《道路交通安全法》的责任划分与现场处置条款。
        corpus_laws=frozenset({"民法典", "道路交通安全法"}),
        boost_laws=frozenset(),
        boost=1.0,
        needs_domain_corpus=True,
        scope_hint="道路交通事故与人身损害赔偿",
        out_of_scope=(
            "劳动用工与工伤认定、消费购物与食品安全、婚姻家事与继承、"
            "刑事定罪与量刑、行政处罚与复议程序、与事故无关的商事合同"
        ),
        in_scope=(
            "事故现场处置与报警、交通事故认定书与责任划分、交强险与商业险赔付顺序、"
            "医疗费/误工费/护理费/营养费/住院伙食补助费/交通费、残疾赔偿金与死亡赔偿金、"
            "被扶养人生活费、精神损害抚慰金、车辆与财产损失、赔偿权利人范围、诉讼时效"
        ),
        slot_labels={
            "accident_date": "事故时间", "liability": "责任认定",
            "injury": "伤情与伤残", "insurance": "保险情况",
            "expenses": "已发生费用", "region": "事故发生地",
        },
        # 责任划分决定赔不赔、赔多少，伤情决定项目与年限；时间排在第三
        # 但不等于不重要——它管的是时效，缺了就算不出截止日。
        slot_priority=("liability", "injury", "accident_date"),
        slot_asks={
            "accident_date": "事故是哪一天、在哪个城市发生的",
            "liability": "交警的事故认定书上责任是怎么划分的",
            "injury": "伤到哪里、有没有做伤残鉴定",
            "insurance": "对方车上除了交强险，还有商业三者险吗",
            "expenses": "到现在已经花了多少钱，主要是医疗费",
            "region": "事故发生在哪个城市",
        },
        tools=_GENERIC_TOOLS,
        stances=_STANCES_TRAFFIC,
        disclaimer=(
            "仅供参考，不构成法律意见。责任比例、伤残等级与赔偿数额须由公安机关、"
            "鉴定机构和裁判机关认定；本系统只给法条上的计算式与核对清单，"
            "不预测赔偿结果，也不代为填写具体金额。"
        ),
        version_demo=(
            "原《侵权责任法》第 16 条的人身损害赔偿项目，现见《民法典》第 1179 条；"
            "该法已随《民法典》2021-01-01 施行废止"
        ),
        seal="道",
        headline="谁的责任、伤到哪、花了多少，一项一项核对",
        lede="说清「事故怎么发生、责任怎么定、伤情与花费」，答案按赔偿项目逐项列，金额留空由你按票据填。<br>点下面的例子可以直接试。",
        clarify_question=(
            "事故是哪天、在哪发生的，有没有交警的事故认定书，"
            "伤情和责任是怎么认定的？"),
        refuse_text=(
            "这个系统在当前分区只处理道路交通事故与人身损害赔偿的问题——"
            "责任认定、医疗费与误工费、保险理赔这些。您问的方向不在本分区范围内。"),
        keywords=("事故", "撞", "交警", "认定书", "误工", "伤残", "保险",
                  "交强险", "肇事", "伤情", "医疗费", "责任认定",
                  # v135：本领域但措辞口语化的说法
                  "电动车", "行人", "私了", "定损", "理赔", "剐蹭",
                  "追尾", "酒驾", "护栏", "修车", "打车", "代位"),
        empty_hint=("事故的时间地点、有没有事故认定书、伤情和花费"),
        capability="赔偿项目核对",
        tagline="先定责任比例，再排交强险与商业险的赔付顺序，最后逐项核对赔偿项目",
        facts=(
            ("语料", "侵权编全量 + 道交法 4 条摘录"),
            ("工具", "5 个通用工具"),
            ("演示", "交强险分项限额与赔付顺序"),
        ),
        examples=(
            ("被撞", "我被车撞了对方全责，误工费和护理费怎么算"),
            ("交强险", "交强险能赔多少，不够的部分由谁承担"),
            ("伤残", "鉴定为十级伤残，残疾赔偿金按什么标准算"),
            ("能力边界", "上班路上被车撞，算工伤还是交通事故"),
        ),
    ),
    "family": LawProfile(
        id="family",
        name="婚姻家庭与继承",
        short="婚家·继承",
        desc="离婚方式与冷静期、共同财产与债务、抚养权与抚养费、法定继承与遗嘱",
        domain="family_split",
        # 民法典婚姻家庭编（1040-1118）与继承编（1119-1163）本来就全量在
        # 主索引里，这个内核**不需要任何新语料**——「换内核」真的只是换配置。
        corpus_laws=frozenset({"民法典"}),
        boost_laws=frozenset(),
        boost=1.0,
        needs_domain_corpus=False,
        scope_hint="婚姻家庭与继承",
        out_of_scope=(
            "劳动用工与工资社保、消费购物、交通事故赔偿、与家事无关的商事合同、"
            "行政处罚、刑事"
        ),
        in_scope=(
            "协议离婚与三十日冷静期、诉讼离婚的法定情形、夫妻共同财产与个人财产、"
            "婚前财产与房产加名、夫妻共同债务、彩礼返还、子女抚养权与抚养费、探望权、"
            "法定继承顺序与份额、遗嘱形式与效力、遗产债务清偿、旧《婚姻法》《继承法》条号衔接"
        ),
        slot_labels={
            "marriage_date": "结婚时间", "children": "子女情况",
            "property": "主要财产", "debt": "主要债务",
            "dispute": "争议焦点", "has_will": "是否有遗嘱",
        },
        # 婚家最该先问的是「你想解决哪一块」，不是结婚日期：离婚、财产、
        # 抚养、继承四件事适用的完全是不同条文，问错方向等于换了一个案子。
        slot_priority=("dispute", "property", "children"),
        slot_asks={
            "marriage_date": "什么时候结婚的",
            "children": "有没有孩子、孩子多大",
            "property": "主要财产有哪些——房子、存款，还是公司股权",
            "debt": "有没有共同的债务",
            "dispute": "您这次想解决的是哪一件事——能不能离、财产怎么分、"
                      "孩子归谁，还是损害赔偿",
            "has_will": "有没有遗嘱",
        },
        tools=_GENERIC_TOOLS,
        stances=_STANCES_FAMILY,
        disclaimer=(
            "仅供参考，不构成法律意见。财产范围、抚养权归属与继承份额须结合完整事实"
            "与证据认定；本系统不预测裁判结果、不代为分割财产，也不给任何比例建议。"
        ),
        version_demo=(
            "原《婚姻法》《继承法》已随《民法典》2021-01-01 施行废止，"
            "条文并入婚姻家庭编与继承编，旧条号不能再用"
        ),
        seal="家",
        headline="什么身份关系、争的是财产还是孩子",
        lede="说清「什么关系、什么时候、争的是什么、有没有书面材料」，答案列法定要件与条号，不替你决定离不离。<br>点下面的例子可以直接试。",
        clarify_question=(
            "您想问的是哪一块：能不能离、财产怎么分、孩子归谁，"
            "还是能不能要赔偿？"),
        refuse_text=(
            "这个系统在当前分区只处理婚姻家庭与继承的问题——离婚、财产分割、"
            "子女抚养、遗嘱与继承这些。您问的方向不在本分区范围内。"),
        keywords=("离婚", "婚姻", "抚养", "继承", "遗嘱", "彩礼", "出轨",
                  "财产分割", "监护", "收养", "结婚", "夫妻",
                  # v135：用户截图里被静态追问挡回去的偏门说法。
                  # 「夫妻共同债务」在 in_scope 里，民法典 1064/1089 也在索引里，
                  # 回放粗分却因为词表没有「债务」而判成「说不清」。
                  "债务", "共同债务", "宠物", "狗", "猫", "冷静期",
                  "抚养费", "探望权", "加名", "遗产", "分割", "再婚",
                  "家暴", "房产", "存款", "公积金"),
        empty_hint=("什么身份关系、什么时候发生的事、争的是财产、孩子还是继承"),
        capability="身份要件核对",
        tagline="身份关系先定性，再看财产怎么分、债务谁承担、继承顺序怎么排",
        facts=(
            ("语料", "民法典婚姻家庭编 + 继承编"),
            ("工具", "5 个通用工具"),
            ("演示", "婚姻法 / 继承法旧条号已失效"),
        ),
        examples=(
            ("离婚财产", "婚后买的房只写了对方名字，离婚能分吗"),
            ("抚养权", "孩子三岁，抚养权一般判给谁"),
            ("继承", "父亲没留遗嘱，房子由谁继承、各占多少"),
            ("能力边界", "老板不发工资，能按婚姻法要吗"),
        ),
    ),
}

_current: contextvars.ContextVar[LawProfile | None] = contextvars.ContextVar(
    "law_profile", default=None)


def resolve(raw: Any) -> LawProfile:
    """白名单解析。空值、拼错、模型瞎编的一律回落劳动法——不猜内核。

    传入的已经是 LawProfile 对象时原样返回（`system()` 等内部调用会这么传）；
    `None` 在这里表示「没有显式选择」，回落劳动法而不是读上下文——
    读上下文是 `get()` 的职责，两个语义不能混，否则一个空参数就能
    把当前会话的内核悄悄改掉。
    """
    if isinstance(raw, LawProfile):
        return raw
    key = str(raw or "").strip().lower()
    return PROFILES.get(key) or PROFILES[DEFAULT_ID]


def get() -> LawProfile:
    """当前内核。没显式切过就是劳动法，所以所有老代码/老测试行为不变。"""
    return _current.get() or PROFILES[DEFAULT_ID]


def is_labor() -> bool:
    return get().id == DEFAULT_ID


@contextmanager
def use(raw: Any) -> Iterator[LawProfile]:
    """在一段代码里切换内核。

    chat 的 Agent 跑在独立 worker 线程里，ContextVar **不会**从请求线程
    继承过去，所以 server 必须在 `work()` 里显式 `with use(pid):` 包一次。
    这正是「显式传参」比「指望上下文自动传播」可靠的地方。
    """
    p = resolve(raw)
    token = _current.set(p)
    try:
        yield p
    finally:
        _current.reset(token)


def all_meta() -> list[dict[str, Any]]:
    """给 /api/meta 和前端选择器用。不暴露任何内部路径或配置值。"""
    return [{"id": p.id, "name": p.name, "short": p.short,
             "desc": p.desc, "domain": p.domain,
             "laws": sorted(p.corpus_laws) if p.corpus_laws else [],
             "all_laws": p.corpus_laws is None,
             "tool_count": len(p.tools),
             "version_demo": p.version_demo,
             # v121 界面层：上面那条索引、首屏那张卡、示例问题全靠这四项。
             # 前端一个领域名都不写死——加内核只改这里。
             "seal": p.seal,
             "capability": p.capability,
             "tagline": p.tagline,
             "facts": [{"k": k, "v": v} for k, v in p.facts],
             "examples": [{"tag": t, "text": x} for t, x in p.examples],
             # v138：接入内核的案情要素字段表。前端「案情档案」页要靠它把
             # 抽到的要素（争议焦点/主要财产/商品问题…）画成可编辑的表单，
             # 而不是永远显示劳动法那八个字段。劳动法内核这里也有一份，但
             # 前端对它走自己写死的 SLOT_DEFS（那套带 select/数字校验/占位
             # 符），两处不该各自维护一份——所以 labor 这份仅作配置留档，
             # 前端按 pid 分流、不读它。
             "slot_labels": p.slot_labels or {},
             # v121：首屏大字与引导语。空串时回落到原来那句，老内核不受影响。
             "headline": p.headline or "把情况说清楚，我帮你找依据",
             "lede": p.lede}
            for p in PROFILES.values()]


def ids() -> tuple[str, ...]:
    return tuple(PROFILES)
