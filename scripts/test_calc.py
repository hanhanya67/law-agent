# -*- coding: utf-8 -*-
"""经济补偿计算的一分钱级测试，重点盯三倍封顶与十二年线。

为什么单独一个脚本：这两条规则最容易错，而且错得不显眼。

  - 十二年线只约束**高收入者**（月工资高于当地社平三倍）。把它无差别套在
    普通劳动者身上，每多干一年就少算一个月工资，用户完全看不出来——数字
    看着挺合理，只是偏小。这类错没有人会来报 bug，只会悄悄少赔钱。
  - 缺当地社平工资时若照给金额，高薪用户拿到的是一个远超法院支持范围的
    数字，拿去谈、谈崩、再回头怪工具。所以「查不到就不出数字」这条必须
    有断言钉住，否则以后有人为了「体验流畅」把它改回去。

跑法：
    ./.venv/Scripts/python.exe scripts/test_calc.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import region_params                                    # noqa: E402
from app.tools import (CalcArgs, RegionArgs, ToolContext,         # noqa: E402
                       calculate_compensation, query_region_params)

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


def calc(region: str, wage: int, s: str, e: str,
         reason: str = "协商解除") -> dict:
    """默认走「协商解除」：只有一条方案、倍数 1.0，断言不用除二。

    要测二倍赔偿金就显式传 reason="违法辞退"——那条方案倍数是 2.0，
    月数会翻倍，混在同一个 helper 里会让下面一半断言莫名其妙地失败。
    """
    a = CalcArgs(monthly_wage=wage, start_date=s, end_date=e,
                 leave_reason=reason, region=region)
    res = calculate_compensation(a, ToolContext(index=None))
    return res.data or {}


def one(d: dict) -> dict:
    """取唯一那条方案。测试统一用「违法辞退」，只有一条，避免取错分支。"""
    sc = d.get("scenarios") or []
    return sc[0] if sc else {}


def main() -> int:
    print("\n== 参数表本身 ==")
    bj = region_params.resolve("北京", 2025).param
    sh = region_params.resolve("上海", 2025).param
    hz = region_params.resolve("杭州", 2025).param
    ok("北京 2025 年度社平工资 12116 元", bj and bj.avg_monthly == 12116,
       str(bj and bj.avg_monthly))
    ok("北京三倍封顶线 = 36348 元", bj and bj.cap_base == 36348, str(bj and bj.cap_base))
    ok("上海 2025 年度社平工资 12577 元", sh and sh.avg_monthly == 12577,
       str(sh and sh.avg_monthly))
    # 与官方公告的缴费基数上限互证：12577×3 = 37731，公告写的也是 37731。
    # 两个独立口径能对上，这个数才敢直接用在答案里。
    ok("上海封顶线与官方缴费上限 37731 一致", sh and sh.cap_base == 37731,
       str(sh and sh.cap_base))
    ok("杭州封顶线与官方缴费上限 25611 一致", hz and hz.cap_base == 25611,
       str(hz and hz.cap_base))
    ok("杭州标记为推算值（basis=derived）", hz and hz.basis == "derived",
       str(hz and hz.basis))
    ok("北京上海标记为官方公布（basis=official）",
       bj and sh and bj.basis == "official" and sh.basis == "official")

    print("\n== 地名写法归一 ==")
    for raw in ("北京", "北京市", " 北京 "):
        ok(f"「{raw}」能查到北京", bool(region_params.resolve(raw, 2025).param), raw)
    ok("「浙江杭州」能查到杭州（省名写在前面）",
       bool(region_params.resolve("浙江杭州", 2025).param))
    ok("「上海市」能查到上海", bool(region_params.resolve("上海市", 2025).param))
    ok("「广州」查不到（本库未收录）",
       region_params.resolve("广州", 2025).param is None)
    ok("未收录时把已收录城市列出来",
       "北京" in region_params.resolve("广州", 2025).reason)
    ok("空地名单独给出「没说城市」的原因",
       "没有说在哪个城市" in region_params.resolve("", 2025).reason)

    print("\n== 上年度方向：相对离职年度，不是相对今天 ==")
    # 2026 年离职 → 用 2025 年度。这条拦住「用今天年份算」的写法。
    ok("2026 年 10 月离职命中 2025 年度",
       bool(region_params.resolve("北京", 2025).param))
    # 2025 年离职 → 用 2024 年度。这一条是补数据（v112）之后才成立的：
    # 只收 2025 年度时，「去年被辞退」一律算不出钱，等于一半提问都没数字。
    ok("2025 年 6 月离职命中 2024 年度",
       bool(region_params.resolve("北京", 2024).param))
    ok("两个年度的数字确实不同（不是复制粘贴）",
       region_params.resolve("北京", 2024).param.avg_monthly
       != region_params.resolve("北京", 2025).param.avg_monthly)
    # 再往前就没有了——「查不到就说查不到」这条不能被 2024 的补录带偏。
    ok("2024 年 6 月离职要 2023 年度，本库没有 → 查不到",
       region_params.resolve("北京", 2023).param is None)
    ok("查不到年度时说清差在哪一年",
       "2023" in region_params.resolve("北京", 2023).reason)

    print("\n== 2024 年度参数：每一条都有官方口径互证 ==")
    bj24 = region_params.resolve("北京", 2024).param
    sh24 = region_params.resolve("上海", 2024).param
    hz24 = region_params.resolve("杭州", 2024).param
    ok("北京 2024 年度 11937 元", bj24 and bj24.avg_monthly == 11937,
       str(bj24 and bj24.avg_monthly))
    # 143244 是官方表格给的年值，11937×12=143244，能对上说明不是抄错的
    ok("北京 2024 年值 143244 与月值自洽", bj24 and bj24.avg_monthly * 12 == 143244,
       str(bj24 and bj24.avg_monthly * 12))
    ok("北京 2024 三倍 35811（与大兴区通告「3 倍为 35811」一致）",
       bj24 and bj24.cap_base == 35811, str(bj24 and bj24.cap_base))
    ok("上海 2024 年度 12434 元", sh24 and sh24.avg_monthly == 12434,
       str(sh24 and sh24.avg_monthly))
    ok("上海 2024 三倍 37302（与 2025-07 缴费上限一致）",
       sh24 and sh24.cap_base == 37302, str(sh24 and sh24.cap_base))
    ok("杭州 2024 年度 8433 元（年 101194 折算）",
       hz24 and hz24.avg_monthly == 8433, str(hz24 and hz24.avg_monthly))
    ok("杭州 2024 三倍 25299（与浙江 2025 缴费上限一致）",
       hz24 and hz24.cap_base == 25299, str(hz24 and hz24.cap_base))
    # 2024 年的杭州是官方年值折算（official），2025 年反而是反推（derived）——
    # 这个方向容易想当然地写反，钉住它。
    ok("杭州 2024 标 official、2025 标 derived",
       hz24 and hz24.official
       and not region_params.resolve("杭州", 2025).param.official,
       f"{hz24 and hz24.basis} / "
       f"{region_params.resolve('杭州', 2025).param.basis}")
    # 省名不能替市名兜底：「宁波」若命中杭州，三倍封顶按杭州算是错的。
    ok("「宁波」不会误命中杭州",
       region_params.resolve("宁波", 2024).param is None)
    ok("「浙江杭州」仍能命中杭州",
       bool(region_params.resolve("浙江杭州", 2024).param))

    print("\n== 三倍封顶：高收入者 ==")
    d = calc("北京", 50000, "2016-01", "2026-10")      # 129 个月 → 11 个月
    s0 = one(d)
    ok("基数被压到 36348", d.get("wage_base") == 36348, str(d.get("wage_base")))
    ok("标记为已封顶", d.get("capped") is True)
    ok("年限未超十二年时不触发年限封顶", d.get("years_capped") is False)
    ok("金额 = 36348 × 11", s0.get("amount") == 36348 * 11, str(s0.get("amount")))
    ok("金额不等于按 5 万算的数（证明真的封顶了）",
       s0.get("amount") != 50000 * 11)

    print("\n== 十二年线：只对高收入者生效 ==")
    d_hi = calc("北京", 50000, "2010-01", "2026-10")   # 201 个月 → 17 个月
    ok("高薪 17 年 → 年限压到 12", one(d_hi).get("months") == 12.0,
       str(one(d_hi).get("months")))
    ok("高薪 17 年 → 金额 = 36348 × 12",
       one(d_hi).get("amount") == 36348 * 12, str(one(d_hi).get("amount")))
    ok("标记年限被压缩", d_hi.get("years_capped") is True)

    d_lo = calc("北京", 5000, "2010-01", "2026-10")    # 同样 201 个月
    ok("普通工资 17 年 → 仍是 17 个月（不封顶）",
       one(d_lo).get("months") == 17.0, str(one(d_lo).get("months")))
    ok("普通工资 17 年 → 金额 = 5000 × 17",
       one(d_lo).get("amount") == 5000 * 17, str(one(d_lo).get("amount")))
    ok("普通工资不标封顶", d_lo.get("capped") is False)
    ok("普通工资的说明里点明「年限不受十二年限制」",
       any("不受十二年限制" in c for c in d_lo.get("caveats") or []))

    print("\n== 十二年线的边界 ==")
    d12 = calc("北京", 50000, "2014-10", "2026-10")    # 正好 144 个月 = 12 年
    ok("正好 12 年不触发年限压缩", d12.get("years_capped") is False)
    ok("正好 12 年 → 12 个月", one(d12).get("months") == 12.0)
    d13 = calc("北京", 50000, "2014-09", "2026-10")    # 145 个月 → 12 年 1 个月
    ok("12 年 1 个月触发年限压缩", d13.get("years_capped") is True)
    ok("12 年 1 个月 → 仍是 12 个月", one(d13).get("months") == 12.0,
       str(one(d13).get("months")))

    print("\n== 二倍赔偿金同样封顶 ==")
    # 22 个月 = 11（补偿）× 2。基数已封顶，二倍在封顶后的基数上翻倍。
    d_dbl = calc("北京", 50000, "2016-01", "2026-10", reason="违法辞退")
    ok("高薪 11 个月二倍 → 22 个月", one(d_dbl).get("months") == 22.0,
       str(one(d_dbl).get("months")))
    ok("高薪 11 个月二倍 → 金额 = 36348 × 22",
       one(d_dbl).get("amount") == 36348 * 22, str(one(d_dbl).get("amount")))
    d_hi_dbl = calc("北京", 50000, "2010-01", "2026-10", reason="违法辞退")
    ok("高薪 17 年二倍 → 24 个月（12×2）",
       one(d_hi_dbl).get("months") == 24.0, str(one(d_hi_dbl).get("months")))
    ok("「二倍是否也受十二年约束」写进了说明（有争议要说出来）",
       any("裁判口径不完全一致" in c for c in d_hi_dbl.get("caveats") or []))

    print("\n== 缺参数：只给公式，不给金额 ==")
    for label, reg, s, e in [
        ("没说城市", "", "2010-01", "2026-10"),
        ("城市未收录", "南昌", "2010-01", "2026-10"),
        # 2024-06 离职要 2023 年度——2024 年度补录进来之后，这个用例必须往前
        # 挪一年，否则它测的其实是「能算出来」，断言反而成了假的。
        ("年度对不上", "北京", "2019-01", "2024-06"),
    ]:
        dd = calc(reg, 50000, s, e)
        sc = one(dd)
        ok(f"{label}：amount 为 None（不出数字）", sc.get("amount") is None,
           str(sc.get("amount")))
        ok(f"{label}：月数照给（公式还在）", sc.get("months", 0) > 0)
        ok(f"{label}：把没算出来的原因写进 caveats",
           any("只给计算方法" in c for c in dd.get("caveats") or []))

    print("\n== 各地数字分别落到位 ==")
    dsh = calc("上海", 50000, "2010-01", "2026-10")
    ok("上海基数 37731", dsh.get("wage_base") == 37731, str(dsh.get("wage_base")))
    ok("上海金额 = 37731 × 12", one(dsh).get("amount") == 37731 * 12)
    dhz = calc("杭州", 50000, "2010-01", "2026-10")
    ok("杭州基数 25611", dhz.get("wage_base") == 25611, str(dhz.get("wage_base")))
    ok("杭州答案里注明这是反推值",
       any("反推" in c for c in dhz.get("caveats") or []))
    ok("北京答案里带出处（能追到数据）",
       any("2025 年度职工月平均工资" in c for c in
           calc("北京", 50000, "2016-01", "2026-10").get("caveats") or []))

    print("\n== 主动辞职：0 元，且不需要社平工资 ==")
    d0 = calc("", 50000, "2010-01", "2026-10", reason="主动辞职")
    ok("主动辞职金额为 0（不是 None）", one(d0).get("amount") == 0,
       str(one(d0).get("amount")))
    ok("主动辞职月数为 0", one(d0).get("months") == 0.0)

    print("\n== query_region_params 工具 ==")
    r = query_region_params(RegionArgs(region="北京", year=2025), ToolContext(index=None))
    ok("工具返回北京封顶线", (r.data or {}).get("cap_base") == 36348)
    ok("工具 summary 含月平均工资", "12,116" in (r.summary or ""))
    r2 = query_region_params(RegionArgs(region="杭州", year=2025), ToolContext(index=None))
    ok("工具对推算值给出提示", "反推" in (r2.summary or ""))
    r3 = query_region_params(RegionArgs(region="广州", year=2025), ToolContext(index=None))
    ok("未收录城市返回 error 而不是编数字",
       bool(r3.error) and (r3.data or {}).get("found") is False)
    r4 = query_region_params(RegionArgs(region="上海"), ToolContext(index=None))
    ok("不填年度取收录的最新年度", (r4.data or {}).get("year") == 2025)

    print("\n== 生成侧：金额必须递到模型眼前（_calc_brief）==")
    # 这一组钉的是一个「很贵」的漏：工具算出了钱，但生成 prompt 里没有位置放，
    # 模型于是只能照着法条重复公式，还建议用户自己去查社平工资。
    from app.llm import LLM, _calc_brief, needs_calc
    from app.schemas import AgentState, Budget, CaseSlots, Evidence, Step

    def mk_state(reg="北京", wage=50000, s="2010-01", n_search=0):
        st = AgentState(session_id="_t", question="月薪5万被辞退能拿多少赔偿")
        st.budget = Budget(steps_left=5)
        st.slots = CaseSlots(monthly_wage=wage, start_date=s, region=reg)
        st.scratchpad = [Step(index=i + 1, tool="search_statute") for i in range(n_search)]
        return st

    def add_calc(st, e="2026-10"):
        # 一律从 st.slots 取，别在这里写死工资——写死过一次，
        # 结果「普通工资」那几条测的其实是 5 万，断言全红还看不出为什么。
        a = CalcArgs(monthly_wage=st.slots.monthly_wage,
                     start_date=st.slots.start_date, end_date=e,
                     leave_reason="违法辞退", region=st.slots.region or "")
        res = calculate_compensation(a, ToolContext(index=None))
        st.scratchpad.append(Step(index=len(st.scratchpad) + 1,
                                  tool="calculate_compensation",
                                  data=res.data or {}, summary=res.summary or ""))
        return st

    b_hi = _calc_brief(add_calc(mk_state()))
    ok("brief 里有具体金额 872,352", "872,352" in b_hi, b_hi[:120])
    ok("brief 说明基数已封顶", "超过当地三倍封顶线" in b_hi)
    ok("brief 说明年限被压缩", "年限超过十二年" in b_hi)
    ok("brief 明确「直接报数、不要自己重算」", "不要自己重算" in b_hi)
    ok("brief 带数据出处", "北京市人力资源和社会保障局" in b_hi)

    b_lo = _calc_brief(add_calc(mk_state(wage=5000)))
    ok("普通工资的 brief 提醒别套十二年上限", "不要替他套十二年上限" in b_lo)

    b_none = _calc_brief(add_calc(mk_state(reg="南昌")))
    ok("缺参数的 brief 明令禁止给数字", "不要给出任何金额数字" in b_none)
    ok("缺参数的 brief 带上没查到的原因", "没有收录" in b_none)

    # v132：这段文案从「（本轮没有计算补偿金额）」改成了一条硬约束。原句太软，
    # 模型当没看见，照样自己乘出一个精确到元的数字（真 key 实测 872,352，
    # 而 calculate_compensation 一次都没调用）。所以断言也从「有没有说没算」
    # 改成「有没有把『一个数字都不许出现』说死」。
    b_no = _calc_brief(mk_state())
    ok("没算过时 brief 明令禁止出现任何金额", "一个具体数字都不许出现" in b_no)
    ok("没算过时 brief 说明补齐要素就能算", "补齐入职日期" in b_no)

    print("\n== 决策侧：检索空转时把计算拉回来（_calc_budget_guard）==")
    # 实测过模型连查 9 次、把预算吃光，计算工具一次没轮到，
    # 最后答「三倍社平 × 12 × 2」——规则全对，数字一个没有。
    g = LLM._calc_budget_guard(mk_state(n_search=6))
    ok("检索 6 次后强制调计算工具", g is not None and g.tool_calls
       and g.tool_calls[0].name == "calculate_compensation")
    ok("强制调用时把城市一起带上",
       g is not None and (g.tool_calls[0].args or {}).get("region") == "北京")
    ok("检索 2 次时不干预（给模型留空间）",
       LLM._calc_budget_guard(mk_state(n_search=2)) is None)
    st_done = add_calc(mk_state(n_search=6))
    ok("已经算过就不再重复指派", LLM._calc_budget_guard(st_done) is None)
    ok("槽位不全时不硬算",
       LLM._calc_budget_guard(mk_state(wage=None, n_search=6)) is None)
    st_odd = mk_state(n_search=6)
    st_odd.question = "公司不给我交社保怎么办"
    ok("不是金额题时不插手", LLM._calc_budget_guard(st_odd) is None)

    # v132 软闸：不等步数见底，只要「要素齐 + 已有条文 + 在问金额」就派给工具。
    # 为什么必须加：真 key 实测「月薪5万北京干16年被违法辞退能拿多少」，
    # 模型查了 3 条法条后 ask_user 追问日期，循环因「已挂起追问 + 已有证据」
    # 跳出，算钱一步永远轮不到，答案里的 872,352 元是模型自己乘的。
    st_soft = mk_state(n_search=3)
    st_soft.evidences = [Evidence(article_id="x", law_name="L", article_no="1",
                                  article_no_cn="第一条",
                                  content="c", status="现行有效")]
    gs = LLM._calc_budget_guard(st_soft)
    ok("软闸：要素齐 + 有证据 + 步数还剩很多也直接算",
       gs is not None and gs.tool_calls[0].name == "calculate_compensation",
       gs)
    st_soft_noev = mk_state(n_search=3)      # 没检索过任何条文
    ok("软闸：还没有条文时不抢在检索前算",
       LLM._calc_budget_guard(st_soft_noev) is None)
    ok("needs_calc 对已算过的返回 False",
       needs_calc(add_calc(mk_state())) is False)

    # 半程闸：缺入职日期算不出总额，但社平工资必须查出来。
    # 否则模型会写「当地社平工资目前还没有」——库里明明存着官方数据。
    st_half = mk_state(wage=50000, s=None, n_search=3)
    st_half.evidences = st_soft.evidences
    gh = LLM._calc_budget_guard(st_half)
    ok("半程闸：缺日期时改查当地社平工资",
       gh is not None and gh.tool_calls[0].name == "query_region_params",
       gh)
    st_half_noreg = mk_state(reg=None, s=None, n_search=3)
    st_half_noreg.evidences = st_soft.evidences
    ok("半程闸：不知道城市时不瞎查",
       LLM._calc_budget_guard(st_half_noreg) is None)

    print("\n== 口语数字抽取（_guess_slots）：年份不许被当成工资 ==")
    # 实测到的真 bug：在线跑「2024年3月入职，月薪12000，今年9月被辞退」时，
    # 规则兜底把 monthly_wage 抽成 **2024**（「2024年」的年份被 `(\d{3,6})` 抓到），
    # 后面一路算下去，补偿金少六倍，而且不报错、日志里也看不出异常。
    # 这类错没有人会来报 bug——用户拿到的是一个「看着合理、只是偏小」的数字。
    from app.llm import _guess_slots                            # noqa: E402
    from app.schemas import AgentState, CaseSlots               # noqa: E402

    def _slot_checks() -> None:
        gs = _guess_slots
        ok("月薪 12000 不被「2024年」的年份抢走",
           gs("2024年3月入职，月薪12000").get("monthly_wage") == 12000,
           str(gs("2024年3月入职，月薪12000")))
        ok("「拖欠工资3个月」不抽成月薪 3",
           gs("拖欠工资3个月，月薪8000").get("monthly_wage") == 8000,
           str(gs("拖欠工资3个月，月薪8000")))
        ok("「月薪15k」按 15000 记", gs("月薪15k").get("monthly_wage") == 15000,
           str(gs("月薪15k")))
        ok("「月薪1.2万」按 12000 记", gs("月薪1.2万").get("monthly_wage") == 12000,
           str(gs("月薪1.2万")))
        ok("「月工资不低于6000」也能抽到",
           gs("月工资不低于6000").get("monthly_wage") == 6000,
           str(gs("月工资不低于6000")))
        # 日期归属：原来是「第一个 4 位年份一律当入职时间」，
        # 「2025年9月被辞退」于是变成「2025-09 入职、没离职」，年限算成 0.5。
        _only_end = gs("2025年9月被公司辞退")
        ok("「被辞退」的年份归 end_date", _only_end.get("end_date") == "2025-09",
           str(_only_end))
        ok("且不冒充 start_date", "start_date" not in _only_end, str(_only_end))
        ok("「起/止」两个裸日期分别落到入职与离职",
           gs("2024年3月起工作，2025年9月止") == {"start_date": "2024-03",
                                            "end_date": "2025-09"},
           str(gs("2024年3月起工作，2025年9月止")))
        import time as _t
        ok("相对年份方向（今年 = 当前年）",
           gs("今年9月被辞退").get("end_date") == f"{_t.strftime('%Y')}-09",
           str(gs("今年9月被辞退")))
        ok("相对年份方向（去年 = 当前年 − 1）",
           gs("去年9月被辞退").get("end_date")
           == f"{int(_t.strftime('%Y')) - 1}-09",
           str(gs("去年9月被辞退")))

        # 联动断言：这才是「用户少拿六倍」的完整路径——抽错的数字一路传到金额。
        # 用「今年9月」（2026-09，对应 2025 年度参数）而不是「去年9月」：
        # 后者要 2024 年度社平工资，库里没有，会走「只给公式不出数字」那条路，
        # 金额恒为 None，这条断言就测不到工资基数了。
        patch = gs("我在北京，2024年3月入职，月薪12000，今年9月被公司辞退")
        st_g = AgentState(session_id="_g", question="x")
        st_g.slots = CaseSlots(**{k: v for k, v in patch.items()
                                  if not k.startswith("_")})
        d_g = calc("北京", st_g.slots.monthly_wage, st_g.slots.start_date,
                   st_g.slots.end_date, "违法辞退")
        ok("抽出的槽位能直接喂进计算（30 个月 → 3 个月，二倍 6 个月）",
           one(d_g).get("months") == 6.0, str(one(d_g).get("months")))
        ok("金额按 12000 计 = 72000（若抽成 2024 则只有 12144）",
           one(d_g).get("amount") == 72000, str(one(d_g).get("amount")))

    _slot_checks()

    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print(f"  ✗ {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
