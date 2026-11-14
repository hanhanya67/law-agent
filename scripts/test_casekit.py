# -*- coding: utf-8 -*-
"""v124 回归：案件级附件、条件式倾向卡、分支程序表。

为什么单独一套：这版新增的东西跨了三层（cases / casekit+memory / 前端），
既有套件各看一段，没有一个地方同时钉住「五内核都给得出、红线没破、
开关按任务存、删任务不留孤儿行」。少其中任何一条都能让交付包看起来
是好的，实际点开某个内核就露馅。

跑法：
    ./.venv/Scripts/python.exe scripts/test_casekit.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import casekit, cases, domains, extras, profiles  # noqa: E402
from app import docgen  # noqa: E402
from app.memory import MemoryStore  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

N = 0
FAILS: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          (("  → " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


# 那三个词是全局红线：整份决策卡不许出现「建议/应当/不该 + 申诉/起诉/仲裁」。
# 「建议上诉 / 建议不上诉」不在其中 —— 上诉是对已有裁判走下一程序，
# 且必须跟着「由本人决定」。
RED_VERDICT = re.compile(r"(不?建议|应当|不该)(申诉|起诉|仲裁)")
# v137：费率也是百分比，所以判据不是「有没有 %」而是「挂在什么语境上」。
from app.costs import bad_percentages as _bad_pct   # noqa: E402
RED_PCT = re.compile(r"\d+(?:\.\d+)?\s*%")
BLANK = "＿＿＿＿"

PIDS = ("labor", "consumer", "civil", "traffic", "family")


def _scrub(obj):
    """扫「有没有给独断结论」前，先剔掉 no_* 否定声明字段。

    「不提供…『建议起诉』结论」这句话里当然含那四个字，把它算成违规是
    假红灯；但直接把关键词从正则里删掉又是真放宽。和 test_profiles.py
    同一口径：声明与结论分开扫，再单独断言声明必须存在。
    """
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()
                if not str(k).startswith("no_")}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def test_inclination() -> None:
    print("\n[1] 结论卡：直接给结论，依据只能是事实")
    slots_full = {"monthly_wage": 12000, "has_contract": True,
                  "start_date": "2023-06", "end_date": "2026-08"}
    calc = {"scenarios": [{"label": "经济补偿", "amount": 36000}]}

    go = cases.build_decision("公司拖欠工资，能要什么",
                              slots=slots_full, calc_data=calc)
    cb = go["cost_benefit"]
    inc = cb.get("inclination") or {}
    ok("成本卡内部带 inclination（没新增 decision 顶层键）",
       set(go) == {"scope", "similar", "cost_benefit", "policy"} and bool(inc))
    ok("信息齐备时给出明确方向", inc.get("tone") == "go", inc.get("label"))
    ok("结论直接写「建议上诉」", inc.get("label") == "建议上诉", inc.get("label"))
    ok("结论挂着已核算金额作依据",
       "36000" in (inc.get("verdict") or ""), inc.get("verdict"))
    ok("免责收成一句",
       inc.get("disclaimer") == "仅供参考，不构成法律意见。",
       inc.get("disclaimer"))
    inc_blob = json.dumps(inc, ensure_ascii=False)
    ok("不再写「由本人决定 / 不替你拍板」",
       "由本人决定" not in inc_blob and "拍板" not in inc_blob
       and "条件式" not in inc_blob)
    ok("不给任何百分比", not _bad_pct(json.dumps(go, ensure_ascii=False)))
    ok("整份决策卡不出现独断搭配",
       not RED_VERDICT.search(json.dumps(go, ensure_ascii=False)))
    ok("成本收益仍是六行", len(cb["rows"]) == 6)
    ok("不再输出 no_verdict 垫话", "no_verdict" not in cb)
    ok("金额行仍是留空符",
       BLANK in next(r["value"] for r in cb["rows"] if r["label"] == "可得金额"))

    empty = cases.build_decision("没签劳动合同，还拖欠工资")
    ok("信息不足时收成「先补齐材料」",
       empty["cost_benefit"]["inclination"]["tone"] == "hold",
       empty["cost_benefit"]["inclination"]["label"])
    risky = cases.build_decision("时效快到了还能申请吗",
                                 slots={},
                                 calc_data={"scenarios": [{"amount": 1200}]})
    ok("时效风险 + 小额 → 倾向暂不上诉",
       risky["cost_benefit"]["inclination"]["tone"] == "wait",
       risky["cost_benefit"]["inclination"]["label"])

    # 两个入口必须逐字段一致（test_cases 那条严格相等不能被新字段破坏）。
    direct = cases.build_decision("没签劳动合同，还拖欠工资",
                                  stance="employee")
    via = extras.from_case("没签劳动合同，还拖欠工资")["decision"]
    ok("build_decision 与 from_case 的倾向卡一致",
       direct["cost_benefit"]["inclination"] == via["cost_benefit"]["inclination"])

    # 接入内核也要有同一张卡，且不能夹带劳动争议话术或独断搭配。
    for pid in PIDS[1:]:
        p = profiles.resolve(pid)
        with profiles.use(pid):
            r = extras.from_case("买到过期食品能赔多少")
        inc2 = r["decision"]["cost_benefit"].get("inclination") or {}
        ok(f"{pid} 也有结论卡", bool(inc2))
        blob = json.dumps(r, ensure_ascii=False)
        clean = json.dumps(_scrub(r), ensure_ascii=False)
        ok(f"{pid} 办案包不含独断搭配", not RED_VERDICT.search(clean))
        ok(f"{pid} 办案包不含胜率百分比", not _bad_pct(blob))
        pol = r["decision"].get("policy", {})
        ok(f"{pid} 保留了不提供胜率的声明",
           "no_win_rate" in pol and
           "不提供" in str(pol.get("no_win_rate", "")))
        ok(f"{pid} 判例卡仍是四键",
           set(r["decision"]) == {"scope", "similar", "cost_benefit", "policy"})


def test_procedure() -> None:
    print("\n[2] 分支程序表：五内核都给得出，且不编库外条号")
    for pid in PIDS:
        pr = casekit.build_procedure(pid)
        nodes = pr.get("nodes") or []
        br = pr.get("branch") or {}
        ok(f"{pid} 程序表可用且有节点", pr.get("available") is True and bool(nodes),
           len(nodes))
        ok(f"{pid} 有分叉且分叉点落在真实节点上",
           bool(br.get("at")) and br["at"] in {n["id"] for n in nodes}, br.get("at"))
        paths = br.get("paths") or []
        ok(f"{pid} 分叉至少两条路径", len(paths) >= 2, len(paths))
        ok(f"{pid} 含起诉/上诉路径",
           any("上诉" in (p.get("label") or "") for p in paths))
        appeal = next((p for p in paths if "上诉" in (p.get("label") or "")), {})
        # 《民事诉讼法》没随包收录，硬编条号会变成查不到的「依据」。
        ok(f"{pid} 上诉路径不编库外条号", not appeal.get("cite"))
        ok(f"{pid} 上诉路径提示以文书告知为准",
           "以判决书" in (appeal.get("detail") or ""))
        ok(f"{pid} 节点都带法定期间与说明",
           all(n.get("when") and n.get("detail") for n in nodes))

    # 劳动法的八个节点必须和既有时间轴逐字一致，不能另造一套。
    tl = extras.build_timeline()
    ok("劳动法程序表沿用既有八阶段",
       [n["id"] for n in casekit.build_procedure("labor")["nodes"]]
       == [n["id"] for n in tl["nodes"]])


def test_document() -> None:
    print("\n[3] 五内核文书骨架：留空、不代笔、不宣称可提交")
    msgs = [
        {"role": "user", "ts": 1700000000,
         "content": "我在今年三月买的食品过期了，商家只肯退货不肯赔偿，有订单和实物照片"},
        {"role": "assistant", "ts": 1700000001, "content": "好的，我看一下条文"},
        {"role": "user", "ts": 1700000002, "content": "你好"},
    ]
    for pid in PIDS:
        d = casekit.build_document(pid, slots={"monthly_wage": 8000},
                                   profile={}, messages=msgs)
        text = d.get("text") or ""
        ok(f"{pid} 文书骨架非空", len(text) > 400, len(text))
        ok(f"{pid} 请求/金额留空", BLANK in text)
        ok(f"{pid} 不宣称可以提交", d.get("ready") is False)
        ok(f"{pid} 只摘用户原话、不写助手的话",
           "我在今年三月买的食品过期了" in text and "好的，我看一下条文" not in text)
        ok(f"{pid} 寒暄不进事实段", "你好" not in text.split("证据清单")[0])
        ok(f"{pid} 带提交前核对清单", len(d.get("checklist") or []) >= 3)

    ok("劳动法仍走原仲裁申请书",
       casekit.build_document("labor")["title"] == "劳动人事争议仲裁申请书")
    ok("接入内核标题各不相同",
       len({casekit.build_document(p)["title"] for p in PIDS[1:]}) == 4)
    ok("未知内核回落劳动法（不猜）",
       casekit.build_document("not_a_profile")["title"]
       == "劳动人事争议仲裁申请书")
    ok("下载文件名按内核变",
       docgen.filename_for("consumer").startswith("消费与食品安全争议"))


def test_flags() -> None:
    print("\n[4] 附件开关：按任务存、只改一项、越权拒绝、删任务级联清")
    # 不用 TemporaryDirectory 的自动清理：Windows 上 SQLite 还握着 db 句柄时
    # rmtree 会抛 WinError 32，测试明明全绿却以异常收尾。显式 close 再删。
    td = tempfile.mkdtemp(prefix="casekit_")
    try:
        st = MemoryStore(Path(td) / "t.db")
        _flags_body(st)
        st._conn.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _flags_body(st: MemoryStore) -> None:
    t1 = st.create_task(7, "案子甲", profile_id="consumer")
    t2 = st.create_task(7, "案子乙", profile_id="labor")
    a, b = t1["task_id"], t2["task_id"]

    ok("新任务两个开关默认全关",
       st.get_attachment_flags(a) == {"procedure": False, "document": False})
    r = st.set_attachment_flags(a, 7, {"procedure": True})
    ok("只开时间表", r == {"procedure": True, "document": False}, r)
    r = st.set_attachment_flags(a, 7, {"document": True})
    ok("再开申请书，时间表保持开",
       r == {"procedure": True, "document": True}, r)
    ok("另一案子不受影响",
       st.get_attachment_flags(b) == {"procedure": False, "document": False})
    ok("越权改别人的任务被拒",
       st.set_attachment_flags(a, 999, {"document": False}) is None)
    ok("越权不改坏原值", st.get_attachment_flags(a)["document"] is True)
    r = st.set_attachment_flags(a, 7, {"procedure": False})
    ok("关掉一项，另一项不动",
       r == {"procedure": False, "document": True}, r)

    ok("删任务成功", st.delete_task(a, 7) is True)
    # 直接查表：级联漏了就会留一行孤儿。
    with st._lock:
        left = st._conn.execute(
            "SELECT COUNT(*) FROM task_attachments WHERE task_id=?", (a,)
        ).fetchone()[0]
    ok("删任务不留附件孤儿行", left == 0, left)
    ok("另一个案子的开关还在",
       st.get_attachment_flags(b)["document"] is False)


def test_server_wiring() -> None:
    print("\n[5] 服务端接线与包内文件")
    srv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    ok("GET /api/case/attachments 已注册",
       '@app.get("/api/case/attachments")' in srv)
    ok("POST /api/case/attachments 已注册",
       '@app.post("/api/case/attachments")' in srv)
    ok("默认关时不做无谓生成", "if not (flags[\"procedure\"] or flags[\"document\"]):" in srv)
    ok("归属校验在位", "task_owner(tid) != uid" in srv)
    mem = (ROOT / "app" / "memory.py").read_text(encoding="utf-8")
    ok("删任务级联清附件表",
       "DELETE FROM task_attachments WHERE task_id=?" in mem)
    ok("casekit.py 随包", (ROOT / "app" / "casekit.py").is_file())


def main() -> int:
    test_inclination()
    test_procedure()
    test_document()
    test_flags()
    test_server_wiring()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"{N} 项：{N - len(FAILS)} 通过，{len(FAILS)} 失败")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"{N} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
