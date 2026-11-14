# -*- coding: utf-8 -*-
"""办案包（extras）回归：时间轴 / 请求事项 / 举证体检。

这套测试守的不是「文案好不好看」，是四条会伤到人的边界：

  1. **条号可核验**：模块里出现的每一个《法》第X条，都要能在
     data/statutes.jsonl 里查到。动态扫输出而不是只查常量表——
     有人往规则里新加一条没登记的引用，这样才拦得住。
  2. **金额不落纸**：请求事项里金额恒为留空，算出来的钱只进 estimates。
     docgen 那边「请求事项/金额/条号留空」是 46 项硬断言，这里不能从侧门破掉。
  3. **在职讨薪不画截止日**：法律上不存在的期限，画出来就是让人按错的日子行动。
  4. **宽泛词抑制**：「加付赔偿金」不该带出「违法解除赔偿金」。

篡改自检（改完代码要能红，才说明断言不是摆设）：
  · 删掉 `_pick_rules` 里的 shadowed 判断 → [3] 两条抑制断言红；
  · 把 BLANK 改成具体数字 → [3] 占位符固定 + 不出现金额数字两条红；
  · 去掉 `_deadline_anchor` 的 unlimited 分支 → [2] 在职那组红；
  · 往任一规则塞一个编造的条号 → [1] 语料核验红。

跑法：
    ./.venv/Scripts/python.exe scripts/test_extras.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 这一套必须能离线跑。清掉 key 是为了让「评测 86 题全回放」那条承诺
# 不因为这里意外 import 了 LLM 而悄悄破功。
os.environ.pop("DEEPSEEK_API_KEY", None)

from app import extras as X                                    # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- 语料索引

def corpus() -> set[tuple[str, str]]:
    p = ROOT / "data" / "statutes.jsonl"
    out: set[tuple[str, str]] = set()
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.add((d.get("law_name", ""), d.get("article_no_cn", "")))
            except (ValueError, AttributeError):
                pass
    return out


_CITE_RE = re.compile(r"《([^》]+)》(第[一二三四五六七八九十百零〇\d]+条)")


def parse_cite(s: str) -> tuple[str, str] | None:
    m = _CITE_RE.search(s or "")
    return (m.group(1), m.group(2)) if m else None


def all_output_cites() -> list[str]:
    """把规则表、时间轴、举证表里的引用全扫一遍，一个不漏。"""
    out: list[str] = list(X.CITES)
    for r in X._CLAIM_RULES:
        out.extend(r["basis"])
    for _, _, _, _, c in X._TIMELINE_NODES:
        out.append(c)
    for r in X._EVIDENCE_BASE:
        out.append(r["cite"])
    return out


def test_cites() -> None:
    print("\n[1] 条号可核验：一个都编不得")
    idx = corpus()
    ok("语料库能读到且非空", len(idx) > 100, f"只有 {len(idx)} 条")
    ok("CITES 非空", len(X.CITES) > 10)

    cites = all_output_cites()
    bad_fmt = [c for c in cites if not parse_cite(c)]
    ok("所有引用都是《法名》第X条格式", not bad_fmt, str(bad_fmt[:3]))

    missing = [c for c in cites if parse_cite(c) and parse_cite(c) not in idx]
    ok("模块输出的每条引用都在语料库里", not missing, str(missing[:3]))
    ok("CITES 里没有重复登记", len(set(X.CITES)) == len(X.CITES))

    # 动态：规则表里临时塞一条编的，扫描器必须能看见（否则上面的断言
    # 只查常量表，加漏登记的新引用就成假绿灯了）。
    probe = X._CLAIM_RULES[0]["basis"] + ["《中华人民共和国劳动合同法》第九百九十九条"]
    ok("扫描器能发现规则表里新加的假条号",
       any(parse_cite(c) not in idx for c in probe))

    listed = X.cited_articles()
    ok("cited_articles 返回 CITES 的内容", listed == list(X.CITES))
    listed.append("《编的》第一条")
    ok("改返回值不会污染模块常量", len(X.cited_articles()) == len(X.CITES))

    # 结构约束：这一层不许碰模型、数据库，也不许往 Agent 工具表里加东西。
    forbidden = ("LLM", "MemoryStore", "requests", "sqlite3", "urlopen")
    ok("extras 不持有模型/数据库/网络对象",
       not any(k in X.__dict__ for k in forbidden),
       str([k for k in forbidden if k in X.__dict__]))
    src = (ROOT / "app" / "extras.py").read_text(encoding="utf-8")
    ok("extras 没有注册 Agent 工具", "@tool(" not in src)
    from app import tools                                       # noqa: PLC0415
    ok("Agent 工具数仍贴着 10 的上限以内", len(tools.REGISTRY) <= 10,
       f"现在 {len(tools.REGISTRY)} 个")


# ---------------------------------------------------------------- 时间轴

DL_OK = {
    "ok": True, "unlimited": False,
    "conservative": {"onset": "2025-03-10", "deadline": "2026-03-10",
                     "onset_label": "知道权利被侵害之日"},
}
DL_ONGOING = {"ok": True, "unlimited": True, "conservative": None,
              "candidates": []}


def test_timeline() -> None:
    print("\n[2] 程序时间轴：只给算过的日期")
    t = X.build_timeline({})
    nodes = t["nodes"]
    ids = [n["id"] for n in nodes]
    ok("八个法定阶段齐备", len(nodes) == 8, str(ids))
    ok("阶段 id 不重复", len(set(ids)) == 8)
    ok("顺序：起算 → 申请 → 受理 → 裁决 → 起诉 → 执行",
       ids == ["onset", "fix", "apply", "accept", "award",
               "sue", "effective", "execute"], str(ids))
    ok("每个节点都有相对期间、说明和依据",
       all(n.get("when") and n.get("detail") and n.get("cite") for n in nodes))
    ok("没有起算日时整块仍然可用", t["available"] is True)
    ok("没有起算日时不给锚点", t["anchor"] is None and t["deadline"] is None)
    ok("没有起算日时一个绝对日期都不编",
       all(n["date"] is None for n in nodes))
    ok("没核算时明说只列阶段", "只列法定阶段" in t["anchor_note"])

    by = {n["id"]: n for n in nodes}
    ok("申请节点引第 27 条", "第二十七条" in by["apply"]["cite"])
    ok("受理节点引第 29 条", "第二十九条" in by["accept"]["cite"])
    ok("裁决节点引第 43 条", "第四十三条" in by["award"]["cite"])
    ok("起诉节点引第 48 条", "第四十八条" in by["sue"]["cite"])
    ok("执行节点引第 51 条", "第五十一条" in by["execute"]["cite"])
    joined = " ".join(n["when"] + " " + n["detail"] for n in nodes)
    ok("法定期间写清楚（5 日 / 45 日 / 15 日）",
       all(k in joined for k in ("5 日内", "45 日内", "15 日内")), joined[:120])
    ok("末尾说明日期按期间规则核算、其余是法定相对期间",
   "按期间规则核算" in t["note"] and "法定相对期间" in t["note"], t["note"])

    t2 = X.build_timeline({}, DL_OK)
    by2 = {n["id"]: n for n in t2["nodes"]}
    ok("算过期限后用工具给的起算日", t2["anchor"] == "2025-03-10")
    ok("算过期限后用工具给的截止日", t2["deadline"] == "2026-03-10")
    ok("起算节点标绝对日期", by2["onset"]["date"] == "2025-03-10")
    ok("申请节点标截止日", by2["apply"]["date"] == "2026-03-10")
    ok("不自己再加一年日期", "2026-03-10" in by2["apply"]["detail"])

    t3 = X.build_timeline({}, DL_ONGOING)
    ok("在职讨薪：没有截止日", t3["deadline"] is None and t3["unlimited"] is True)
    ok("在职讨薪：一个绝对日期都不画",
       all(n["date"] is None for n in t3["nodes"]))
    ok("在职讨薪：申请阶段改成不受一年限制",
       "不受一年" in {n["id"]: n for n in t3["nodes"]}["apply"]["when"])
    ok("在职讨薪：说明里点出离职后仍有一年",
       "终止之日起一年内" in {n["id"]: n for n in t3["nodes"]}["apply"]["detail"])

    t4 = X.build_timeline({"end_date": "2025-06-01"},
                          {"ok": False, "error": "起算日期无法解析"})
    ok("工具算失败时不给用户随口填的离职日当时效起算日",
       t4["anchor"] is None and all(n["date"] is None for n in t4["nodes"]))
    ok("有离职日但没核算时明确提示", "没做期限核算" in t4["anchor_note"])

    t5 = X.build_timeline({}, {"ok": True, "unlimited": False,
                               "conservative": None})
    ok("conservative 缺失时不硬取日期", t5["deadline"] is None)


# ---------------------------------------------------------------- 请求事项

def ids_of(claims: dict) -> list[str]:
    return [i["id"] for i in claims["items"]]


def test_claims() -> None:
    print("\n[3] 请求事项：候选、留空、不替人勾选")
    # 断言不能拿 X.BLANK 自己当期望值：把常量改成「99999」时，被测文本和
    # 期望值会一起变，整组断言照样全绿——那正是这个项目反复在防的假绿灯。
    ok("金额占位符固定为全角下划线（不许换成数字）",
       X.BLANK == "＿＿＿＿", repr(X.BLANK))
    # 先确认「不该命中」断言对象真的存在，否则没命中就是恒真的假绿灯。
    all_ids = {r["id"] for r in X._CLAIM_RULES}
    ok("规则表里确有 illegal_termination / wage_arrears",
       {"illegal_termination", "wage_arrears"} <= all_ids)

    c = X.from_case("公司一直没签劳动合同，我能要什么？")["claims"]
    ok("没签合同 → 二倍工资", "double_wage" in ids_of(c))
    c = X.from_case("公司无故把我辞退了，能要赔偿金吗？")["claims"]
    ok("无故辞退 → 违法解除赔偿金", "illegal_termination" in ids_of(c))
    c = X.from_case("老板拖欠工资三个月了")["claims"]
    ok("欠薪 → 支付劳动报酬", "wage_arrears" in ids_of(c))

    c = X.from_case("试用期不发工资合法吗")["claims"]
    ok("试用期不发工资 → 补足差额", "probation_wage" in ids_of(c))
    ok("长词压住短词：不带出欠薪请求", "wage_arrears" not in ids_of(c),
       str(ids_of(c)))

    c = X.from_case("能不能要加付赔偿金？")["claims"]
    ok("加付赔偿金 → 第 85 条那一项", "add_compensation" in ids_of(c))
    ok("长词压住短词：不带出违法解除赔偿金",
       "illegal_termination" not in ids_of(c), str(ids_of(c)))
    ok("带前提条件的候选项有 caution",
       any(i["caution"] for i in c["items"] if i["id"] == "add_compensation"))

    c = X.from_case("公司没交社保，怎么办")["claims"]
    ok("社保项有「可能不属仲裁受理」的提醒",
       any("不属仲裁受理范围" in i["caution"] for i in c["items"]
           if i["id"] == "social_insurance"))
    c = X.from_case("想申请确认劳动关系")["claims"]
    ok("确认劳动关系 → 独立请求", "confirm_relation" in ids_of(c))

    many = ("没签劳动合同 拖欠工资 违法解除 试用期不发工资 "
            "没交社保 确认劳动关系 加付赔偿金")
    c = X.from_case(many)["claims"]
    ok("一次最多摆四条", len(c["items"]) <= X._MAX_CLAIMS,
       str(len(c["items"])))
    ok("命中多条时按触发词长度排",
       ids_of(c)[0] == "probation_wage", str(ids_of(c)))
    lens = [len(X._longest_hit(next(r for r in X._CLAIM_RULES
                                    if r["id"] == cid), many))
            for cid in ids_of(c)]
    ok("触发词长度单调不增", lens == sorted(lens, reverse=True), str(lens))

    c = X.from_case("没签劳动合同，能要多少")["claims"]
    it = c["items"][0]
    ok("金额一律留空", X.BLANK in it["text"])
    ok("金额占位字段恒存在", it["amount_blank"] == X.BLANK)
    ok("候选里不出现具体金额数字", not re.search(r"\d{3,}", it["text"]))
    ok("默认一条都不勾", all(i["checked"] is False for i in c["items"]))
    ok("每条都有可核验的依据", all(parse_cite(b) for i in c["items"]
                                  for b in i["basis"]))
    ok("说明里写清金额必须本人填", "金额一律留空" in c["note"])
    ok("带免责", "不构成法律意见" in c["disclaimer"])

    c2 = X.from_case("没签劳动合同", slots={"monthly_wage": 12000})["claims"]
    ok("缺入职时间 → missing 列出来",
       "start_date" in c2["items"][0]["missing"], str(c2["items"][0]["missing"]))
    c3 = X.from_case("没签劳动合同",
                     slots={"start_date": "2024-03-01",
                            "monthly_wage": 12000})["claims"]
    ok("要素齐 → missing 为空", c3["items"][0]["missing"] == [])

    calc = {"scenarios": [{"label": "经济补偿", "amount": 99999,
                           "months": 2.0, "formula": "12000 × 2"}]}
    c4 = X.from_case("被辞退了能拿多少钱", calc_data=calc)["claims"]
    ok("算出的金额进 estimates", c4["estimates"][0]["amount"] == 99999)
    ok("算出的金额**不**写进任何请求事项",
       all("99999" not in i["text"] for i in c4["items"]))
    ok("有核算结果时说明里点出「不自动填入」", "不自动填入" in c4["note"])

    c5 = X.from_case("今天天气不错")["claims"]
    ok("无关问题不硬凑候选", c5["items"] == [] and c5["available"] is False)
    c6 = X.from_case("我这个情况还能主张什么",
                     verdict={"label": "可以主张二倍工资"})["claims"]
    ok("问题里没术语、定性里有 → 照样命中", "double_wage" in ids_of(c6),
       str(ids_of(c6)))


# ---------------------------------------------------------------- 举证体检

def test_evidence() -> None:
    print("\n[4] 举证体检：谁举证、差什么")
    e = X.from_case("被辞退了")["evidence_check"]
    items = e["items"]
    ok("六类基础材料齐备", len(items) == 6, str(len(items)))
    ok("id 不重复", len({i["id"] for i in items}) == 6)
    by = {i["id"]: i for i in items}
    ok("单位掌握的材料标 who=单位", by["employer"]["who"] == "单位")
    ok("单位举证引第 39 条", "第三十九条" in by["employer"]["cite"])
    ok("时效中断引第 27 条", "第二十七条" in by["interrupt"]["cite"])
    ok("每条都写清材料与理由",
       all(i["materials"] and i["why"] and i["cite"] for i in items))
    ok("「已记下不等于有证据」这句不能省", "不等于手上有证据" in e["note"])
    ok("带免责", "不构成法律意见" in e["disclaimer"])
    ok("summary 与明细一致",
       e["summary"]["have"] + e["summary"]["todo"] == len(items))

    e2 = X.from_case("没签劳动合同", slots={"has_contract": False,
                                            "monthly_wage": 12000,
                                            "start_date": "2024-03-01",
                                            "leave_reason": "辞退"})["evidence_check"]
    by2 = {i["id"]: i for i in e2["items"]}
    ok("has_contract=False 也算要素已明确", by2["relation"]["status"] == "have")
    ok("没合同仍给得出替代材料", "工资流水" in by2["relation"]["materials"])
    ok("四个要素齐时 have=4", e2["summary"]["have"] == 4,
       str(e2["summary"]))
    ok("无槽位时四项待补", X.from_case("x")["evidence_check"]["summary"]["have"] == 0)

    e3 = X.from_case("没签劳动合同，还欠薪")["evidence_check"]
    by3 = {i["id"]: i for i in e3["items"]}
    ok("未签合同追加「用工满一个月」材料",
       any("用工满一个月" in s for s in by3["relation"]["extra"]),
       str(by3["relation"]["extra"]))
    ok("欠薪追加「欠薪明细」材料",
       any("欠薪明细" in s for s in by3["wage"]["extra"]),
       str(by3["wage"]["extra"]))
    e4 = X.from_case("今天天气")["evidence_check"]
    ok("没有命中请求时不追加专属材料",
       all(not i["extra"] for i in e4["items"]))


# ---------------------------------------------------------------- 统一入口

class _Step:
    def __init__(self, tool: str, data: dict | None = None, error: str | None = None):
        self.tool, self.data, self.error = tool, data or {}, error


class _Slots:
    def __init__(self, d: dict):
        self._d = d

    def known(self) -> dict:
        return dict(self._d)


def _state(steps: list, slots: dict | None = None, question: str = "没签劳动合同"):
    return SimpleNamespace(
        question=question, slots=_Slots(slots or {}), verdict=None,
        scratchpad=steps,
    )


def test_entrypoints() -> None:
    print("\n[5] 统一入口：回放、独立接口、MCP 共用一份")
    r = X.build_extras(_state([]))
    # v117 从三块扩到五块。这里仍然用「恰好等于」而不是「包含」：
    # 多出一个没登记的块，和少一块一样，都该被看见。
    ok("五块产物齐全",
       set(r) == {"timeline", "claims", "evidence_check", "decision", "persona"})
    ok("v116 的三块没被挤掉",
       {"timeline", "claims", "evidence_check"} <= set(r))
    ok("默认立场如实标 unset（不把默认值伪装成用户选过）",
       r["persona"]["stance"] is None
       and r["persona"]["stance_source"] == "unset", str(r["persona"]["stance"]))
    ok("判例检索仍按保守的职工视角过滤",
       r["decision"]["scope"]["applicant"] == "employee")
    ok("决策卡里没有胜诉率",
       "no_win_rate" in r["decision"].get("policy", {}))
    ok("空轨迹不崩、时间轴仍给阶段", len(r["timeline"]["nodes"]) == 8)
    ok("空轨迹不给日期", r["timeline"]["anchor"] is None)

    r2 = X.build_extras(_state([_Step("calculate_deadline", DL_OK)]))
    ok("轨迹里的期限结果被采用", r2["timeline"]["anchor"] == "2025-03-10")
    calc = {"scenarios": [{"label": "经济补偿", "amount": 36000}]}
    r3 = X.build_extras(_state([_Step("calculate_compensation", calc)]))
    ok("轨迹里的金额结果被采用", r3["claims"]["estimates"][0]["amount"] == 36000)

    r4 = X.build_extras(_state([_Step("calculate_deadline", DL_OK,
                                      error="日期解析失败")]))
    ok("失败的工具步骤不被当成结果", r4["timeline"]["anchor"] is None)

    r5 = X.build_extras(_state([_Step("calculate_deadline", {"ok": False}),
                                _Step("calculate_deadline", DL_OK)]))
    ok("同一工具多次调用取最近一次成功的", r5["timeline"]["anchor"] == "2025-03-10")

    q, slots = "没签劳动合同", {"monthly_wage": 12000}
    a = X.from_case(q, slots=slots)
    b = X.build_extras(_state([], slots, q))
    ok("from_case 与 build_extras 五块结果一致",
       all(a[k] == b[k] for k in r), str(sorted(set(a) ^ set(b))))

    # 回放模式下没有模型，extras 也必须完整。这一条钉住「零 LLM」的承诺。
    os.environ["DEEPSEEK_API_KEY"] = ""
    ok("清空 key 后仍能产出完整办案包",
       len(X.from_case("违法解除")["timeline"]["nodes"]) == 8)


def main() -> int:
    test_cites()
    test_timeline()
    test_claims()
    test_evidence()
    test_entrypoints()
    print("\n" + "=" * 62)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"{total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        for n in FAIL:
            print(f"  - {n}")
        return 1
    print(f"{total} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
