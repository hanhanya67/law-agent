# -*- coding: utf-8 -*-
"""地区工资参数表：上年度职工月平均工资，用于三倍封顶与十二年线。

为什么单独一个模块、单独一个 JSON，而不是塞进 law.db：

  1. 这是**外部事实数据**，不是法条。法条要追溯立法版本（谁公布、何时施行、
     何时被替代），工资数据要追溯发文机关和年度，两者的失效机制完全不同。
     混在一张表里，迟早互相污染——比如一次「法规清理」把工资数据当垃圾删掉。

  2. 它只读、条数极少、不需要事务和索引。加一张表换来的是备份、恢复、
     初始化三处都要跟着改，而这三处恰恰是最容易漏的地方。

  3. 最要紧的是：这张表必须能一眼看出**哪些是官方公布、哪些是反推的**。
     JSON 里 basis 字段就摆在数字旁边，比藏在表结构的某一列里更难被忽略。

数据来源原则写在 JSON 的 _readme 里，一句话：查不到官方公布值的城市宁可不收。
填一个「大概差不多」的数字，用户拿着去谈赔偿，少算的那部分是补不回来的。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DATA = Path(__file__).resolve().parent.parent / "data" / "region_params.json"

# 模块级缓存。参数表在一次进程生命周期内不会变；改了 JSON 要重启服务，
# 这在开发和演示里都是常识，比每算一次金额就读一次盘划算。
_CACHE: dict[str, Any] | None = None


@dataclass(frozen=True)
class RegionParam:
    region: str
    year: int
    avg_monthly: int
    basis: str                 # official / derived
    source_doc: str = ""
    source_url: str = ""
    note: str = ""

    @property
    def cap_base(self) -> int:
        """三倍封顶后的月工资基数（元）。"""
        return self.avg_monthly * 3

    @property
    def official(self) -> bool:
        return self.basis == "official"

    def describe(self) -> str:
        """给答案用的一句话出处。"""
        head = (f"{self.region} {self.year} 年度职工月平均工资 "
                f"{self.avg_monthly:,} 元，三倍为 {self.cap_base:,} 元")
        if not self.official:
            head += "（这个数是按社保缴费基数上限反推的，不是官方公布的社平工资）"
        return head


@dataclass
class LookupResult:
    param: RegionParam | None = None
    reason: str = ""           # 查不到的原因，可以直接进答案
    known: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.param is not None


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(_DATA.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 数据文件缺失不该让整个计算崩掉——降级成「查不到参数」，
            # 上层会因此只出公式不出数字，这正是缺参数时该有的行为。
            _CACHE = {"params": []}
    return _CACHE


def _candidates(region: str) -> set[str]:
    """把用户填的地名拆成可用于匹配的候选集合。

    「浙江杭州」「杭州市」「杭州」都要能落到同一条记录上——用户在槽位里
    填什么写法都有可能，而这不是他该操心的细节。
    """
    s = re.sub(r"[\s　]+", "", str(region or ""))
    if not s:
        return set()
    out = {s}
    for suf in ("特别行政区", "自治区", "省", "市"):
        if s.endswith(suf) and len(s) > len(suf):
            out.add(s[: -len(suf)])
    for part in re.split(r"省|市|自治区|特别行政区", s):
        if part:
            out.add(part)
    return out


def _aliases(p: dict) -> set[str]:
    return set(p.get("aliases") or ()) | {p["region"]}


def _match(cands: set[str], p: dict) -> bool:
    """候选写法与条目别名是否指同一地。

    除了相等，还要认包含关系：「浙江杭州」这种把省写在前面的叫法，
    按标点切不出来，但显然就是杭州。只做单向包含（候选里的别名片段），
    不做双向——「北京」不该匹配到「北京周边」这种假想条目。
    """
    for a in _aliases(p):
        for c in cands:
            if a == c or (len(a) >= 2 and a in c):
                return True
    return False


def _mk(p: dict) -> RegionParam:
    return RegionParam(
        region=p["region"], year=int(p["year"]), avg_monthly=int(p["avg_monthly"]),
        basis=p.get("basis") or "official",
        source_doc=p.get("source_doc") or "", source_url=p.get("source_url") or "",
        note=p.get("note") or "",
    )


def resolve(region: str, year: int | None = None) -> LookupResult:
    """查某地某年度的社平工资。

    year 传的是**社平工资所属年度**，不是离职年度——调用方要自己减一。
    这个方向很容易搞反，所以函数不去猜，只在查不到时说清楚差在哪一年。
    """
    params: list[dict] = _load().get("params") or []
    # 保持数据文件里的顺序（北京、上海、杭州），不用 sorted —— 按 unicode
    # 排出来是「上海、北京、杭州」，念着别扭，而这句话要直接进答案。
    seen: set[str] = set()
    order: list[str] = []
    for p in params:
        if p["region"] not in seen:
            seen.add(p["region"])
            order.append(p["region"])
    known = tuple(order)
    cands = _candidates(region)
    if not cands:
        return LookupResult(None, "你没有说在哪个城市，无法判断是否触及三倍封顶。", known)

    hits = [p for p in params if _match(cands, p)]
    if not hits:
        shown = re.sub(r"[\s　]+", "", str(region))
        return LookupResult(
            None,
            f"本库没有收录「{shown}」的上年度职工月平均工资，"
            f"目前只收录 {'、'.join(known)} 三地。", known)

    if year is None:
        return LookupResult(_mk(max(hits, key=lambda x: x["year"])), "", known)

    exact = [p for p in hits if int(p["year"]) == int(year)]
    if exact:
        return LookupResult(_mk(exact[0]), "", known)

    have = sorted({int(p["year"]) for p in hits}, reverse=True)
    name = hits[0]["region"]
    return LookupResult(
        None,
        f"本库只收录到{name} {'、'.join(str(y) for y in have)} 年度的社平工资，"
        f"而你离职年度对应的是 {year} 年度。两个年度的数字不能混用——"
        f"工资每年在涨，套错年份要么高估要么低估。", known)


def supported() -> tuple[str, ...]:
    out: list[str] = []
    for p in (_load().get("params") or []):
        if p["region"] not in out:
            out.append(p["region"])
    return tuple(out)
