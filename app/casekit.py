# -*- coding: utf-8 -*-
"""案件级附件（v124）：每个任务一份「程序时间表」+ 一份「文书骨架」。

和办案包 extras 的区别，是这一版最要紧的边界：

  · extras 是**每轮答案末尾**整包 emit 的产物，跟着答案走、每轮重算；
  · 附件是**任务级**的，用户自己决定开不开（默认关），开着就一直挂在
    聊天框下面，切任务按 task_id 恢复，不会被下一轮答案冲掉。

程序时间表补上了 v124 之前缺的两条分支：不服裁决 → 起诉（一审）→
上诉（二审）→ 执行，以及服从裁决 → 生效 → 执行。分支里的法定期间只写
本库能查到原文的那些；《民事诉讼法》没有随包收录，所以上诉期间不引
条号，只提示「以判决书载明的告知为准」——不编库外条号，这条和
逐句溯源是同一个承诺。

文书骨架沿用 docgen 文件头的两条铁律：请求事项 / 金额 / 法律依据留空，
事实段只摘用户自己说过的话。按内核换的是格式，不是替人写诉状。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import docgen as _dg
from . import domains as _domains
from . import extras as _extras
from . import profiles as _profiles

_LAW_ZC = "中华人民共和国劳动争议调解仲裁法"

# 上诉期间：本库未收录《民事诉讼法》原文，故意不引条号。
_APPEAL_DETAIL = (
    "对一审判决不服，可在判决书送达之日起的法定期间内向上一级人民法院上诉。"
    "本库未收录《民事诉讼法》原文，故此处不引条号；具体期间以判决书末尾的"
    "上诉告知为准，不要按这里的天数自行推算。")

_BRANCH_LABOR: dict[str, Any] = {
    "at": "award",
    "label": "裁决之后分两条路",
    "paths": [
        {
            "label": "服从裁决 · 裁决生效",
            "when": "收到裁决书起 15 日后双方均未起诉",
            "detail": "逾期不起诉的，裁决发生法律效力；对方不履行的，可依法"
                      "向人民法院申请强制执行。",
            "cite": f"《{_LAW_ZC}》第五十条",
            "tone": "stay",
        },
        {
            "label": "不服裁决 → 起诉（一审）",
            "when": "收到裁决书起 15 日内",
            "detail": "对仲裁裁决不服的，可自收到裁决书之日起 15 日内向人民"
                      "法院提起诉讼；逾期不起诉，裁决即生效。",
            "cite": f"《{_LAW_ZC}》第四十八条",
            "tone": "go",
        },
        {
            "label": "不服一审判决 → 上诉（二审）",
            "when": "判决书送达后的法定期间（以文书告知为准）",
            "detail": _APPEAL_DETAIL,
            "cite": "",
            "tone": "go",
        },
    ],
}

_BRANCH_GENERIC: dict[str, Any] = {
    "at": "limit",
    "label": "协商不成之后分两条路",
    "paths": [
        {
            "label": "协商 / 调解解决",
            "when": "争议发生后随时",
            "detail": "与对方达成书面和解并留痕，通常比走完整个诉讼程序省时。",
            "cite": "",
            "tone": "stay",
        },
        {
            "label": "起诉（一审）",
            "when": "仍在诉讼时效内",
            "detail": "一般诉讼时效为三年，自知道权利受损及义务人之日起算；"
                      "起算日与中断事由须按具体事实核对。",
            "cite": "《中华人民共和国民法典》第一百八十八条",
            "tone": "go",
        },
        {
            "label": "不服一审判决 → 上诉（二审）",
            "when": "判决书送达后的法定期间（以文书告知为准）",
            "detail": _APPEAL_DETAIL,
            "cite": "",
            "tone": "go",
        },
    ],
}


def build_procedure(pid: str) -> dict[str, Any]:
    """按内核给一条带分叉的程序时间线。

    劳动法复用 build_timeline() 的八个法定阶段，接入内核复用
    domains.timeline_for() 的节点 —— 两处都是已经过条号核验的现成数据，
    这里只补「分叉」，不另造一套日期或期间。
    """
    p = _profiles.resolve(pid)
    if p.id == "labor":
        tl = _extras.build_timeline()
        branch = _BRANCH_LABOR
        label = "劳动用工"
        note = ("八个阶段是《劳动争议调解仲裁法》的法定相对期间；"
                "分叉处的日期一律以你收到的文书为准。")
    else:
        tl = _domains.timeline_for(p, "")
        branch = _BRANCH_GENERIC
        label = p.name
        note = ("节点是本领域的法定顺序与相对期间；"
                "具体日期以受理机关与文书告知为准。")
    nodes = [dict(n) for n in (tl.get("nodes") or [])]
    return {
        "available": True,
        "title": "程序时间表 · " + label,
        "nodes": nodes,
        "branch": branch,
        "anchor_note": tl.get("anchor_note") or "",
        "note": note,
        "disclaimer": "仅供参考，不构成法律意见；期间以你收到的文书与当地规定为准。",
    }


def build_document(pid: str, slots: dict | None = None,
                   profile: dict | None = None,
                   messages: list[dict] | None = None,
                   as_of: date | None = None) -> dict[str, Any]:
    """按内核取文书骨架。事实段只摘用户自己在对话里说过的话。"""
    statements = _dg.pick_statements(messages or [])
    return _dg.for_profile(pid, slots=slots or {}, profile=profile or {},
                           statements=statements, as_of=as_of)


def build(pid: str, slots: dict | None = None,
          profile: dict | None = None,
          messages: list[dict] | None = None,
          as_of: date | None = None) -> dict[str, Any]:
    """附件整包。服务端 /api/case/attachments 直接返回这个结构。"""
    p = _profiles.resolve(pid)
    return {
        "profile": p.id,
        "profile_name": p.name,
        "procedure": build_procedure(p.id),
        "document": build_document(p.id, slots, profile, messages, as_of),
    }
