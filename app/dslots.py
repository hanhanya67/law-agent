# -*- coding: utf-8 -*-
"""接入内核的案情槽位（v138）。

一句话说清这层在解决什么：v137 之前，除了劳动法，四个接入内核**不抽任何
案情要素**（`prompts._domain_slots` 恒让模型输出 `{}`），所以：

  · 追问问不到点上——`missing_critical()` 拿到的永远是劳动法那四项，
    婚家用户问「对方出轨」，系统回「是公司辞退、自己辞职，还是被迫离职？」；
  · 用户答了也记不住——问完「买的是什么」，他答「一盒过期面包，12 块」，
    下一轮系统还是不知道这句话在说哪件事，因为那句话没地方存。

为什么不能直接把字段塞进 `CaseSlots`：它是 pydantic 写死的劳动法八项，
`save_slots` 里 `CaseSlots(**json)` 会把不认识的键**静默丢掉**，更糟的是
「月工资」会被商品价格误命中——一张 12 元的发票变成「月薪 12 元」。

所以这一层只做三件很小的事，别的一概不管：
  1. **字段表从内核配置来**：`LawProfile.slot_labels` 的键就是存储键。
     这里不另立一张全局表——两份字段清单必然有一天对不上。
  2. **值一律是字符串**。时间写 `YYYY-MM-DD`，金额只写阿拉伯数字，是与否
     写「是/否」。统一成一种类型，提示词拼接和前端显示都不用分支。
  3. **只认字段表里的键**。模型照劳动法字段表输出 `monthly_wage` 时，
     这里直接丢掉，而不是存成一个消费内核的「月工资」。

追问顺序单独一支 `LawProfile.slot_priority`：字段表的顺序是给界面看的
（婚家把「结婚时间」写在最前），而追问要先问「你想解决哪一块」——两者
不是一个次序，混在一起就只能二选一。
"""
from __future__ import annotations

import re
from typing import Any

# 和 `CaseSlots.brief()` 的空态**必须字面一致**：`_rule_memory` 靠
# `"暂无" in slots_brief` 判断要不要把事实写进摘要。两处用两个哨兵，
# 摘要里就会凭空多出一行「（暂无已知案情要素）」。
EMPTY = "（暂无已知案情要素）"

# 单个槽位值的上限。值是要拼进提示词的，任它长到几千字等于把上下文预算
# 交给用户粘贴的一大段案情——但那本来就该走「提问」，不该走槽位。
MAX_LEN = 60

# 金额类字段。判据是**字段名**，不是值：追问「能赔多少」这类问题时，
# 缺的十有八九是这个字段，而它叫什么由各内核自己定（price / expenses /
# agreed_penalty）。按名字里的词认，比再写一张对照表省一次维护。
_MONEY_KEYS = ("price", "expense", "penalty", "amount", "fee", "wage", "cost")


def spec(profile: Any) -> dict[str, str]:
    """字段表：键 → 中文名。内核没配就是空表（此时一切照旧，什么都不抽）。"""
    return dict(getattr(profile, "slot_labels", None) or {})


def priority(profile: Any) -> list[str]:
    """追问顺序：内核指定的那几项在前，其余按字段表顺序补齐。

    为什么要允许内核覆盖：字段表按界面可读性排（婚家从「结婚时间」开始），
    但只丢给用户一个「什么时候结婚的」，问不出他到底想办什么。内核把
    `slot_priority` 写成 ("dispute", "property", "children") 之后，第一句
    问的就是「您这次想解决的是哪一件事」。
    """
    labels = spec(profile)
    head = [k for k in (getattr(profile, "slot_priority", ()) or ()) if k in labels]
    return head + [k for k in labels if k not in head]


def label(profile: Any, key: str) -> str:
    return spec(profile).get(key, key)


def ask_for(profile: Any, key: str) -> str:
    """某个字段的追问说法。**不在字段表里就返回空串**，不返回键名。

    返回键名会把 `leave_reason` 这种内部字段名直接念给用户（调用方传错
    字段时就会这样）。空串让上层回到内核自带的澄清问句，那至少是中文。
    """
    labels = spec(profile)
    if key not in labels:
        return ""
    asks = getattr(profile, "slot_asks", None) or {}
    return str(asks.get(key) or "").strip() or labels[key]


def normalize(value: Any) -> str:
    """把一个值收拾成能存、能拼提示词的短字符串。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        value = "、".join(str(x) for x in value if str(x).strip())
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    s = s.strip("。；;，,、")
    # 模型偶尔会写「不知道 / 未提及 / 不清楚」来占位。那不是事实，是噪声：
    # 存进去之后这一项就永远不会再被追问，等于系统假装自己知道。
    if s in ("未知", "不知道", "未提及", "不清楚", "无", "暂无", "不详"):
        return ""
    return s[:MAX_LEN]


def merge(values: dict[str, str], profile: Any,
          patch: dict[str, Any]) -> list[str]:
    """合并一轮抽取结果，返回真正变了的字段名（顺序按 patch 给的顺序）。

    语义和 `CaseSlots.merge` 一致：空值不覆盖已确认的值——用户这一轮没提
    月工资，不等于他要删掉上一轮说的月工资。要删得去档案里手工改。
    """
    labels = spec(profile)
    changed: list[str] = []
    for k, v in (patch or {}).items():
        if k not in labels:
            continue
        s = normalize(v)
        if not s or values.get(k) == s:
            continue
        values[k] = s
        changed.append(k)
    return changed


def missing(profile: Any, values: dict[str, str]) -> list[str]:
    """还缺哪些要素，按追问顺序。"""
    return [k for k in priority(profile)
            if not str((values or {}).get(k) or "").strip()]


def money_field(profile: Any) -> str:
    """金额类字段里排在最前的那个。没有就返回空串。

    只用于「用户问能赔多少、但他没说自己花了多少」这一种情形——这时候
    再检索十次也没有用，缺的是他的数，不是法条。
    """
    for k in priority(profile):
        if any(w in k for w in _MONEY_KEYS):
            return k
    return ""


def brief(profile: Any, values: dict[str, str]) -> str:
    """拼成一小段给模型看的文字。空态与 `CaseSlots.brief()` 同字面。"""
    labels = spec(profile)
    parts = [f"{labels.get(k, k)}={v}" for k, v in (values or {}).items()
             if k in labels and str(v).strip()]
    return "；".join(parts) if parts else EMPTY


def known(profile: Any, values: dict[str, str]) -> dict[str, str]:
    """只留字段表认得、且非空的项。给前端「记下」那一步和存档用。"""
    labels = spec(profile)
    return {k: str(v) for k, v in (values or {}).items()
            if k in labels and str(v).strip()}
