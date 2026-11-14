# -*- coding: utf-8 -*-
"""用户画像：只记「用户自己说过的事实」，不推断。

为什么不做「从语气判断你是职工还是 HR」
--------------------------------------
`persona.py` 已经写过这个理由：同一句「公司不给加班费怎么处理」，职工要的是
怎么要回来，HR 要的是怎么合规，方向正好相反，而措辞是最不可靠的信号。
所以立场只能显式给。

那这里做什么
------------
做的是**记忆**，不是**推断**：用户说「我在杭州」「我是外卖员」「我在国企」，
就把这三个事实记下来，下次不用再问，并且**在档案页里列出来给他删**。
判据很简单——每一条都能指回原话片段（存 `source`）。指不回去的，不记。

一个刻意的克制：只认三类句式，且职业要落在白名单后缀里。
宁可少记，也不要把「我是了解」「我是代理人」这类话记成职业。
"""
from __future__ import annotations

import re

# key -> 展示名。顺序就是档案页里的展示顺序。
FACT_LABELS: dict[str, str] = {
    "city": "所在城市",
    "occupation": "职业",
    "employer_type": "单位性质",
}

_EMPLOYER = ("国企", "私企", "外企", "事业单位", "政府机关", "学校", "医院",
             "个体工商户", "上市公司", "民营企业", "国有企业")

# 职业词尾白名单：只认这些结尾，避免把「我是了解」「我是代理人」记成职业。
_OCC_SUFFIX = ("员", "师", "工", "经理", "主管", "总监", "护士", "教师", "司机",
               "程序员", "会计", "律师", "销售", "客服", "保安", "厨师", "设计师",
               "运营", "编辑", "记者", "医生", "骑手")

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("city", re.compile(r"我(?:现在|目前)?(?:在|住在|人在)([\u4e00-\u9fff]{2,4}?)"
                        r"(?:市|这边|那边|上班|工作|生活)?[，,。；;！!？?\s]")),
    ("city", re.compile(r"([\u4e00-\u9fff]{2,4}?)市(?:的)?[，,。；;！!？?\s]")),
    ("employer_type", re.compile(r"我(?:在|是|属于)(?:一家|一个)?("
                                 + "|".join(_EMPLOYER) + r")")),
    # 句尾也要算边界：用户常常就打在最后一个字（「我是外卖员」到这就结束了）。
    ("occupation", re.compile(r"我是(?:一名|一个|个)?([\u4e00-\u9fff]{2,6}?)"
                              r"(?:[，,。；;！!？?\s]|的|$)")),
)

# 这些词看着像职业，其实不是。命中就丢。
_NOT_OCC = ("职工", "劳动者", "员工", "工人", "本人", "当事", "受害", "消费者",
            "家长", "业主", "租客", "房东", "代理人", "家属", "朋友", "新手")


def extract(text: str) -> dict[str, dict[str, str]]:
    """从一句话里抽事实。返回 {key: {"value":..., "source":原句片段}}。

    抽不到就是空 dict——宁可空着，也不拿半句猜测填进去。
    """
    s = re.sub(r"\s+", "", str(text or ""))
    if not s:
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, pat in _RULES:
        if key in out:
            continue
        m = pat.search(s)
        if not m:
            continue
        val = (m.group(1) or "").strip()
        if not val or len(val) < 2:
            continue
        if key == "occupation":
            if any(w in val for w in _NOT_OCC):
                continue
            if not val.endswith(_OCC_SUFFIX):
                continue
        if key == "city" and (val in _EMPLOYER
                              or any(w in val for w in _EMPLOYER)
                              or val.endswith(("公司", "单位", "厂", "店", "部门"))):
            continue
        out[key] = {"value": val[:20], "source": s[max(0, m.start() - 4):m.end()][:40]}
    return out
