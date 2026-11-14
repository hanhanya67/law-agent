# -*- coding: utf-8 -*-
"""办案包（extras）：答案之外的结构化产物。

v116 是三块（时间轴 / 请求事项候选 / 举证体检），v117 加了两块：
判例参考与申诉决策卡、显式立场与表达偏好。仍然是同一条原则——
不需要模型决定「要不要做」的确定性投影，就不进工具循环。

为什么单独一层、而不是再加几个 Agent 工具：
  tools.py 开头那条原则——工具超过 10 个，模型选错率显著上升。现有 9 个
  已经贴着上限。而这几块东西是既有结论（定性、槽位、算出的钱和期限）
  的确定性投影，问完就该有，没有「该不该调」这个决策。放进决策循环，
  只是给模型多几个可以选错的机会。

  代价是它们不会进逐字答案。这是有意的：请求事项一旦由模型写进正文，
  就变成「系统替当事人主张」；摆成可勾选的候选，责任留在本人手里。

三条硬约束（test_extras.py 有断言钉着）：
  1. 零 LLM / 零数据库 / 零网络。回放模式、MCP 子进程、离线单测跑的是同一份代码。
  2. 所有条号必须能在 data/statutes.jsonl 查到。测试逐条核验，不许凭印象补。
     宁可少给一条，也不能给一条用户核不到的依据。
  3. 金额永远留空。docgen 生成的仲裁申请书里「请求事项 / 金额 / 条号」三处
     留空是既有硬断言（test_docgen.py 46 项）。这里的「请求事项」是**建议卡**，
     不是往文书里填字——两者不能混，混了就是把法律风险偷偷转嫁给用户。
"""
from __future__ import annotations

from typing import Any

from . import cases as _cases
from . import persona as _persona

LAW_ZC = "中华人民共和国劳动争议调解仲裁法"
LAW_LC = "中华人民共和国劳动合同法"
LAW_L = "中华人民共和国劳动法"

BLANK = "＿＿＿＿"          # 全角下划线，和 docgen 模板里的留空写法一致
DISCLAIMER = "仅供参考，不构成法律意见。"

# 本模块会输出的全部条号。test_extras 会拿它去语料库逐条查，
# 查不到就是回归红——写在这里而不是散在函数里，是为了让「改了条号」
# 这件事必然被那道检查看见。
CITES: tuple[str, ...] = (
    f"《{LAW_ZC}》第六条",
    f"《{LAW_ZC}》第二十七条",
    f"《{LAW_ZC}》第二十九条",
    f"《{LAW_ZC}》第三十九条",
    f"《{LAW_ZC}》第四十三条",
    f"《{LAW_ZC}》第四十八条",
    f"《{LAW_ZC}》第五十条",
    f"《{LAW_ZC}》第五十一条",
    f"《{LAW_LC}》第七条",
    f"《{LAW_LC}》第二十条",
    f"《{LAW_LC}》第四十六条",
    f"《{LAW_LC}》第四十七条",
    f"《{LAW_LC}》第八十二条",
    f"《{LAW_LC}》第八十五条",
    f"《{LAW_LC}》第八十七条",
    f"《{LAW_L}》第五十条",
    f"《{LAW_L}》第七十二条",
)


# ---------------------------------------------------------------- 程序时间轴

# (id, 阶段, 相对时点, 说明, 依据)
# 「相对时点」而不是编一个绝对日期：只有起算日确定，日期才确定。没有起算日
# 时把 T+45 日硬算成某个具体的日子，用户会照着去办事——那是在造法定期限。
_TIMELINE_NODES: tuple[tuple[str, str, str, str, str], ...] = (
    ("onset", "时效起算", "T",
     "从知道或者应当知道权利被侵害之日起算。把这个日子记准，后面所有期限都从它推。",
     f"《{LAW_ZC}》第二十七条"),
    ("fix", "固定证据", "T 起尽早",
     "先把合同、工资流水、考勤、解除通知、聊天记录留好。证据散了再往后走，"
     "每一步都会更难。",
     f"《{LAW_ZC}》第六条"),
    ("apply", "递交仲裁申请", "时效届满前",
     "一般劳动争议的仲裁时效为一年。申请书写清双方信息、请求事项、事实与理由。",
     f"《{LAW_ZC}》第二十七条"),
    ("accept", "受理与否", "申请后 5 日内",
     "仲裁委收到申请后五日内决定受理并通知；不予受理或逾期不决定的，"
     "可以就同一事项向法院起诉。",
     f"《{LAW_ZC}》第二十九条"),
    ("award", "作出裁决", "受理后 45 日内",
     "案情复杂经主任批准可书面通知延期，但延长不得超过十五日；"
     "逾期未裁决的，可以就该事项向法院起诉。",
     f"《{LAW_ZC}》第四十三条"),
    ("sue", "不服可起诉", "收到裁决书后 15 日内",
     "劳动者对终局裁决不服、或任何一方对其他裁决不服，都可在这十五日内"
     "向人民法院起诉。",
     f"《{LAW_ZC}》第四十八条"),
    ("effective", "裁决生效", "起诉期满未起诉",
     "期满不起诉，裁决书发生法律效力。",
     f"《{LAW_ZC}》第五十条"),
    ("execute", "申请执行", "生效后对方不履行时",
     "一方逾期不履行生效调解书、裁决书的，另一方可依民事诉讼法有关规定"
     "向法院申请执行。",
     f"《{LAW_ZC}》第五十一条"),
)


def _deadline_anchor(deadline_data: dict | None) -> tuple[str | None, str | None,
                                                          str | None, bool]:
    """从期限工具的返回里取 (起算日, 截止日, 说明, 是否在职讨薪)。

    不自己解析日期字符串、也不自己加一年：算日期这件事 deadline.compute()
    已经做过并且有 43 项回归。这里只负责**搬运**，多算一次就多一处能错的地方。
    """
    if not isinstance(deadline_data, dict) or not deadline_data.get("ok"):
        return None, None, None, False
    # 在职讨薪：法律上不存在那个截止日，绝不能画出来（v108 的老教训）。
    if deadline_data.get("unlimited") is True:
        return None, None, "劳动关系存续期间追索报酬不受一年时效限制，本阶段没有截止日", True
    c = deadline_data.get("conservative")
    if not isinstance(c, dict):
        return None, None, None, False
    return (c.get("onset") or None, c.get("deadline") or None,
            c.get("onset_label") or None, False)


def build_timeline(slots: dict | None = None,
                   deadline_data: dict | None = None,
                   question: str = "") -> dict[str, Any]:
    """把法定程序摊成一条可核对的时间轴。

    available 恒为 True：程序阶段本身与个案日期无关，没起算日也该给。
    缺日期时只是不给绝对日期，而不是整块藏起来——藏起来用户就不知道
    「受理后 5 日」这种节点存在，等到过了才听说。
    """
    slots = slots or {}
    onset, due, onset_label, unlimited = _deadline_anchor(deadline_data)

    # 只在工具真的算过的情况下用工具结果。槽位里的 end_date 是用户随口说的，
    # 没有经过期间规则核算，拿它当法定起算日会把「大概那天」包装成「依法这天」。
    anchor_note = ""
    if onset:
        anchor_note = f"起算日：{onset}" + (f"（{onset_label}）" if onset_label else "")
    elif unlimited:
        anchor_note = "在职讨薪阶段没有一年截止日"
    elif slots.get("end_date"):
        anchor_note = "你填了离职日期，本轮没做期限核算，时间轴只列法定阶段"
    else:
        anchor_note = "还没有可核算的起算日，时间轴只列法定阶段"

    nodes: list[dict[str, Any]] = []
    for nid, title, when, detail, cite in _TIMELINE_NODES:
        row: dict[str, Any] = {
            "id": nid, "title": title, "when": when,
            "detail": detail, "cite": cite, "date": None,
        }
        if nid == "onset" and onset:
            row["date"] = onset
        if nid == "apply":
            if unlimited:
                row["when"] = "在职期间不受一年限制"
                row["detail"] = ("劳动关系存续期间因拖欠劳动报酬申请仲裁，不受一年"
                                 "时效限制；但劳动关系终止后，须自终止之日起一年内提出。")
            elif due:
                row["date"] = due
                row["detail"] += f" 本轮核算的截止日：{due}。"
        nodes.append(row)

    return {
        "available": True,
        "anchor": onset,
        "anchor_note": anchor_note,
        "deadline": due,
        "unlimited": unlimited,
        "nodes": nodes,
        "note": ("日期按期间规则核算；其余节点是法定相对期间，"
                 "具体日子以你收到的文书为准。"),
    }


# ---------------------------------------------------------------- 请求事项候选

# 触发词按「越长越优先」排，不按表顺序：v110 那条教训是宽泛的两字词
# （「没签」）会压过五字词（「无固定期限」），短词先命中就把整题带偏。
# 这里在运行时统一按命中词长度排序，规则表本身只管写全。
_CLAIM_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "double_wage", "keys": ("没签劳动合同", "未签劳动合同", "没签书面合同",
                                      "未订书面合同", "没签合同", "未签合同", "不签合同",
                                      "没签", "未签", "二倍工资", "双倍工资"),
        "text": f"请求裁决被申请人支付未订立书面劳动合同的二倍工资差额 {BLANK} 元",
        "basis": [f"《{LAW_LC}》第八十二条"],
        "needs": ["start_date", "monthly_wage"],
        "hint": "自用工满一个月的次日起算，最长十一个月；具体月份数按实际用工期间核。",
    },
    {
        "id": "illegal_termination",
        "keys": ("违法解除", "违法辞退", "无故辞退", "突然辞退", "没有理由辞退",
                 "无故开除", "非法解除", "赔偿金", "二倍赔偿", "2N"),
        "text": f"请求裁决被申请人支付违法解除劳动合同的赔偿金 {BLANK} 元",
        "basis": [f"《{LAW_LC}》第八十七条", f"《{LAW_LC}》第四十七条"],
        "needs": ["start_date", "end_date", "monthly_wage"],
        "hint": "赔偿金按经济补偿标准的二倍计算；年限与三倍封顶按第四十七条口径。",
    },
    {
        "id": "severance", "keys": ("经济补偿", "经济补偿金", "N+1", "n+1", "补偿金",
                                   "裁员", "协商解除", "合同到期不续"),
        "text": f"请求裁决被申请人支付解除劳动合同的经济补偿 {BLANK} 元",
        "basis": [f"《{LAW_LC}》第四十六条", f"《{LAW_LC}》第四十七条"],
        "needs": ["start_date", "end_date", "monthly_wage"],
        "hint": "每满一年支付一个月工资；六个月以上不满一年按一年计，不满六个月按半个月计。",
    },
    {
        "id": "wage_arrears", "keys": ("拖欠工资", "欠薪", "克扣工资", "不发工资",
                                      "没发工资", "未发工资", "工资没给", "讨薪", "要回工资"),
        "text": f"请求裁决被申请人支付拖欠的劳动报酬 {BLANK} 元",
        "basis": [f"《{LAW_L}》第五十条", f"《{LAW_LC}》第八十五条"],
        "needs": ["monthly_wage"],
        "hint": "写清欠的是哪几个月、各多少；有银行流水的按流水逐月对。",
    },
    {
        "id": "add_compensation", "keys": ("加付赔偿金", "责令限期支付", "逾期不支付",
                                           "50%", "100%", "多要一半"),
        "text": f"请求裁决被申请人加付未及时足额支付劳动报酬的赔偿金 {BLANK} 元",
        "basis": [f"《{LAW_LC}》第八十五条"],
        "needs": [],
        "caution": "这一项以劳动行政部门已责令限期支付、单位逾期仍不支付为前提，"
                   "不是自动能主张；先把责令文书或投诉回执准备好。",
        "hint": "加付标准为应付金额的百分之五十以上百分之一百以下。",
    },
    {
        "id": "probation_wage", "keys": ("试用期不发工资", "试用期工资", "试用期不给",
                                        "试用期白干", "试用期低于最低工资"),
        "text": f"请求裁决被申请人补足试用期工资差额 {BLANK} 元",
        "basis": [f"《{LAW_LC}》第二十条"],
        "needs": ["monthly_wage"],
        "hint": "不得低于本单位相同岗位最低档工资或约定工资的百分之八十，"
                "且不得低于当地最低工资标准。",
    },
    {
        "id": "social_insurance", "keys": ("没交社保", "未缴社保", "不交社保", "补缴社保",
                                           "社保没给", "没买社保"),
        "text": "请求裁决被申请人依法补缴劳动关系存续期间的社会保险",
        "basis": [f"《{LAW_L}》第七十二条"],
        "needs": ["start_date", "end_date"],
        "caution": "社会保险费的征缴在很多地区由社保经办机构处理、不属仲裁受理范围，"
                   "先向 12333 或当地社保部门确认该走哪条路。",
        "hint": "本项金额不由本人填，按缴费基数与当地经办口径核定。",
    },
    {
        "id": "confirm_relation", "keys": ("确认劳动关系", "不承认劳动关系", "否认用工",
                                           "没合同算不算员工", "事实劳动关系"),
        "text": f"请求确认申请人与被申请人自 {BLANK} 起存在劳动关系",
        "basis": [f"《{LAW_LC}》第七条", f"《{LAW_ZC}》第六条"],
        "needs": ["start_date"],
        "hint": "确认劳动关系常常是主张二倍工资、经济补偿的前置请求，"
                "要不要一并列上请本人决定。",
    },
)

_MAX_CLAIMS = 4            # 一次最多摆四条，再多就没人逐条看了


def _longest_hit(rule: dict[str, Any], text: str) -> str:
    """该规则命中的最长触发词；没命中返回空串。"""
    return max((k for k in rule["keys"] if k in text), key=len, default="")


def _pick_rules(text: str) -> list[dict[str, Any]]:
    """命中规则，按词长降序；被更长词包含的短命中一律压掉。

    v110「宽泛规则永远赢」那条教训在这里同样成立：「加付赔偿金」里含着
    「赔偿金」，不压就会连「违法解除赔偿金」一起摆出来；「试用期不发工资」
    含着「不发工资」，不压就会多一条欠薪请求。候选宁可少一条让人自己补，
    也不能摆一条对不上的让人误勾。
    """
    hits: list[tuple[str, int, dict[str, Any]]] = []
    for i, rule in enumerate(_CLAIM_RULES):
        word = _longest_hit(rule, text)
        if word:
            hits.append((word, -i, rule))
    keep: list[tuple[int, int, dict[str, Any]]] = []
    for word, order, rule in hits:
        shadowed = any(other is not rule and word in other and len(other) > len(word)
                       for other, _, _ in hits)
        if not shadowed:
            keep.append((len(word), order, rule))
    # 同长度时按表内顺序，结果可复现（order 存的是 -i）。
    keep.sort(key=lambda x: (-x[0], -x[1]))
    return [r for _, _, r in keep]


def build_claims(question: str = "", verdict: dict | None = None,
                 slots: dict | None = None,
                 calc_data: dict | None = None) -> dict[str, Any]:
    """按案情给出请求事项**候选**，一条都不替用户勾选。

    verdict 只用来扩词面（用户问「能要回多少」时问题里可能一个法条术语都没有，
    但定性结果里写了「二倍工资」）。不拿它判断该不该给：定性说 unknown 时
    照样可以按问题里的关键词摆候选，用户自己会判断哪条对得上。
    """
    verdict = verdict or {}
    slots = slots or {}
    text = " ".join(str(verdict.get(k) or "") for k in ("label", "legal", "reason"))
    text = f"{question} {text}"

    items: list[dict[str, Any]] = []
    for rule in _pick_rules(text)[:_MAX_CLAIMS]:
        missing = [k for k in rule["needs"] if slots.get(k) in (None, "")]
        items.append({
            "id": rule["id"],
            "text": rule["text"],
            "basis": list(rule["basis"]),
            "hint": rule["hint"],
            "caution": rule.get("caution", ""),
            "missing": missing,
            # 永远 false。默认全勾的界面等于替用户做了主张，
            # 而「取消勾选」需要他先怀疑——他往往不知道该怀疑哪一条。
            "checked": False,
            "amount_blank": BLANK,
        })

    # 算出来的钱单独摆，不塞进任何一条请求事项的 text。
    # 硬塞进去就等于把金额写进了请求事项，docgen 那条留空约定会从侧门被破掉。
    estimates: list[dict[str, Any]] = []
    if isinstance(calc_data, dict):
        for s in calc_data.get("scenarios") or []:
            if not isinstance(s, dict):
                continue
            estimates.append({"label": s.get("label") or "",
                              "amount": s.get("amount"),
                              "months": s.get("months"),
                              "formula": s.get("formula") or ""})

    return {
        "available": bool(items),
        "items": items,
        "estimates": estimates,
        "note": ("以下是可勾选的候选，不是替你写好的申请书。"
                 "金额一律留空——漏写请求事项仲裁委不会主动补，"
                 "写错金额要当庭变更，这两处必须本人确认后填写。"
                 + ("右侧核算结果只作参考，不自动填入。" if estimates else "")),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------- 举证体检

_EVIDENCE_BASE: tuple[dict[str, Any], ...] = (
    {
        "id": "relation", "item": "劳动关系确实存在", "who": "你",
        "materials": "劳动合同、工资流水、工牌/门禁记录、考勤表、社保缴费记录、招用登记",
        "slot": "has_contract",
        "why": "所有请求都从「双方存在劳动关系」出发，这一条立不住，后面都谈不上。",
        "cite": f"《{LAW_ZC}》第六条",
    },
    {
        "id": "wage", "item": "工资标准与实发金额", "who": "你",
        "materials": "工资条、银行代发流水、个税记录、劳动合同里的工资条款",
        "slot": "monthly_wage",
        "why": "补偿、赔偿、二倍工资都按它折算，说不清基数就算不出数。",
        "cite": f"《{LAW_LC}》第四十七条",
    },
    {
        "id": "period", "item": "入职与离职时间", "who": "你",
        "materials": "入职登记表、offer、离职证明、解除/终止通知、最后一次考勤",
        "slot": "start_date",
        "why": "工作年限决定补偿月数，起止日也决定时效从哪天算。",
        "cite": f"《{LAW_ZC}》第二十七条",
    },
    {
        "id": "leave", "item": "解除或离职的经过", "who": "你",
        "materials": "书面辞退通知、微信/邮件记录、录音、交接单、离职审批",
        "slot": "leave_reason",
        "why": "是被辞退、协商解除还是自己辞职，决定能不能要补偿或赔偿。",
        "cite": f"《{LAW_LC}》第八十七条",
    },
    {
        "id": "interrupt", "item": "时效中断或中止的痕迹", "who": "你",
        "materials": "催讨工资的聊天记录、投诉回执、单位答应支付的书面回复",
        "slot": "",
        "why": "对方答应履行、或你向有关部门请求救济，会让一年时效重新起算。",
        "cite": f"《{LAW_ZC}》第二十七条",
    },
    {
        "id": "employer", "item": "由单位掌握的材料", "who": "单位",
        "materials": "考勤原始记录、工资台账、规章制度与公示记录、劳动合同文本",
        "slot": "",
        "why": "这类证据在单位手里，你不掌握也可以申请仲裁庭责令其提供；"
               "单位拒不提供要承担不利后果。",
        "cite": f"《{LAW_ZC}》第三十九条",
    },
)

# 命中某类请求时追加的专属材料。只加材料，不改 status：
# 「需要额外准备什么」和「已知要素齐不齐」是两件事，混在一起就分不清
# 到底是没说案情，还是说了但没证据。
_CLAIM_MATERIALS: dict[str, tuple[str, str]] = {
    "double_wage": ("用工满一个月仍在岗的证据", "首月考勤、入职登记、持续工作的聊天记录"),
    "illegal_termination": ("解除行为本身", "书面解除通知或能证明被要求离岗的录音/聊天记录"),
    "severance": ("解除原因与工作年限", "解除协议、离职证明、历年合同或社保缴费年限"),
    "wage_arrears": ("欠薪明细", "逐月应发与实发对照、催讨记录、银行流水"),
    "add_compensation": ("行政前置的凭证", "劳动行政部门责令限期支付文书、投诉受理回执"),
    "probation_wage": ("试用期约定与当地最低工资", "劳动合同试用期条款、当地最低工资标准文件"),
    "social_insurance": ("应缴未缴的期间", "社保缴费查询记录、工资条里的代扣项"),
    "confirm_relation": ("实际用工的事实", "工作安排记录、工牌、同事证言、报销与转账记录"),
}


def build_evidence_check(slots: dict | None = None,
                         claims: dict | None = None,
                         question: str = "") -> dict[str, Any]:
    """「谁举证、要哪些材料、还差什么」的体检表。

    status 的语义是**要素**齐不齐，不是证据齐不齐：槽位里有值只说明
    用户告诉过系统，不等于他手上留着材料。所以哪怕全 have，末尾那句
    「材料仍要自己留好」也不能省——省了就会有人以为不用带证据去开庭。
    """
    slots = slots or {}
    items: list[dict[str, Any]] = []
    for row in _EVIDENCE_BASE:
        key = row["slot"]
        if key:
            known = slots.get(key) not in (None, "")
            status = "have" if known else "todo"
        else:
            # 单位举证 / 时效中断这两条与槽位无关，恒为待办，
            # 写 have 会让人以为系统替他确认过。
            status = "todo"
        items.append({
            "id": row["id"], "item": row["item"], "who": row["who"],
            "materials": row["materials"], "why": row["why"],
            "cite": row["cite"], "status": status,
            "extra": [],
        })

    by_id = {it["id"]: it for it in items}
    for claim in (claims or {}).get("items") or []:
        add = _CLAIM_MATERIALS.get(claim.get("id") or "")
        if not add:
            continue
        # 挂到最相关的那一条上，不新开一整块：清单太长就没人逐条看。
        target = by_id.get({
            "double_wage": "relation", "illegal_termination": "leave",
            "severance": "period", "wage_arrears": "wage",
            "add_compensation": "wage", "probation_wage": "wage",
            "social_insurance": "period", "confirm_relation": "relation",
        }.get(claim["id"], "relation"))
        if target is not None:
            target["extra"].append(f"{add[0]}：{add[1]}")

    have = sum(1 for it in items if it["status"] == "have")
    return {
        "available": True,
        "items": items,
        "summary": {"have": have, "todo": len(items) - have},
        "note": ("「已记下」表示你在咨询里说过，不等于手上有证据；"
                 "原件自己留好，以仲裁委要求为准。"),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------- 统一入口

def _step_data(state: Any, tool_name: str) -> dict | None:
    """从执行轨迹里取某个工具最近一次成功返回的结构化数据。

    取不到就是 None，不抛异常：拒答、寒暄、回放里都可能没有这一步，
    extras 必须在这几种情况下照样能出结果，只是少几个日期和数字。
    """
    steps = getattr(state, "scratchpad", None) or []
    for s in reversed(steps):
        if getattr(s, "tool", "") == tool_name and not getattr(s, "error", None):
            data = getattr(s, "data", None)
            if isinstance(data, dict):
                return data
    return None


def _new_blocks(question: str, slots: dict, claims: dict,
                calc_data: dict | None, stance: str | None,
                persona_cfg: dict | None,
                with_external: bool = False) -> dict[str, Any]:
    """v117 的两块（判例参考 / 画像）。各自兜底，不连累前三块。

    判例库万一没随包发出去，时间轴、请求事项、举证体检必须照常显示；
    「办案包整体消失」比「少一块」严重得多，而且用户看不出区别在哪。

    stance=None 表示**用户没选过**。判例检索仍要按某个申请人视角过滤，
    保守取职工视角；但画像必须如实报 unset，不能把默认值伪装成
    「用户已明确自己是职工」—— 那正是我们说不做的「替人认定立场」。
    """
    case_stance = stance or "employee"
    try:
        decision = _cases.build_decision(
            question, stance=case_stance, slots=slots,
            claims=claims, calc_data=calc_data,
            with_external=with_external)
    except Exception as e:                                   # noqa: BLE001
        decision = {"error": f"判例库暂不可用：{type(e).__name__}",
                    "policy": {"no_win_rate": "本系统不提供胜诉率或成功概率。"}}
    try:
        cfg = persona_cfg or {}
        who = _persona.build_persona(
            question, stance=stance,
            tone_mode=cfg.get("tone_mode"),
            confirmed=bool(cfg.get("confirmed")),
            facts=cfg)
    except Exception as e:                                   # noqa: BLE001
        who = {"stance": stance, "error": f"画像暂不可用：{type(e).__name__}"}
    return {"decision": decision, "persona": who}


def build_extras(state: Any, *, stance: str | None = None,
                 persona: dict | None = None,
                 with_external: bool = False) -> dict[str, Any]:
    """AgentState → 办案包。答案层在 done 前 emit 这一个事件。

    不读数据库、不调模型，所以回放模式（评测 86 题全离线）里五块内容
    一样能生成，评测不需要为它多花一分钱或多等一秒。
    """
    slots_obj = getattr(state, "slots", None)
    slots = slots_obj.known() if hasattr(slots_obj, "known") else dict(slots_obj or {})
    v = getattr(state, "verdict", None)
    verdict = v.model_dump() if hasattr(v, "model_dump") else dict(v or {})
    question = str(getattr(state, "question", "") or "")

    # v119：接入内核走独立分支，不碰下面那套劳动争议规则表。
    # 放在最前面短路，是为了让「问消费问题却先跑一遍 _pick_rules()」
    # 这种隐性串味连发生的机会都没有。
    from . import profiles as _profiles
    _p = _profiles.get()
    if _p.id != "labor":
        return _profile_extras(_p, question, slots, stance, persona)

    deadline_data = _step_data(state, "calculate_deadline")
    calc_data = _step_data(state, "calculate_compensation")
    claims = build_claims(question, verdict, slots, calc_data)

    out = {
        "timeline": build_timeline(slots, deadline_data, question),
        "claims": claims,
        "evidence_check": build_evidence_check(slots, claims, question),
    }
    out.update(_new_blocks(question, slots, claims, calc_data, stance, persona,
                           with_external))
    return out


def from_case(question: str = "", slots: dict | None = None,
              verdict: dict | None = None,
              deadline_data: dict | None = None,
              calc_data: dict | None = None,
              *, stance: str | None = None,
              persona: dict | None = None,
              with_external: bool = False) -> dict[str, Any]:
    """给独立接口 / MCP 用的入口：没有 AgentState 也能算。

    和 build_extras 共用同一组纯函数，避免「网页一套、MCP 一套」，
    两套迟早会长出不一样的结果。stance 只能由调用方显式传，
    答案流里默认是职工视角，并在 persona.stance_source 里如实标 unset。
    """
    slots = slots or {}
    from . import profiles as _profiles
    _p = _profiles.get()
    if _p.id != "labor":
        return _profile_extras(_p, question, slots, stance, persona)
    claims = build_claims(question, verdict, slots, calc_data)
    out = {
        "timeline": build_timeline(slots, deadline_data, question),
        "claims": claims,
        "evidence_check": build_evidence_check(slots, claims, question),
    }
    out.update(_new_blocks(question, slots, claims, calc_data, stance, persona,
                           with_external))
    return out


def _profile_extras(p, question: str, slots: dict,
                    stance: str | None, persona_cfg: dict | None) -> dict[str, Any]:
    """接入内核的办案包：五块整包换掉，另加一块专属能力。

    顶层键**故意多一个 domain**：test_extras 用 `set(r) == 五块` 钉死了
    劳动法内核，那个分支一个键都不能动；接入内核是全新分支，没有历史
    断言，可以安全地长出新块。前端按 `if (d.domain)` 渲染即可。
    """
    from . import domains as _d
    claims = _d.claims_for(p, question)
    return {
        "timeline": _d.timeline_for(p, question),
        "claims": claims,
        "evidence_check": _d.evidence_for(p, claims),
        "decision": _d.decision_for(p, question),
        "persona": _d.persona_for(p, question),
        "domain": _d.build_domain(p, question),
    }


def cited_articles() -> list[str]:
    """供外部（MCP 的 resources、文档）列举本模块引用到的条号。"""
    return list(CITES)
