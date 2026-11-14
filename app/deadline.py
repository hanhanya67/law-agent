# -*- coding: utf-8 -*-
"""期限计算。整个项目里唯一「错过就不可挽回」的一块。

为什么单独成模块而不是塞进 tools.py：
  1. 它必须能被单测。`scripts/test_deadline.py` 直接 import 这里，不经过
     Agent 循环——期限算错比答案措辞难听严重得多，不能靠「跑一个问题看
     看输出对不对」来验证。
  2. 它要能被文书生成复用（申请书末尾的落款日期与时效提示）。
  3. 规则表是数据，不是逻辑。加一条争议类型不该改代码结构。

一条不可让步的原则：**不给单一确定日期**。
法定起算点是「知道或者应当知道权利被侵害之日」，而「应当知道」在实务里
本身就是争议焦点，算不出来。所以这里的输出是「多个候选起算点 + 哪一个
最保守」，而不是一个日子。给单一日期是假装精确，比不给更危险——用户会
照着那个日子躺到过期。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------- 规则表
#
# article：支撑该期限的条文 id，必须能在语料库里查到。写死条号而不核验，
#          就是这个项目一直在防的「没有出处的引用」。
# months / days：二选一。按「年/月」算的和按「日」算的，跨月天数不一样，
#          不能统一折算成天。
# special：这条规则自己的例外，必须原样传达给用户，不能被摘要吃掉。

RULES: dict[str, dict[str, Any]] = {
    "wage": {
        "label": "追索劳动报酬（拖欠工资、加班费）",
        "months": 12,
        "article": "调解仲裁法_27",
        "onset_label": "劳动关系终止之日",
        # 27 条第四款：存续期间不受一年限制。这条最容易被说反——模型经常
        # 让还在职的人「赶紧去仲裁，不然过一年就没了」，那是错的。
        "special": "劳动关系存续期间因拖欠劳动报酬发生争议的，申请仲裁不受一年时效限制；"
                   "但劳动关系终止的，应当自终止之日起一年内提出。",
    },
    "dismissal": {
        "label": "违法解除、经济补偿或赔偿金",
        "months": 12,
        "article": "调解仲裁法_27",
        "onset_label": "解除或终止之日",
        "special": "",
    },
    "no_contract": {
        "label": "未签书面劳动合同的二倍工资差额",
        "months": 12,
        "article": "调解仲裁法_27",
        "onset_label": "应当知道未签合同之日",
        "special": "二倍工资差额的时效起算，实务中有「整体起算」与「按月分别起算」"
                   "两种口径，本页按最早的那个起算日算。",
    },
    "arbitration_award": {
        "label": "劳动者对终局裁决不服，向法院起诉",
        "days": 15,
        "article": "调解仲裁法_48",
        "onset_label": "收到仲裁裁决书之日",
        "special": "终局裁决指追索劳动报酬、工伤医疗费、经济补偿或赔偿金，"
                   "不超过当地月最低工资标准十二个月金额的争议等（第四十七条）。",
    },
    "arbitration_other": {
        "label": "对非终局裁决不服，向法院起诉",
        "days": 15,
        "article": "调解仲裁法_50",
        "onset_label": "收到仲裁裁决书之日",
        "special": "期满不起诉的，裁决书发生法律效力。",
    },
    "employer_revoke": {
        "label": "用人单位申请撤销终局裁决",
        "days": 30,
        "article": "调解仲裁法_49",
        "onset_label": "收到仲裁裁决书之日",
        "special": "向劳动争议仲裁委员会所在地的中级人民法院提出，且须符合"
                   "第四十九条列举的六种情形之一。",
    },
    "injury": {
        "label": "工伤认定申请",
        # 本库没有《工伤保险条例》，给不出可核验的期限。占位是为了让模型
        # 有工具可调，而不是凭印象编一个数字。见 compute() 里的处理。
        "unsupported": True,
        "article": "",
        "onset_label": "事故伤害发生之日",
        "special": "",
    },
}

# ---------------------------------------------------------------- 通用时效
#
# v126：其余四部法不走劳动争议那一套一年仲裁时效。民法典 188–199 条就在
# 本地语料里，可直接核验；这里只收录**正文非空**的条号（189–191 在库里是
# 空条目，引用了就是无出处）。
# 一条原则不变：中止 / 中断会改变起算日，但「是否成立」是事实认定，
# 程序不能替用户认定，所以只给提示与「按新的起算日再算一次」，不编新日期。
CIVIL_RULES: dict[str, dict[str, Any]] = {
    "civil_general": {
        "label": "一般民事请求权（三年诉讼时效）",
        "months": 36,
        "article": "民法典_188",
        "onset_label": "知道或者应当知道权利受到损害以及义务人之日",
        "special": "向人民法院请求保护民事权利的诉讼时效期间为三年；自权利受到"
                   "损害之日起超过二十年的，人民法院不予保护（第一百八十八条）。"
                   "期间届满后义务人可以提出不履行义务的抗辩（第一百九十二条），"
                   "人民法院不得主动适用诉讼时效的规定（第一百九十三条）。",
        "extra_articles": ["民法典_194", "民法典_195", "民法典_197",
                           "民法典_198"],
    },
    "civil_no_limit": {
        "label": "不适用诉讼时效的请求权（第一百九十六条）",
        "unsupported": True,
        "article": "民法典_196",
        "onset_label": "",
        "special": "",
        "error": "这类请求权依法不适用诉讼时效，没有可计算的截止日",
        "hint": "《民法典》第一百九十六条列举的请求权（请求停止侵害、排除妨碍、"
                "消除危险；不动产物权和登记的动产物权的权利人请求返还财产；"
                "请求支付抚养费、赡养费或者扶养费等）**不适用诉讼时效的规定**，"
                "因此这里不给一个截止日——给一个就等于造了法律上不存在的期限。"
                "但请求权是否落入该条，仍须结合具体事实判断。",
        "hint_articles": ["民法典_196"],
    },
    "civil_exclusion": {
        "label": "撤销权、解除权等存续期间（第一百九十九条）",
        "unsupported": True,
        "article": "民法典_199",
        "onset_label": "",
        "special": "",
        "error": "存续期间不适用中止、中断、延长；起算日按第一百九十九条认定，不在此推定",
        "hint": "《民法典》第一百九十九条：法律规定或者当事人约定的撤销权、"
                "解除权等权利的存续期间，自权利人知道或者应当知道权利产生之日起"
                "计算，**不适用有关诉讼时效中止、中断和延长的规定**；届满后权利"
                "消灭。具体期间由相应法律或约定确定，本库未收录的一律不给数字。",
        "hint_articles": ["民法典_199"],
    },
}

_INTERRUPTION_LABELS = {
    "claimed": "曾向对方提出履行请求",
    "agreed": "对方同意履行义务",
    "filed": "已经提起诉讼或者申请仲裁",
    "other": "存在与起诉、申请仲裁同等效力的情形",
    "suspended": "最后六个月内发生法定障碍（中止）",
}


def rules_for(profile: str = "labor") -> dict[str, dict[str, Any]]:
    """按内核选规则表。labor 逐字返回原表（老回归钉着它的键与文案）。"""
    return RULES if profile == "labor" else CIVIL_RULES


# 期间计算的依据。民法典 200–203 条就在库里，正好是通用规则，
# 用它们比自己写一套「加一年」的算术可靠——尤其是闰年和月末。
PERIOD_ARTICLES = ["民法典_200", "民法典_201", "民法典_202", "民法典_203"]


# ---------------------------------------------------------------- 日期算术

def parse_date(s: str) -> date | None:
    """尽量宽容地解析日期。返回 None 表示解析不了，由上层报错。

    只给「2024-03」也算——很多人记不清具体哪一天。此时按当月 1 日算，
    由上层在 warnings 里标注「按月初估算」，因为这一天的差别可能正好跨过
    截止日。
    """
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    for fmt in ("%Y-%m", "%Y/%m", "%Y年%m月"):
        try:
            return datetime.strptime(s, fmt).date().replace(day=1)
        except ValueError:
            continue
    # 只有年份
    if s.isdigit() and len(s) == 4:
        return date(int(s), 1, 1)
    return None


def is_month_precision(s: str) -> bool:
    """判断用户是不是只给了年月（没有日）。用于决定要不要提示「按月初估算」。

    用整串匹配而不是数分隔符：`2024-3-1` 和 `2024.03` 都合法，靠数
    「-」出现几次来判断很容易看走眼。
    """
    return bool(re.fullmatch(r"\s*\d{4}\s*[-/.年]\s*\d{1,2}\s*月?\s*",
                             (s or "").strip()))


def add_months(d: date, months: int) -> date:
    """按民法典 202 条：到期月的对应日为期间最后一日；没有对应日的，月末日。

    2024-02-29 加 12 个月 → 2025 年 2 月没有 29 日 → 2025-02-28。
    这正是法条里「没有对应日的，月末日为期间的最后一日」的情形，
    不是我们拍脑袋取的近似。
    """
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def compute_deadline(onset: date, months: int = 0, days: int = 0) -> date:
    """算截止日。起始当日不计入，自下一日开始计算（民法典 201 条）。

    按年/月算：202 条说的是「到期月的**对应日**为期间的最后一日」，而这个
    对应日是相对起算日说的，不是相对次日。所以 2024-01-01 知道权利被侵害、
    一年时效 → 期间从 01-02 开始 → 末日 2025-01-01。直接用 onset 加 12 个月，
    不是 onset+1 再加。

    按日算：15 日 → 01-02 是第 1 天，第 15 天是 01-16，即 onset+15。
    """
    if months:
        return add_months(onset, months)
    return onset + timedelta(days=days)


# ---------------------------------------------------------------- 主计算

LEVELS = {
    "expired": "已过期限",
    "urgent": "紧急（15 天内）",
    "near": "临近（60 天内）",
    "ok": "时间尚充裕",
}


def _level(left: int) -> str:
    if left < 0:
        return "expired"
    if left <= 15:
        return "urgent"
    if left <= 60:
        return "near"
    return "ok"


def _holiday_hint(d: date) -> str:
    """末日落在周末时给一句提示，但**不自动顺延**。

    民法典 203 条说的是「法定休假日」，周六周日是否算在内，实务里各地
    仲裁委口径不完全一致。自动顺延看起来贴心，实际是替用户做了一个
    我们无权做的判断——万一某地不顺延，用户就真的过期了。
    """
    if d.weekday() >= 5:
        return ("最后一日是周" + "六日"[d.weekday() - 5] +
                "。民法典第二百零三条规定末日为法定休假日的顺延至假期结束次日，"
                "周末是否适用请以受理机关口径为准，建议提前办理。")
    return ""


def compute(dispute: str, onset_text: str, relation_ended: bool | None = None,
            end_text: str = "", as_of: date | None = None,
            profile: str = "labor", interruption: str = "") -> dict[str, Any]:
    """算出起算点、截止日、剩余天数。

    v126：`profile` 决定用哪张规则表。labor 走原劳动争议表（一年仲裁时效 +
    在职讨薪不受限），其余四内核走民法典 188–199 的三年诉讼时效。
    `interruption` 只影响提示文案，绝不改日期——中止/中断是否成立是事实
    认定，工具替用户认了，算出来的「新截止日」就是编的。

    返回的 dict 直接给 tools.py 渲染成 summary，也给文书生成复用。

    relation_ended 是三态，不是布尔：
      True  → 关系已终止，讨薪从终止之日起一年（27 条第四款特别规定）
      False → 关系存续，讨薪**不受**一年时效限制
      None  → 用户没说，两分支都摆出来，标注「取决于你是否还在职」

    为什么这里必须三态：默认 False 会把「没说」当成「在职」，从而给出
    「不受时效限制」这个过于乐观的结论；默认 True 又会吓到一个还在职的人。
    两者都是替用户猜，而猜错的代价是他按错的时间线行动。
    """
    as_of = as_of or date.today()
    rules = rules_for(profile)
    rule = rules.get(dispute)
    if rule is None:
        return {"ok": False, "error": f"未知的争议类型：{dispute}",
                "known": list(rules)}

    notes: list[str] = []

    # 本库没有这部法、或者这类请求权根本不适用时效时，老实说没有，
    # 而不是编一个数字。labor 的「工伤认定」文案逐字保留（老回归钉着）。
    if rule.get("unsupported"):
        injury_hint = ("工伤认定的申请期限由《工伤保险条例》规定，本库没有收录该法，"
                       "因此无法给出带条号的期限。一般规则是单位应当在事故伤害发生"
                       "之日起 30 日内提出，单位不提的，职工或其近亲属可在 1 年内"
                       "直接申请——**这一句没有经过本库核验**，请以当地社保部门口径为准。"
                       "逾期是否还能申请，各地做法不同，建议尽快致电 12333 确认。")
        return {
            "ok": False,
            "unsupported": True,
            "label": rule["label"],
            "error": rule.get("error") or
                     "本库未收录《工伤保险条例》，给不出可核验的认定期限",
            "hint": rule.get("hint") or injury_hint,
            "articles": list(rule.get("hint_articles") or []),
        }

    onset = parse_date(onset_text)
    if onset is None:
        return {"ok": False, "label": rule["label"],
                "error": f"起算日期无法解析：{onset_text!r}，请用 YYYY-MM-DD"}
    if onset > as_of:
        return {"ok": False, "label": rule["label"],
                "error": "起算日晚于今天，无法推算期限。请确认是不是填错了年份"}

    if is_month_precision(onset_text):
        notes.append(f"起算日只给了年月，按当月 1 日（{onset}）估算。"
                     f"实际知道日期更晚的，期限相应往后推。")

    base = {
        "ok": True,
        "dispute": dispute,
        "label": rule["label"],
        "rule_text": rule["special"],
        "as_of": as_of.isoformat(),
        "articles": ([rule["article"]] +
                     list(rule.get("extra_articles") or []) + PERIOD_ARTICLES),
        "profile": profile,
        "months": rule.get("months", 0),
        "days": rule.get("days", 0),
        # 起算日只到年月时，算出来的日子是估的。这个标记一路传到答案里，
        # 否则用户会把「8 月 1 日」当成他真实离职的日子。
        "onset_estimated": is_month_precision(onset_text),
        "notes": notes,
    }

    # ---------- 非劳动内核：三年诉讼时效 + 中止 / 中断提示 ----------
    # 只给法条原文要点与「请按新的起算日再算一次」，不给新的截止日。
    # 194 条的中止要求「最后六个月内」、195 条的中断要求四类法定情形，
    # 哪一条成立都要看证据，程序没有资格替用户认定。
    if profile != "labor":
        notes.extend([
            "《民法典》第一百九十四条：在时效期间**最后六个月内**因法定障碍不能"
            "行使请求权的，时效中止，自中止原因消除之日起满六个月届满。"
            "障碍是否成立按证据认定。",
            "《民法典》第一百九十五条：向义务人提出履行请求、义务人同意履行、"
            "提起诉讼或者申请仲裁等情形，时效中断，从中断、有关程序终结时起"
            "重新计算。",
            "《民法典》第一百九十七条：时效期间、计算方法与中止中断事由由法律"
            "规定，当事人约定无效；第一百九十八条：法律对仲裁时效有规定的，"
            "依照其规定。",
        ])
        if interruption:
            lab = _INTERRUPTION_LABELS.get(interruption, "曾主张权利")
            notes.append(
                "你勾选了「" + lab + "」：若该事实成立，时效期间应当重新计算，"
                "**下面这个截止日是按未中断算的**，请把中断发生之日当作新的"
                "起算日再算一次。是否构成中止或中断，由受理机关结合证据认定。")
            base["recompute_hint"] = True
        base["notes"] = notes

    # ---------- 讨薪：三种关系状态，走三条路 ----------
    if dispute == "wage":
        end = parse_date(end_text)
        if relation_ended is False:
            # 用户明说「还在职」时，终止日期一律不采信。
            #
            # 这里原来是 `relation_ended is False and not end`。页面上先填过
            # 终止日期、再点回「还在职」，那个输入框只是被隐藏，值还在——
            # 后端于是按终止日算出了截止日。用户点了在职，拿到的却是离职的
            # 结论，界面上也看不出是哪一环说了算。明示的状态必须盖过残留字段。
            if end:
                notes.append(f"你选了「还在职」，所以没有采用之前填的终止日期"
                             f"（{end.isoformat()}）——劳动关系还没结束，"
                             f"就不存在「终止之日」这个起算点。")
            # 在职状态：27 条第四款前半段，不受一年限制。**不给截止日**——
            # 给一个截止日就等于凭空造了一个法律上不存在的期限。
            base.update({
                "unlimited": True,
                "candidates": [],
                "conservative": None,
                "branch": "ongoing",
            })
            base["notes"] = notes + [
                "劳动关系存续期间因拖欠劳动报酬申请仲裁，**不受一年时效限制**"
                "（第二十七条第四款）。也就是说还在职的话，现在不急着去仲裁。",
                "但这条保护在你离职那天就结束了：一旦劳动关系终止，"
                "必须自终止之日起一年内提出。真要主张，离职前把证据固定好——"
                "工资条、考勤、欠薪的聊天记录。",
            ]
            return base

        if end:
            return _single(base, rule, end, as_of,
                           "劳动关系终止之日", "第二十七条第四款特别规定")

        if relation_ended is None:
            # 用户既没说是否在职，也没给离职日期。此前的写法会落进下面那段
            # 「你说了已终止但没给日期」的兜底——那是把我们的默认值当成
            # 用户的原话，属于冤枉用户。这里改成条件性输出，让模型去追问。
            base.update({
                "unlimited": None,
                "conditional": True,
                "candidates": [],
                "conservative": None,
                "branch": "unknown",
            })
            base["notes"] = notes + [
                "**现在还不能给截止日**：取决于你是不是还在职，两种情形差别很大。",
                "· 还在职：追索劳动报酬申请仲裁**不受一年时效限制**"
                "（第二十七条第四款），不急着去仲裁。",
                "· 已离职：必须自**劳动关系终止之日起一年内**提出。",
                "请先确认劳动关系是否已经结束、以及结束的日期，再来算一次。",
            ]
            return base

        # 说了已终止但没给日期 → 只能按知道之日兜底，且必须说明偏乐观
        notes.append("你说了劳动关系已终止，但没给终止日期。讨薪的时效从"
                     "**终止之日**起算，缺这个日期就只能按「知道权利被侵害之日」"
                     "粗算，结果偏乐观——实际截止日大概会比算出来的更晚，"
                     "但请尽快补上离职日期再算一次。")
        res = _single(base, rule, onset, as_of,
                      "知道权利被侵害之日（缺离职日期，兜底估算）",
                      "第二十七条第一款一般规则")
        return res

    # ---------- 其余类型：起算点唯一 ----------
    return _single(base, rule, onset, as_of,
                   rule["onset_label"], "")


def _single(base: dict, rule: dict, onset: date, as_of: date,
            onset_label: str, why: str) -> dict[str, Any]:
    """只有单一确定起算点时的填充。候选列表保留一个，结构统一便于前端渲染。"""
    if rule.get("months"):
        dl = compute_deadline(onset, months=rule["months"])
    else:
        dl = compute_deadline(onset, days=rule["days"])
    left = (dl - as_of).days
    lv = _level(left)
    cand = {
        "onset_label": onset_label,
        "onset": onset.isoformat(),
        "deadline": dl.isoformat(),
        "left_days": left,
        "level": lv,
        "level_text": LEVELS[lv],
        "why": why,
        "conservative": True,
        "estimated": base.get("onset_estimated", False),
    }
    hh = _holiday_hint(dl)
    if hh:
        cand["holiday_hint"] = hh

    base.update({"unlimited": False, "candidates": [cand],
                 "conservative": cand, "branch": "ended"})
    base["notes"] = (list(base["notes"]) +
                     _state_notes(lv, base["dispute"],
                                  base.get("profile", "labor")))
    return base


def _state_notes(level: str, dispute: str, profile: str = "labor") -> list[str]:
    # 措辞必须跟着阶段走。起诉期只剩 6 天时还劝人「递交仲裁申请」，
    # 是把这个用户往反方向推——那一阶段仲裁程序已经结束了。
    # v126：非劳动内核不能说「递仲裁申请」——那些法域根本没有劳动仲裁
    # 这一道前置程序，写上去就是把劳动争议的流程串到了消费/合同案子上。
    if profile != "labor":
        if level == "expired":
            return ["按上面这个起算点，三年诉讼时效**已经届满**。这不等于一定"
                    "不能主张：是否发生中止、中断，以及义务人是否提出时效抗辩，"
                    "都要结合事实认定，请尽快带齐材料核实。"]
        if level == "urgent":
            return ["剩余不足 15 天。先把主张权利的书面动作做扎实（催告函、"
                    "聊天记录、EMS 回执），这既能留证，也可能构成时效中断。"]
        if level == "near":
            return ["剩余不足 60 天。证据与请求金额该定下来了；金额不高时，"
                    "先对照「值不值得追」决策卡算清投入是否值得。"]
        return []
    litigation = dispute in ("arbitration_award", "arbitration_other",
                             "employer_revoke")
    act = "向法院起诉" if litigation else "把仲裁申请递上去"
    if level == "expired":
        if litigation:
            return ["按上面这个起算点，起诉期限**已经届满**。"
                    "驳回起诉后能否救济，取决于有没有耽误期间的正当理由，"
                    "需要尽快带着全部材料找律师看。"]
        return ["按上面这个起算点，仲裁时效**已经届满**。这不等于一定不能主张："
                "时效可以因向对方主张权利、对方同意履行而中断（第二十七条第二款），"
                "也可以因不可抗力中止。有没有中断的事实，是这案子还能不能打的关键。"]
    if level == "urgent":
        return [f"剩余不足 15 天。建议立刻{act}——这个动作本身就把期限固定住了，"
                f"材料不齐可以后补。"]
    if level == "near":
        if litigation:
            return ["剩余不足 60 天。起诉要准备起诉状和证据副本，排期不短，"
                    "现在就该动手，别拖到最后几天。"]
        return ["剩余不足 60 天。可以先发一份书面催告（微信、邮件、EMS 都算），"
                "这同样构成时效中断，而且能给后面的仲裁留下证据。"]
    return []
