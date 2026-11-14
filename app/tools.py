# -*- coding: utf-8 -*-
"""
工具层。Agent 的手脚。

三条设计原则，每条都有代价在后面等着：
  1. 工具要少而精。超过 10 个，模型选错率显著上升。
  2. description 是写给模型看的 Prompt，要写"什么时候用"，不只写"做什么"。
  3. 工具内部异常绝不 raise，而是转成 error 字段回灌。
     抛异常会直接打断循环；回灌错误，模型下一轮往往自己就改对了。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from .retrieval import LawIndex, cn2int
from .schemas import Evidence, SLOT_LABELS, ToolResult
from . import deadline as dl_mod
from . import docgen
from . import region_params

# 「民法典施行之后，违约金调整的条文变了吗」——这类问法在比较新旧法，
# 不点名任何一部旧法。命中这些词就说明用户想要的是「变了没有」，
# 那就必须能把旧的（已废止）条文也检出来。
_VERSION_WORDS = ("变了", "改了", "修改", "修订", "废止", "还适用", "还有效",
                  "施行", "以前", "原来", "旧法", "之前", "变化")


@dataclass
class ToolContext:
    index: LawIndex
    state: Any = None          # AgentState，部分工具需要读槽位
    # 个人档案（真实姓名、电话、单位名称等）。只有 draft_document 用它——
    # 这些字段**绝不能进槽位**：槽位会被渲染进答案正文，姓名和身份证号
    # 出现在「结论」段里既没必要也不安全。文书工具是唯一的出口。
    profile: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[BaseModel, ToolContext], ToolResult]

    def schema(self) -> dict:
        js = self.args_model.model_json_schema()
        js.pop("title", None)
        js.pop("$defs", None)
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": js}}


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, description: str, args_model: type[BaseModel]):
    def deco(fn):
        REGISTRY[name] = ToolSpec(name, description, args_model, fn)
        return fn
    return deco


def all_schemas() -> list[dict]:
    return [spec.schema() for spec in REGISTRY.values()]


def dispatch(name: str, raw_args: dict, ctx: ToolContext) -> ToolResult:
    """统一入口。参数校验失败也返回 ToolResult，让模型看到错误原文。"""
    t0 = time.time()
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(error=f"不存在名为 {name} 的工具。可用工具："
                                f"{'、'.join(REGISTRY)}",
                          summary=f"工具名 {name} 无效",
                          elapsed_ms=int((time.time() - t0) * 1000))
    try:
        args = spec.args_model(**raw_args)
    except ValidationError as e:
        msgs = "；".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                         for err in e.errors())
        return ToolResult(error=f"参数校验失败：{msgs}",
                          summary="参数不合法，请修正后重试",
                          elapsed_ms=int((time.time() - t0) * 1000))
    try:
        res = spec.fn(args, ctx)
    except Exception as e:                    # noqa: BLE001
        res = ToolResult(error=f"{type(e).__name__}: {e}",
                         summary="工具执行出错")
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


# ---------------------------------------------------------------- 参数模型

class SearchArgs(BaseModel):
    query: str = Field(description="检索语句，用法律术语，不要口语")
    topk: int = Field(default=6, description="返回条数，默认 6")
    only_valid: bool = Field(default=True, description="是否只检索现行有效的条文")


class ArticleArgs(BaseModel):
    law_name: str = Field(description="法律名称，全称或简称均可，如 劳动合同法")
    article_no: str = Field(description="条号，中文或阿拉伯数字均可，如 第四十七条 或 47")


class IdArgs(BaseModel):
    article_id: str = Field(description="条文 ID，形如 劳动合同法_47")


class CalcArgs(BaseModel):
    monthly_wage: int = Field(description="离职前十二个月平均月工资，单位元")
    start_date: str = Field(description="入职日期，YYYY-MM 或 YYYY-MM-DD")
    end_date: str = Field(description="离职日期，YYYY-MM 或 YYYY-MM-DD")
    leave_reason: str = Field(default="公司辞退",
                              description="离职方式：公司辞退 / 违法辞退 / 协商解除 / 被迫离职 / 主动辞职")
    region: str = Field(default="", description="所在城市，如 北京 / 杭州。"
                              "**算经济补偿或赔偿金时务必要传**——它决定是否触及"
                              "三倍封顶；不传就只能给出计算公式、给不出金额")


class AskArgs(BaseModel):
    question: str = Field(description="要向用户追问的问题，只问一个关键要素")


class RegionArgs(BaseModel):
    region: str = Field(description="城市名，如 北京 / 上海 / 杭州")
    year: int | None = Field(
        default=None,
        description="社平工资所属年度，即**离职年度的上一年**（不是离职年度本身）。"
                    "不填则取本库收录的最新年度")


class DeadlineArgs(BaseModel):
    dispute: Literal["wage", "dismissal", "no_contract", "arbitration_award",
                     "arbitration_other", "employer_revoke", "injury"] = Field(
        description="争议类型。wage=追索劳动报酬/拖欠工资/加班费；"
                    "dismissal=违法解除、经济补偿、赔偿金；"
                    "no_contract=未签书面合同的二倍工资；"
                    "arbitration_award=劳动者对终局裁决不服起诉；"
                    "arbitration_other=对非终局裁决不服起诉；"
                    "employer_revoke=用人单位申请撤销终局裁决；"
                    "injury=工伤认定申请")
    onset: str = Field(
        description="起算日期，YYYY-MM-DD。含义随类型变：解除之日 / 收到裁决书之日 / "
                    "知道权利被侵害之日。只记得年月就填 YYYY-MM，工具会标注这是估算")
    relation_ended: bool | None = Field(
        default=None,
        description="劳动关系是否已经结束。**只在 dispute=wage 时有意义**："
                    "true=已离职；false=还在职；null=用户没说。"
                    "不确定时必须传 null，不要猜——猜错了会给出完全相反的结论")
    end_date: str = Field(
        default="", description="劳动关系终止日期 YYYY-MM-DD，已知就填（讨薪必须填）")
    as_of: str = Field(
        default="", description="计算基准日，留空表示按今天算")


# ---------------------------------------------------------------- 工具实现

def _to_ev(a: dict, score: float = 0.0, source: str = "direct") -> Evidence:
    return Evidence(article_id=a["id"], law_name=a["law_name"],
                    article_no=a["article_no"], article_no_cn=a["article_no_cn"],
                    content=a["content"], score=round(score, 4),
                    source=source, status=a.get("status", "现行有效"),
                    repealed_note=a.get("repealed_note") or "")


@tool("search_statute",
      "当需要查找法条但不知道具体条号时使用。输入用法律术语表述的检索语句，"
      "返回匹配度最高的若干条文。已知条号时请改用 get_article，后者精确率 100%。",
      SearchArgs)
def search_statute(a: SearchArgs, ctx: ToolContext) -> ToolResult:
    only_valid = a.only_valid
    note = ""
    # 用户明确问到已废止的法律时，必须放开过滤，否则永远演示不出
    # "命中旧法 → 提示已废止 → 给出替代条文" 这条链路。
    # 注意「部分失效」不算已废止：解释（一）整体还活着，只是某一款死了。
    live = {x["law_short"] for x in ctx.index.articles
            if (x.get("status") or "现行有效") in ("现行有效", "部分失效")}
    dead = {x["law_short"] for x in ctx.index.articles} - live
    hit_dead = [d for d in dead if d in a.query]
    # 只看检索式是不够的：检索式是「违约金的调整」这类术语短语，常常不含
    # 法律名本身。用户原话里若点名了「合同法第114条」，必须一并放开过滤——
    # 否则版本题会被时效过滤静默跳过，模型看到零命中就直接拒答（实测踩过：
    # 「以前合同法是第114条，现在民法典施行了还适用吗」被拒答）。
    if not hit_dead and ctx.state is not None:
        q_text = getattr(ctx.state, "question", "") or ""
        hit_dead = [d for d in dead if d in q_text]
        # 还有一类问法**根本不点名旧法**：「民法典施行之后，违约金调整的条文
        # 变了吗」。上面按法律名判断的规则在这里一个都命不中，于是照旧只检索
        # 现行有效——而这道题的正确答案必须新旧并给，旧的那条（合同法第 114
        # 条）永远进不了证据。实测：同一条检索式，只现行拿不到 114，
        # 放开时效过滤后它排第 3。
        if not hit_dead and "民法典" in q_text and any(
                k in q_text for k in _VERSION_WORDS):
            only_valid = False
            note = "（问句在比较民法典前后的规定，已放开时效过滤）"
    if hit_dead:
        only_valid = False
        note = f"（查询涉及已废止的《{hit_dead[0]}》，已放开时效过滤）"

    hits = ctx.index.search(a.query, topk=max(1, min(a.topk, 10)),
                            only_valid=only_valid)
    if not hits:
        return ToolResult(
            summary=f"以「{a.query}」检索，未命中任何条文{note}",
            data={"query": a.query, "count": 0},
        )
    evs = [_to_ev(art, score) for score, art in hits]
    top = evs[0]
    flag = "" if top.status == "现行有效" else f"（注意：该条{top.status}）"
    return ToolResult(
        summary=f"命中 {len(evs)} 条，最高分 {top.score:.2f}；"
                f"首条为《{top.law_name}》{top.article_no_cn}{flag}{note}",
        data={"query": a.query, "count": len(evs),
              "ids": [e.article_id for e in evs]},
        evidence=evs,
    )


@tool("get_article",
      "按法律名称和条号精确获取条文原文。当用户明确提到某条某款，或上一轮检索"
      "已给出条号时，必须优先使用本工具，不要用 search_statute 去碰运气。",
      ArticleArgs)
def get_article(a: ArticleArgs, ctx: ToolContext) -> ToolResult:
    art = ctx.index.get_article(a.law_name, a.article_no)
    if art is None:
        return ToolResult(summary=f"未找到《{a.law_name}》{a.article_no}，可能未收录",
                          error="条文不存在或未收录")
    ev = _to_ev(art, 1.0, "precise")
    return ToolResult(summary=f"已取到《{ev.law_name}》{ev.article_no_cn}"
                              f"（{ev.status}），{len(ev.content)} 字",
                      data={"id": ev.article_id},
                      evidence=[ev])


@tool("check_validity",
      "查询某条文的时效性与替代关系。命中任何 status 不是「现行有效」的条文后，"
      "必须调用本工具确认它被哪一条替代，否则会拿废止条款回答用户。",
      IdArgs)
def check_validity(a: IdArgs, ctx: ToolContext) -> ToolResult:
    art = ctx.index.by_id.get(a.article_id)
    if art is None:
        return ToolResult(error=f"条文 {a.article_id} 不存在", summary="条文不存在")
    status = art.get("status", "现行有效")
    sup = art.get("superseded_by")
    note = art.get("repealed_note") or ""
    # 整部法有效、但本条某款已被后法废止。这是最容易被忽略的一层，
    # 也是「版本感知」真正的难点：只说「现行有效」会把已死的款当成活的用。
    if note:
        # 措辞要分清两种情形，否则答辩时会被问倒：
        #   整部法已废止（合同法）→ 本条「已废止」，note 说明被哪条承继
        #   整部法有效、某款已废止（解释一第32条）→ 本条「部分失效」
        label = "已废止" if status == "已废止" else "部分失效"
        evs: list[Evidence] = [_to_ev(art)]
        # 从 note 里把「《某法》第X条」这种承继条文取回来一起给模型。
        # 少了这一步，「合同法第114条还有效吗」只能拿到旧条文本身，
        # 模型得再发一次检索才能碰到民法典 585 条——实测它会漏掉那一步，
        # 于是答案里没有新条号，召回判成漏召。
        # 必须取**最后一个**匹配：note 一般先写废止依据（民法典第1260条），
        # 再写承继条文（民法典第585条）。取第一个会把「废止清单」当成替代条文。
        found = re.findall(r"《([^》]{2,40})》第([零一二三四五六七八九十百千\d]+)条", note)
        repl = ctx.index.get_article(*found[-1]) if found else None
        if repl and repl["id"] != art["id"]:
            evs.append(_to_ev(repl, 1.0, "citation_expand"))
        return ToolResult(
            summary=f"《{art['law_short']}》{art['article_no_cn']} {label}：{note}",
            data={"status": label, "superseded_by": art.get("superseded_by"),
                  "repealed_note": note,
                  "replacement_id": repl["id"] if repl else None},
            evidence=evs)
    if status in ("现行有效", "部分失效"):
        return ToolResult(summary=f"《{art['law_short']}》{art['article_no_cn']} 现行有效，"
                                  f"自 {art.get('effective_date','未知')} 起施行",
                          data={"status": status, "superseded_by": None})
    evs: list[Evidence] = [_to_ev(art)]
    replaced = None
    if sup:
        m = re.search(r"(.+?)第([零一二三四五六七八九十百千\d]+)条", sup)
        if m:
            replaced = ctx.index.get_article(m.group(1), m.group(2))
            if replaced:
                evs.append(_to_ev(replaced, 1.0, "citation_expand"))
    summary = (f"《{art['law_short']}》{art['article_no_cn']} 状态为「{status}」"
               + (f"，已由{sup}替代" if sup else "，且未记录替代条文"))
    return ToolResult(summary=summary,
                      data={"status": status, "superseded_by": sup,
                            "replacement_id": replaced["id"] if replaced else None},
                      evidence=evs)


@tool("expand_citation",
      "沿条款交叉引用扩展一跳。当条文正文里出现「依照本法第X条」「按照第X条」这类"
      "引用时调用，否则你无法知道被引条文写了什么，只能凭印象编造。",
      IdArgs)
def expand_citation(a: IdArgs, ctx: ToolContext) -> ToolResult:
    arts = ctx.index.expand(a.article_id)
    if not arts:
        return ToolResult(summary="该条文没有可解析的同法交叉引用",
                          data={"from": a.article_id, "count": 0})
    evs = [_to_ev(x, 0.9, "citation_expand") for x in arts]
    names = "、".join(f"{e.article_no_cn}" for e in evs)
    return ToolResult(summary=f"扩展取回 {len(evs)} 条被引条文：{names}",
                      data={"from": a.article_id, "ids": [e.article_id for e in evs]},
                      evidence=evs)


def _parse_ym(s: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{4})\D*?(\d{1,2})", str(s))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    return y, mo


def _months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return (end[0] - start[0]) * 12 + (end[1] - start[1])


def _compensation_years(months: int) -> tuple[float, str]:
    """把工作月数换成补偿月数。

    规则来自第四十七条：每满一年一个月；六个月以上不满一年按一年；
    不满六个月支付半个月。这三个分支必须写死，不能让模型算。
    """
    if months <= 0:
        return 0.0, "在职时间为零或数据有误"
    years, rest = divmod(months, 12)
    if rest == 0:
        return float(years), f"满 {years} 年，按 {years} 个月计"
    if rest >= 6:
        return float(years + 1), f"{years} 年 {rest} 个月，六个月以上按一年计，共 {years + 1} 个月"
    return years + 0.5, f"{years} 年 {rest} 个月，不满六个月按半个月计，共 {years + 0.5} 个月"


@tool("calculate_compensation",
      "计算经济补偿金或违法解除赔偿金。凡是涉及具体金额的问题，必须调用本工具，"
      "不要自己算术——「六个月以上不满一年按一年算」这类细则模型极易算错。",
      CalcArgs)
def calculate_compensation(a: CalcArgs, ctx: ToolContext) -> ToolResult:
    s, e = _parse_ym(a.start_date), _parse_ym(a.end_date)
    if not s or not e:
        return ToolResult(error="日期格式无法解析，请用 YYYY-MM 或 YYYY-MM-DD",
                          summary="日期解析失败")
    months = _months_between(s, e)
    if months <= 0:
        return ToolResult(error="离职时间早于入职时间，请核对", summary="日期区间不合法")

    base_n, detail = _compensation_years(months)
    reason = a.leave_reason or "公司辞退"

    # "被公司辞退"这个事实本身不决定金额——决定金额的是"解除是否合法"。
    # 合法性未知时列出两种定性，这比硬给一个数字负责，也是真实咨询的做法。
    if any(k in reason for k in ("违法", "非法")):
        scenarios = [("属违法解除，按二倍支付赔偿金", 2.0, "第八十七条")]
    elif any(k in reason for k in ("协商", "被迫")):
        scenarios = [("依法解除或被迫离职，支付经济补偿", 1.0, "第四十六条、第四十七条")]
    elif "主动辞职" in reason:
        scenarios = [("主动辞职，一般无经济补偿", 0.0, "—")]
    else:
        scenarios = [
            ("若属违法解除（公司无法证明解除合法）", 2.0, "第八十七条"),
            ("若属合法解除（协商一致或法定情形）", 1.0, "第四十六条、第四十七条"),
        ]

    lines = [f"在职 {months} 个月（约 {months / 12:.1f} 年）",
             f"补偿月数：{detail}"]

    # ---- 三倍封顶与十二年线（第四十七条第二款）----
    # 必须看清这条限定的是**高收入者**：只有「月工资高于当地上年度职工月
    # 平均工资三倍」的人，基数才按三倍、年限才封十二年。普通劳动者的年限
    # 不封顶。把十二年无差别套上去是极常见的误读，一错就少算好几年补偿。
    #
    # 「上年度」是相对**离职年度**的上一年，不是相对今天。2026 年 11 月离职
    # 的人用 2025 年度的数；用成 2026 年度会高估封顶线。方向搞反的后果是
    # 高估，而高估比低估更危险——用户会照着这个数去谈，谈崩了还不知道为什么。
    pay_year = e[0] - 1
    lk = region_params.resolve(a.region, pay_year)
    param = lk.param

    if param is None:
        # 查不到社平工资 → 三倍封顶是否适用无法判断，**只给公式不给金额**。
        # 这与「缺依据不作答」是同一个口径：给个没封顶的数，高薪用户会拿着
        # 远超法院支持额的数字去谈，比不给数字更糟。
        wage_base: int | None = None
        n_list, years_capped = base_n, False
    else:
        high = a.monthly_wage > param.cap_base
        wage_base = param.cap_base if high else a.monthly_wage
        years_capped = high and base_n > 12.0
        n_list = min(base_n, 12.0) if high else base_n

    if param is not None and wage_base != a.monthly_wage:
        lines.append(f"月工资 {a.monthly_wage:,} 元高于{param.region}三倍封顶线 "
                     f"{param.cap_base:,} 元，基数按 {param.cap_base:,} 元计")
    if years_capped:
        lines.append(f"年限超过十二年，按上限 12 个月计（原本 {base_n:g} 个月）")

    amounts: list[dict[str, Any]] = []
    for label, mult, basis in scenarios:
        n = n_list * mult
        if n == 0:
            lines.append(f"{label}：无需支付（依据{basis}）")
            amounts.append({"label": label, "months": 0.0, "amount": 0,
                            "basis": basis, "wage_base": wage_base, "capped": None})
            continue
        if wage_base is None:
            # 措辞不能写成「即为 X 元 × N」——那读起来像已经算定了。
            # 缺参数时唯一能确定的是月数，基数有「是否超三倍」两种可能，
            # 必须把两个分支都说出来，否则用户会照着单边数字去谈。
            lines.append(f"{label}：按 {n:g} 个月工资计"
                         f"（若月工资未超当地三倍，即 {a.monthly_wage:,} 元 × {n:g}；"
                         f"若超过，基数按三倍、年限最高十二年）")
            amounts.append({"label": label, "months": n, "amount": None,
                            "basis": basis, "wage_base": None, "capped": None,
                            "formula": f"{a.monthly_wage:,} 元 × {n:g} 个月"})
            continue
        amt = int(round(wage_base * n))
        lines.append(f"{label}：{wage_base:,} 元 × {n:g} = {amt:,} 元（依据{basis}）")
        amounts.append({"label": label, "months": n, "amount": amt, "basis": basis,
                        "wage_base": wage_base,
                        "capped": wage_base != a.monthly_wage})

    caveats = []
    if param is None:
        caveats.append(lk.reason)
        caveats.append("所以这里只给计算方法、不给金额：查不到当地社平工资，"
                       "就判断不了三倍封顶适不适用，硬报一个数字可能远超法院支持的范围")
    elif wage_base != a.monthly_wage:
        caveats.append(param.describe())
        caveats.append("赔偿金（二倍）是否同样受十二年线约束，各地裁判口径不完全一致，"
                       "主流做法是随经济补偿一并封顶")
    else:
        caveats.append(f"月工资未超过{param.region}三倍封顶线"
                       f"（{param.cap_base:,} 元），不适用封顶，年限也不受十二年限制")
        if not param.official:
            caveats.append(param.describe())
    caveats.append("计算基数是离职前十二个月平均工资，不是劳动合同上写的工资")

    # 用了哪条规则，就把那条规则交出去。原来这里不返回 evidence，于是
    # 「能拿多少赔偿」这类题的主依据只能退化成检索首条——实测摆出来的是
    # 《解释（一）》第四十七条（通知工会），而答案讲的是二倍赔偿金，两边
    # 对不上。期限工具早就这么做（按 id 从语料库取、标 precise），补偿
    # 计算一直漏了，属于两处实现不一致。
    #
    # 47 条排在前面：它是年限折算和三倍封顶的计算规则，这次答案的数字
    # 就是照它算出来的。87 条只在有二倍方案时才给。
    evs: list[Evidence] = []
    if ctx.index is not None:
        want = ["劳动合同法_47"]
        if any(mult >= 2.0 for _, mult, _ in scenarios):
            want.append("劳动合同法_87")
        for aid in want:
            art = ctx.index.by_id.get(aid)
            if art is not None:
                evs.append(_to_ev(art, 1.0, "precise"))

    return ToolResult(
        summary="；".join(lines) + "。注意：" + "；".join(caveats),
        evidence=evs,
        data={"months": months, "base_months": base_n,
              "scenarios": amounts, "caveats": caveats,
              "wage_base": wage_base,
              "capped": None if param is None else wage_base != a.monthly_wage,
              "years_capped": years_capped,
              "region_param": ({"region": param.region, "year": param.year,
                                "avg_monthly": param.avg_monthly,
                                "cap_base": param.cap_base, "basis": param.basis,
                                "source_doc": param.source_doc,
                                "source_url": param.source_url,
                                "note": param.note} if param else None),
              "region_reason": lk.reason},
    )


@tool("query_region_params",
      "查某地某年度的职工月平均工资与三倍封顶线。**只在用户直接问「当地社平工资"
      "是多少」「三倍封顶线多少」时使用**。要算经济补偿金请直接调 "
      "calculate_compensation——它内部自己查这张表，不需要你先查再传，"
      "重复查一遍只是多花一轮。本库只收录北京、上海、杭州三地，"
      "其余城市会明确告诉你没收录，**此时不要自己编一个数字**。",
      RegionArgs)
def query_region_params(a: RegionArgs, ctx: ToolContext) -> ToolResult:
    lk = region_params.resolve(a.region, a.year)
    if not lk:
        return ToolResult(error=lk.reason, summary=lk.reason,
                          data={"found": False, "known": list(lk.known)})
    p = lk.param
    extra = "" if p.official else "（按社保缴费基数上限反推，不是官方公布的社平工资）"
    return ToolResult(
        summary=(f"{p.region} {p.year} 年度职工月平均工资 {p.avg_monthly:,} 元，"
                 f"三倍封顶线 {p.cap_base:,} 元{extra}"),
        data={"found": True, "region": p.region, "year": p.year,
              "avg_monthly": p.avg_monthly, "cap_base": p.cap_base,
              "basis": p.basis, "source_doc": p.source_doc,
              "source_url": p.source_url, "note": p.note},
    )


@tool("calculate_deadline",
      "计算法定期限：仲裁时效、起诉期限、申请撤销裁决的期限。"
      "问题里出现「多久之内」「过没过期」「还来得及吗」「最晚什么时候」"
      "「超过一年还能不能告」时**必须调用本工具**，不要自己推算日期。"
      "本工具会按民法典期间规则算出具体截止日与剩余天数，并指明这期限"
      "从哪一天起算。工伤认定属于本库未收录的法律，调用后会把原因告诉你，"
      "此时不要自己编一个期限。",
      DeadlineArgs)
def calculate_deadline(a: DeadlineArgs, ctx: ToolContext) -> ToolResult:
    as_of_txt = (a.as_of or "").strip()
    as_of = dl_mod.parse_date(as_of_txt) if as_of_txt else None
    if as_of_txt and as_of is None:
        return ToolResult(error=f"基准日无法解析：{as_of_txt!r}，请用 YYYY-MM-DD",
                          summary="基准日解析失败")

    res = dl_mod.compute(a.dispute, a.onset, a.relation_ended, a.end_date,
                         as_of=as_of)

    # 条文一律从语料库按 id 取，取不到就不给。时效规则的条号如果凭记忆写，
    # 就是一处没有出处的引用——这个项目一直在防这个。
    #
    # source 分两档，这不是细节：`articles` 的第一条是**时效规则本身**
    # （27/48/49/50 条），它是结论的直接依据，标 precise 让它排到最前；
    # 后面那四条民法典期间计算条文只是「这个日期怎么算出来的」的凭证，
    # 标 direct。早先全标 precise，结果民法典第二百条抢了主依据的位置，
    # 答案变成「依据：民法典第二百条」——用户问多久过期，你给他讲期间
    # 从次日起算，答非所问。
    rule_article = (res.get("articles") or [""])[0]
    evs: list[Evidence] = []
    for aid in res.get("articles") or []:
        art = ctx.index.by_id.get(aid)
        if art:
            evs.append(_to_ev(art, 0.95, "precise" if aid == rule_article else "direct"))

    if not res.get("ok"):
        # 算不出来不等于没问过：把「缺哪部法」原样交回，模型才能照实说。
        return ToolResult(error=res.get("error") or "无法计算",
                          summary=res.get("hint") or res.get("error") or "无法计算",
                          data=res, evidence=evs)

    c = res.get("conservative") or {}
    if res.get("unlimited") is True:
        summary = (f"{res['label']}：劳动关系存续期间**不受一年仲裁时效限制**"
                   f"（调解仲裁法第二十七条第四款），此时没有截止日；"
                   f"但离职后须自终止之日起一年内提出")
    elif res.get("conditional"):
        summary = (f"{res['label']}：期限取决于劳动关系是否已结束，"
                   f"还在职不受一年限制、已离职从离职之日起一年，需先确认")
    elif c:
        summary = (f"{res['label']}：自{c['onset_label']}（{c['onset']}）起算，"
                   f"截止 **{c['deadline']}**，剩余 {c['left_days']} 天"
                   f"（{c['level_text']}）")
    else:
        summary = res["label"]

    return ToolResult(summary=summary, data=res, evidence=evs)


class DocArgs(BaseModel):
    doc_type: Literal["arbitration"] = Field(
        default="arbitration",
        description="要生成的文书类型。目前只支持 arbitration"
                    "（劳动人事争议仲裁申请书）")


@tool("draft_document",
      "生成劳动人事争议仲裁申请书的骨架。**只在用户明确要求「写一份/生成/起草"
      "仲裁申请书」「帮我把材料准备出来」「给我个模板」时调用**——普通咨询、"
      "问流程、问要准备什么，都不要调它。文书里的请求事项和赔偿金额一律留空："
      "那两处决定仲裁委审什么，必须当事人自己填。",
      DocArgs)
def draft_document(a: DocArgs, ctx: ToolContext) -> ToolResult:
    state = ctx.state
    if state is None:
        return ToolResult(error="没有会话上下文，取不到案情要素",
                          summary="无法生成：缺少上下文")

    # 案情从槽位取，**不让模型现填**：它已经从对话里抽过一遍了，
    # 再让它复述一遍等于给它一次「顺手编一个日期」的机会。
    doc = docgen.arbitration(state.slots.known(), profile=ctx.profile or {})
    body = doc["text"]
    missing = doc.get("missing") or []
    miss_p = doc.get("missing_profile") or []

    if doc.get("ready"):
        summary = (f"申请书已生成，共 {len(body)} 字，案情要素和个人信息都齐了。"
                   f"请求事项和金额按设计留空，需要当事人自己填。")
    else:
        parts: list[str] = []
        if missing:
            parts.append("案情要素缺：" + "、".join(
                SLOT_LABELS.get(k, k) for k in missing))
        if miss_p:
            parts.append("个人信息缺：" + "、".join(
                docgen.PROFILE_LABELS.get(k, k) for k in miss_p))
        summary = (f"申请书骨架已生成，共 {len(body)} 字，但还不能直接交——"
                   + "；".join(parts))
        if miss_p:
            # 个人信息不在对话里补，得去档案页填——用户不知道这一点，
            # 会以为是模型没问他。
            summary += "。个人信息请到「个人档案」里填，填完再生成一次就齐了"

    return ToolResult(
        summary=summary,
        data={"doc_type": a.doc_type, "text": body,
              # 存中文标签而不是字段名：生成侧两个 brief 都要直接拼进答案，
              # 让每个调用方各自映射一遍，迟早有一处漏掉露出 start_date。
              "missing": [SLOT_LABELS.get(k, k) for k in missing],
              "missing_profile": [docgen.PROFILE_LABELS.get(k, k) for k in miss_p],
              "ready": bool(doc.get("ready")),
              "warnings": doc.get("warnings") or [],
              "checklist": doc.get("checklist") or []})


@tool("ask_user",
      "当关键案情要素缺失、且该要素会显著改变结论时，向用户追问。"
      "一次只问一个问题，问最能改变结论的那个。不要为了凑信息而追问。",
      AskArgs)
def ask_user(a: AskArgs, ctx: ToolContext) -> ToolResult:
    return ToolResult(summary=f"向用户追问：{a.question}",
                      need_user_input=True, question=a.question)
