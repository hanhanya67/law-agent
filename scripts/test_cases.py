# -*- coding: utf-8 -*-
"""判例参考 / 申诉决策卡回归（v117）。

这套测试最要紧的不是「能不能检索到案例」，而是守住三条边界：

  * **不给胜诉率**：输出序列化后不许出现任何百分比数字或「建议申诉」式
    独断结论。把 `outcome_distribution` 改成算百分比 → [3] 会红。
  * **按申请人立场过滤**：单位视角不许混进职工为申请人的案子。把
    `search_cases` 里那行 applicant 过滤删掉 → [2] 会红。
  * **金额恒留空 / 条号可核验**：成本表的金额行写死全角下划线，不拿
    `cases.BLANK` 当期望值（v116 抓到过这种假绿灯）；所有结构化条号
    动态回 statutes.jsonl 查，查不到就红。

篡改自检（改完记得改回来）：
  · 把 cases.BLANK 改成 "99999" → [4] 金额留空那条红；
  · 删掉 search_cases 的 applicant 过滤 → [2] 三条红；
  · 给 distribution 加一个 "win_rate" 百分比 → [3] 反伪精确那组红；
  · 往 data/cases.jsonl 塞一个库外条号 → [1] 条号回库核验红。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("DEEPSEEK_API_KEY", None)   # 强制回放：本套件不该花一分钱

from app import cases as C          # noqa: E402
from app import extras as X          # noqa: E402
from app import persona as P          # noqa: E402

TOTAL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}   {detail}")


def corpus() -> set[tuple[str, str]]:
    have: set[tuple[str, str]] = set()
    for line in (ROOT / "data" / "statutes.jsonl").open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if d.get("law_name") and d.get("article_no_cn"):
            have.add((d["law_name"], d["article_no_cn"]))
    return have


def cites_in(text: str) -> list[tuple[str, str]]:
    """把「《法》第二十九条、第四十三条」拆成可回库核验的 (法, 条) 对。"""
    out: list[tuple[str, str]] = []
    for law, chunk in re.findall(r"《([^》]+)》([^《]*)", str(text or "")):
        for art in re.findall(r"第[一二三四五六七八九十百零〇\d]+条", chunk):
            out.append((law, art))
    return out


def test_library() -> None:
    print("\n[1] 判例库：来源、结构、条号回库核验")
    rows = C.load_cases()
    ok("21 件典型案例全部载入", len(rows) == 21, str(len(rows)))
    ids = [r["case_id"] for r in rows]
    ok("case_id 无重复", len(set(ids)) == len(ids))
    ok("三个官方批次齐全",
       {r["doc_no"] for r in rows} == {
           "人社部函〔2021〕90号", "人社部函〔2023〕36号", "人社部函〔2025〕28号"},
       str({r["doc_no"] for r in rows}))
    ok("每条都有官方处理结果原文",
       all(len(r.get("outcome_quote") or "") >= 10 for r in rows))
    ok("每条都有可核对的来源链接",
       all(str(r.get("source_url") or "").startswith("https://") for r in rows))
    ok("每条都标注「摘要非全文」",
       all("非全文" in str(r.get("note") or "") for r in rows))
    ok("outcome 取值合法",
       {r["outcome"] for r in rows} <= {"supported", "partial", "dismissed", "settled"},
       str({r["outcome"] for r in rows}))
    ok("applicant 取值合法",
       {r["applicant"] for r in rows} <= {"employee", "employer"})
    ok("确有「部分支持」的真实案例（不是非赢即输）",
       sum(1 for r in rows if r["outcome"] == "partial") == 3)
    ok("确有单位为申请人被驳回的案例",
       any(r["case_id"] == "B4-05" and r["applicant"] == "employer"
           and r["outcome"] == "dismissed" for r in rows))

    have = corpus()
    pairs = C.local_case_articles()
    ok("判例库登记了结构化条号", len(pairs) >= 20, str(len(pairs)))
    bad = [p for p in pairs if p not in have]
    ok("判例库每一条结构化依据都能在本地法条库查到", not bad, str(bad[:3]))
    ok("没有把库外法律写进结构化条号",
       all(law.startswith("中华人民共和国")
           or law.startswith("最高人民法院") for law, _ in pairs))
    cites = [c for c in C.cited_articles() for c in cites_in(c)]
    ok("成本卡常量条号也能回库核验",
       cites and all(p in have for p in cites), str([p for p in cites if p not in have]))


def test_stance_filter() -> None:
    print("\n[2] 立场过滤：单位视角不许混进职工案件")
    rows = C.load_cases()
    emp = C.search_cases("竞业限制 违约金 工资", stance="employee", cases=rows)
    ok("默认按职工视角检索出案件", len(emp) > 0)
    ok("职工视角里没有单位为申请人的案子",
       all(c["applicant"] == "employee" for c in emp),
       str([c["case_id"] for c in emp]))
    ok("B4-05（单位申请被驳回）不出现在职工视角",
       all(c["case_id"] != "B4-05" for c in emp))

    er = C.search_cases("竞业限制 违约金", stance="employer", cases=rows)
    ok("单位视角只出单位为申请人的案子",
       er and all(c["applicant"] == "employer" for c in er),
       str([c["case_id"] for c in er]))
    ok("单位视角样本确实很少（不足以算倾向）", len(er) == 1, str(len(er)))

    d = C.build_decision("竞业限制违约金", stance="employer", cases=rows)
    ok("单位视角分布标 insufficient",
       d["similar"]["distribution"]["insufficient"] is True)
    ok("单位视角 applicant 记录为 employer",
       d["scope"]["applicant"] == "employer")
    ok("agent / explore 按保守的职工视角给参考",
       all(C.build_decision("加班费", stance=s, cases=rows)["scope"]["applicant"]
           == "employee" for s in ("agent", "explore", "乱写的立场")))

    a = [c["case_id"] for c in C.search_cases("拖欠工资 加班费", cases=rows)]
    b = [c["case_id"] for c in C.search_cases("拖欠工资 加班费", cases=rows)]
    ok("同一句提问检索顺序稳定", a == b, f"{a} / {b}")
    ok("来源标注明说未联网",
       C.source_policy()["label"] == "本地判例库，未联网")
    ok("默认没有启用外部检索适配器",
       C.source_policy()["web_enabled"] is False)


def test_distribution() -> None:
    print("\n[3] 结局分布：只数事实，不算概率")
    rows = C.load_cases()
    d = C.build_decision("公司拖欠工资、没签劳动合同、还想要加班费", cases=rows)
    dist = d["similar"]["distribution"]
    ok("三档计数之和等于样本数",
       dist["supported"] + dist["partial"] + dist["dismissed"] + dist["settled"]
       == dist["total"], str(dist))
    ok("样本数与实际返回案例数一致",
       dist["total"] == len(d["similar"]["items"]))
    ok("分布里没有 rate / probability / win 字段",
       not any(k in dist for k in ("rate", "win_rate", "probability", "success_rate")))
    ok("labels 用的是中文结局名",
       dist["labels"]["partial"] == "部分支持")

    blob = json.dumps(d, ensure_ascii=False)
    # v137：代理费区间（「按标的 10%–20%」）与诉讼费率（「1–10 万元部分按 2.5%」）
    # 是合法费率，不是胜率。判据从「一个百分比都不许有」收窄成
    # 「每个百分比都必须挂在费率语境上」——守的仍是「不给胜率」。
    _bad_pct = []
    for m in re.finditer(r"\d+(?:\.\d+)?\s*%", blob):
        ctx = blob[max(0, m.start() - 16):m.start()]
        if not any(k in ctx for k in ("标的", "费率", "部分按", "元起", "或按")):
            _bad_pct.append(ctx + m.group(0))
    ok("整份决策卡不含任何胜率类百分比", not _bad_pct, str(_bad_pct[:3]))
    ok("整份决策卡不出现「胜诉率 8」这类数字预测",
       not re.search(r"(胜诉率|成功率|赢面)[^\u4e00-\u9fff]{0,4}\d", blob))
    ok("不出现「建议申诉 / 不建议申诉」的独断结论",
       not re.search(r"(不?建议|应当|不该)(申诉|起诉|仲裁)", blob))
    ok("明确声明不提供胜诉率",
       "不提供胜诉率" in d["policy"]["no_win_rate"])
    ok("结论卡给的是可执行的结论，不是条件式倾向",
       d["cost_benefit"]["inclination"]["label"]
       in ("建议上诉", "不建议上诉", "建议先补材料", "建议先核时效"),
       d["cost_benefit"]["inclination"]["label"])
    ok("不再输出「由本人决定」这类免责垫话",
       "no_advice_verdict" not in d["policy"]
       and "no_verdict" not in d["cost_benefit"])
    ok("每件案例都带官方处理结果原文",
       all(len(c["outcome_quote"]) >= 10 for c in d["similar"]["items"]))
    ok("每件案例都带来源链接",
       all(c["source_url"].startswith("https://") for c in d["similar"]["items"]))
    ok("每件案例带中文结局标签",
       all(c["outcome_label"] in ("支持", "部分支持", "驳回", "调解结案")
           for c in d["similar"]["items"]))
    ok("小样本提示确实会出现",
       C.outcome_distribution(rows[:2])["insufficient"] is True
       and "不足" in C.outcome_distribution(rows[:2])["note"])


def test_cost_benefit() -> None:
    print("\n[4] 成本收益卡：时间、费用、可得并排，金额留空")
    calc = {"scenarios": [{"label": "经济补偿", "amount": 36000}]}
    d = C.build_decision("老板拖了两个月工资，时效快到了，我要不要申请仲裁",
                         slots={"monthly_wage": 12000}, calc_data=calc)
    cb = d["cost_benefit"]
    labels = [r["label"] for r in cb["rows"]]
    ok("六行成本收益齐全", len(labels) == 6, str(labels))
    ok("时间行写清 5 日 / 45 日 / 15 日",
       all(k in " ".join(r["value"] for r in cb["rows"] if "耗时" in r["label"]
                         or "诉讼" in r["label"])
           for k in ("5 日", "45 日", "15 日")))
    ok("写明仲裁不收费",
       any("不收费" in r["value"] for r in cb["rows"]))
    # v137 政策变更：从「不许给金额区间」改成「给区间但必须标成行情」。
    # 判据跟着政策走，但守的东西没变——不许出现**没有出处**的数字。
    ok("代理费用给区间且标明是行情",
       any(re.search(r"\d{3,}.*\d{3,}", r["value"]) and "行情" in r["value"]
           for r in cb["rows"]))
    ok("成本行不把行情写成确定值",
       not any("大约" in r["value"] and "区间" not in r["value"]
               for r in cb["rows"]))
    money = next(r for r in cb["rows"] if r["label"] == "可得金额")
    # 期望值写死，不用 cases.BLANK —— 拿被测常量当期望值会一起变，是假绿灯。
    ok("金额行固定为全角下划线", "＿＿＿＿" in money["value"], money["value"])
    has_num = bool(re.search(r"\d{3,}", money["value"]))
    ok("金额行没有被核算数字填进去", not has_num, money["value"])
    ok("核算参考另放、且标明不写入文书",
       cb["estimate"] == {"amount": 36000.0,
                          "label": "核算参考（不写入任何文书）"}, str(cb["estimate"]))

    have = corpus()
    # 条号不只在 rows 的 cite 里，条件式提示也会带「（法第X条）」。
    # 只扫 rows 会漏掉时效那条依据，等于给「引用了库外条号」留后门。
    texts = [r["cite"] for r in cb["rows"]] + list(cb["conditionals"])
    pairs = [p for t in texts for p in cites_in(t)]
    ok("成本卡与条件式共引用 6 条可解析依据", len(pairs) == 6, str(pairs))
    ok("成本卡每条依据都能回库核验",
       all(p in have for p in pairs), str([p for p in pairs if p not in have]))
    ok("提示改成短句直陈，不再用「若…则…」长句",
       all(len(str(s)) <= 80 for s in cb["conditionals"]), str(cb["conditionals"]))
    ok("条件式提示不超过 4 条", 0 < len(cb["conditionals"]) <= 4)
    ok("不再有「不替你决定」垫话", "no_verdict" not in cb)
    ok("免责收成一句",
       cb["disclaimer"] == "仅供参考，不构成法律意见。", cb["disclaimer"])

    cb2 = C.build_decision("时效快到了，还能申请吗", slots={})["cost_benefit"]
    ok("问到时效时给出专门提示",
       any("时效" in s for s in cb2["conditionals"]))
    ok("没核算金额时提示先补全信息",
       any("先补齐再算金额" in s for s in cb2["conditionals"]), str(cb2["conditionals"]))


def test_entrypoints() -> None:
    print("\n[5] 统一入口与零新增工具")
    q = "没签劳动合同，还拖欠工资"
    direct = C.build_decision(q, stance="employee")
    via_extras = X.from_case(q)["decision"]
    ok("extras.from_case 与 cases.build_decision 结果一致",
       direct == via_extras)
    ok("build_extras 也带决策卡和画像",
       set(X.build_extras(_fake_state(q))) >= {"decision", "persona"})

    from app import tools as T
    names = set(T.REGISTRY)
    ok("Agent 工具仍是 9 个（没往决策循环里塞新工具）", len(names) == 9,
       str(sorted(names)))
    ok("工具表里没有判例/画像工具",
       not any(k in names for k in ("case_decision", "search_cases", "persona")))
    ok("画像模块不参与答案定性",
       "verdict" not in P.presentation(P.build_persona(q)))


class _Slots:
    def __init__(self, d: dict) -> None:
        self.d = dict(d)

    def known(self) -> dict:
        return dict(self.d)


class _FakeState:
    def __init__(self, q: str) -> None:
        self.question = q
        self.slots = _Slots({})
        self.verdict = None
        self.scratchpad = []


def _fake_state(q: str) -> _FakeState:
    return _FakeState(q)


def main() -> int:
    print("判例参考 / 申诉决策卡回归（v117，强制回放）")
    print("=" * 62)
    test_library()
    test_stance_filter()
    test_distribution()
    test_cost_benefit()
    test_entrypoints()
    print("\n" + "=" * 62)
    if FAILED:
        print(f"{TOTAL} 项：{TOTAL - len(FAILED)} 通过，{len(FAILED)} 失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"{TOTAL} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
