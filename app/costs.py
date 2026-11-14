# -*- coding: utf-8 -*-
"""成本与周期：把「值不值得追」这道账算成用户能自己判断的三类数字。

为什么要单独一个模块
--------------------
原来这套文案散在三处：`cases.py`（劳动法）写死两句、`domains.py`（四个接入
内核）写「以受理机关为准」、`index.html` 兜底行写「本内核不填数字」。
三处口径不一致，最要命的是后两处**信息量为零**——用户看完还是不知道要花多久、
要花多少钱，而这两件事恰恰决定「划不划来」。

所以这里按证据强度分三层，每层都标明它是什么成色：

  A 类  法定、条号在本地语料里（可核验）→ 直接给数，能引条号就引
        例：仲裁 45 日裁决、仲裁不收费（调解仲裁法 43 / 53 条，库里有）
  B 类  法定、但该法没随包收录 → 给数或区间，**必须标「未随包」**
        例：一审 3–6 个月、二审 3 个月、诉讼费分段费率（民诉法 / 诉讼费用
        交纳办法，库里没有）
  C 类  没有统一标准 → 只给区间，**必须标「行情」**
        例：律师代理费、请假与出庭次数

为什么 B 类也要给：用户要判断的是「1200 元值不值得追」，而这个判断的
分子分母就是 50 元诉讼费、0 元仲裁费、3–6 个月时间。不给这几个数，
结论卡就说不出「划不来」——那三个字必须站在数字上。

不做什么：不预测能拿回多少（金额只来自本地核算）、不预测胜败、
不把 C 类行情写成确定值。
"""
from __future__ import annotations

import re

# 诉讼费分段（《诉讼费用交纳办法》第十三条财产案件，分段累计）。
# 标 B 类：本地未收录该办法原文，标出来让用户知道可以自己核。
_FEE_BRACKETS: tuple[tuple[float, float], ...] = (
    # (上限, 该段费率)；上限为 float("inf") 表示最后一段
    (10_000.0, 0.0),          # 1 万元以下：每件 50 元（固定额，见 base_fee）
    (100_000.0, 0.025),
    (200_000.0, 0.02),
    (500_000.0, 0.015),
    (1_000_000.0, 0.01),
    (2_000_000.0, 0.009),
    (5_000_000.0, 0.008),
    (10_000_000.0, 0.007),
    (20_000_000.0, 0.006),
    (float("inf"), 0.005),
)
_BASE_FEE = 50.0              # 1 万元以下每件 50 元

NOTE_B = ("诉讼费与审限出自《诉讼费用交纳办法》《民事诉讼法》，本地未收录，"
          "按通用标准给区间，具体以受理法院为准。")
NOTE_C = "代理费、鉴定费无全国统一标准，区间是各地常见行情，以当地指导价为准。"

# 各法域的专属投入项（只写该领域真有的东西，不硬凑）
_DOMAIN_EXTRA: dict[str, tuple[str, ...]] = {
    "consumer": (
        "12315 投诉与消协调解不收费；市场监管部门处理不收费。",
    ),
    "civil": (
        "约定仲裁的，机构受理费按标的比例收取，各地标准不同。",
    ),
    "traffic": (
        "伤残鉴定按项计费，先问受理机构；交强险在限额内先行赔付。",
    ),
    "family": (
        "离婚案件受理费每件 50–300 元；财产总额 20 万元以内不另收，"
        "超过部分按 0.5% 加收（《诉讼费用交纳办法》，本地未收录）。",
    ),
}


def litigation_fee(amount: float | None) -> float | None:
    """按标的额算一审受理费（财产案件，分段累计）。金额未知就返回 None。"""
    if amount is None:
        return None
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return None
    if a <= 0:
        return None
    if a <= 10_000:
        return _BASE_FEE
    fee = _BASE_FEE
    low = 10_000.0
    for high, rate in _FEE_BRACKETS[1:]:
        if a <= low:
            break
        seg = min(a, high) - low
        fee += seg * rate
        if a <= high:
            break
        low = high
    return round(fee, 0)


def _fee_phrase(amount: float | None) -> str:
    fee = litigation_fee(amount)
    if fee is None:
        return "诉讼费按标的额分段：1 万元以下每件 50 元，1–10 万元部分按 2.5%。"
    return (f"按可得 {amount:g} 元算，一审受理费约 {fee:g} 元"
            f"（1 万元以下每件 50 元，1–10 万元部分按 2.5%）。")


def labor_lines(amount: float | None = None) -> dict[str, object]:
    """劳动法内核：A 类数字为主，B 类补诉讼段，C 类给区间。

    条号写全名（不写「第五十三条」裸条）——卡片是给人看的，
    「第五十三条」离开书名就不知道是哪部法。
    """
    zw = "《劳动争议调解仲裁法》"
    return {
        "time": (f"时间：受理 5 日内决定（{zw}第二十九条）、45 日内裁决"
                 f"（可延 15 日，最长约 60 日，{zw}第四十三条）；"
                 "起诉后一审 3–6 个月、二审 3 个月。"),
        "money": (f"费用：仲裁不收费（{zw}第五十三条）；"
                  + _fee_phrase(amount)
                  + "代理费另计。"),
        "proxy": "代理费区间：常见 3000–10000 元起，或按标的 10%–20%。",
        "notes": [NOTE_B, NOTE_C],
    }


def domain_lines(profile_id: str, amount: float | None = None) -> dict[str, object]:
    """四个接入内核：没有仲裁前置，走协商 / 投诉 → 调解 → 诉讼。"""
    pid = str(profile_id or "")
    head = {
        "consumer": "时间：协商或投诉通常数日到数周；进入诉讼后一审 3–6 个月。",
        "civil": "时间：协商、催告通常数日到数周；进入诉讼后一审 3–6 个月、"
                 "二审 3 个月。",
        # 道交法第七十三条只写「及时制作」，没写天数——10 日是部门规章的规定，
        # 本地没收录，所以这里不给天数，只给条号。
        "traffic": "时间：事故认定书由交警部门及时制作（道路交通安全法"
                   "第七十三条）；协商不成起诉后一审 3–6 个月。",
        "family": "时间：协议离婚有 30 日冷静期（民法典第一千零七十七条）；"
                  "起诉后一审 3–6 个月、二审 3 个月。",
    }.get(pid, "时间：协商、投诉、调解到诉讼，通常以周或月计；一审 3–6 个月。")

    money = "费用：" + _fee_phrase(amount) + "代理费另计。"
    extras = _DOMAIN_EXTRA.get(pid, ())
    if extras:
        money += extras[0]
    return {
        "time": head,
        "money": money,
        "proxy": "代理费区间：常见 3000–10000 元起，或按标的 10%–20%。",
        "notes": [NOTE_B, NOTE_C],
    }


def bad_percentages(text: str) -> list[str]:
    """返回文本里**不属于费率**的百分比。

    为什么需要这个函数：全项目有一条硬红线「不给胜诉率」，以前的断言写法是
    「一个 % 都不许出现」。v137 起代理费要写「按标的 10%–20%」、诉讼费要写
    「1–10 万元部分按 2.5%」——合法的法定费率也是百分比，一刀切就会把
    正确的内容判成违规。

    所以判据从「有没有 %」变成「这个 % 挂在什么语境上」：只有紧邻
    标的 / 费率 / 部分按 / 元起 / 或按 这几个词才算费率，其余一律当违规。
    这样「胜诉率 70%」「支持率 65%」还是会被抓到，而费率不会误伤。
    """
    out: list[str] = []
    for m in re.finditer(r"\d+(?:\.\d+)?\s*%", str(text or "")):
        ctx = str(text)[max(0, m.start() - 16):m.start()]
        if not any(k in ctx for k in ("标的", "费率", "部分按", "元起", "或按")):
            out.append((ctx + m.group(0)).strip())
    return out


def summary_line(amount: float | None, small: bool) -> str:
    """④ 收益比较的收口句：只对已核金额下判断。"""
    if amount is None:
        return "金额算出来后再判断值不值得追。"
    fee = litigation_fee(amount)
    if small:
        return f"可得约 {amount:g} 元，诉讼费约 {fee:g} 元，往返与代理成本高于可得金额。"
    return f"可得约 {amount:g} 元，诉讼费约 {fee:g} 元，扣掉成本后仍有剩余。"
