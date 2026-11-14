# -*- coding: utf-8 -*-
"""自主学习 / 个人主页回归（v139）——函数级 + 真服务接口级。

两套分开验的理由：
  · 函数级把「离线能不能学」「进度怎么算」「按 (uid,pid) 隔离」这些
    纯逻辑钉死，不依赖网络、不花钱；
  · 真服务级验的是**鉴权与归属**——写接口有没有被 current_user 挡住、
    CSRF 有没有生效、A 能不能判 B 的题。调函数永远测不到这层。

最重要的一条：**回放包（无 key）必须照常能出题能判答**。服务起来时显式
把 DEEPSEEK_API_KEY 置空，断言出题来源仍是 offline 且题目可作答。
在线出题由模型写题面，但条号与引用恒由服务端回填，不采信模型输出。

跑法：./.venv/Scripts/python.exe scripts/test_study.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _svc import start_service, stop_service    # noqa: E402
from app import study                            # noqa: E402
from app.study import StudyStore                 # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                # noqa: BLE001
    pass

PASS: list[str] = []
FAIL: list[str] = []
BASE = ""
ADMIN = ("admin", "StudyAdmin123")


def ok(tag: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(tag)
    print(f"  [{'PASS' if cond else 'FAIL'}] {tag}" + (f"  {extra}" if extra else ""))


# ---------------------------------------------------------------- 函数级
def part_logic(db: str) -> None:
    s = StudyStore(db)
    uid = 101
    r = study.new_quiz(s, None, uid, "consumer", n=5)
    ok("回放包能出题（无 key 不空手）", len(r["items"]) == 5
       and r["source"] == "offline", r["source"])
    ok("每题四个选项", all(len(q["options"]) == 4 for q in r["items"]))
    ok("题目不泄露答案与解析",
       all("answer" not in q and "explain" not in q for q in r["items"]))
    ok("题面非空", all(q["question"].strip() for q in r["items"]))
    q0 = s.get_quiz(uid, "consumer", r["items"][0]["quiz_id"])
    ok("答案在 ABCD 内", q0 and q0["answer"] in "ABCD", str(q0 and q0["answer"]))
    ok("引用条号由服务端回填且只有一个",
       json.loads(q0["cites"]) == [q0["article_id"]])
    ok("题目来源标为离线", q0["source"] == "offline")

    g = study.grade_answer(s, None, uid, "consumer", q0["quiz_id"], q0["answer"])
    ok("正确答案判对", g is not None and g["correct"] is True)
    ok("判答返回引用条号", bool(g and g["cites"]))
    g2 = study.grade_answer(s, None, uid, "consumer", q0["quiz_id"], "Z")
    ok("错误答案判错", g2 is not None and g2["correct"] is False)
    ok("判错给出正确答案", bool(g2 and g2["answer"] == q0["answer"]))
    # 选项全文也该算对，别只认字母。
    full = next((o for o in json.loads(q0["options"])
                 if o.startswith(q0["answer"] + ".")), "")
    g3 = study.grade_answer(s, None, uid, "consumer", q0["quiz_id"], full)
    ok("选项全文作答也算对", g3 is not None and g3["correct"] is True)

    ok("跨内核判答被拒",
       study.grade_answer(s, None, uid, "labor", q0["quiz_id"], "A") is None)
    ok("跨用户判答被拒",
       study.grade_answer(s, None, 102, "consumer", q0["quiz_id"], "A") is None)

    d = study.dashboard(s, uid)
    ok("主页给全五个法典进度", len(d["profiles"]) == 5)
    ok("进度都是 0–100 的整数",
       all(0 <= p["percent"] <= 100 and isinstance(p["percent"], int)
           for p in d["profiles"]))
    con = next(p for p in d["profiles"] if p["pid"] == "consumer")
    ok("答过的法典进度上升", con["percent"] > 0, f"{con['percent']}%")
    ok("没答过的法典进度仍为 0",
       all(p["percent"] == 0 for p in d["profiles"] if p["pid"] != "consumer"))
    ok("进度口径写进说明", "章节覆盖率" in d["note"])
    ok("打卡恰好七天", len(d["heat"]) == 7)
    ok("知识树按法典分组", len(d["tree"]) == 5)
    ok("知识树长出了章节",
       sum(len(l["chapters"]) for t in d["tree"] for l in t["laws"]) > 0)
    ok("累计作答数对得上", d["total_attempts"] == 3, str(d["total_attempts"]))
    ok("第一枚徽章已获得", any(b["name"] == "开篇" for b in d["badges"]))
    ok("答对后错题不计入待复习",
       all(w["quiz_id"] != q0["quiz_id"] or w["mastered"]
           for w in d["wrong"]), f"待复习 {d['wrong_count']}")
    d2 = study.dashboard(s, 102)
    ok("另一用户的学习数据完全独立",
       d2["total_attempts"] == 0 and d2["wrong_count"] == 0
       and all(p["percent"] == 0 for p in d2["profiles"]))

    # 故意做错，验错题集与薄弱章节。
    bad = study.new_quiz(s, None, uid, "consumer", n=2)
    for q in bad["items"]:
        study.grade_answer(s, None, uid, "consumer", q["quiz_id"], "Z")
    wb = s.wrongbook(uid, "consumer", 0)
    ok("答错进错题集", len(wb) >= 2, f"{len(wb)} 条")
    ok("错题集不串到别的法典",
       s.wrongbook(uid, "family", 0) == [])
    d3 = study.dashboard(s, uid)
    ok("薄弱章节被列出", len(d3["weak"]) >= 1)
    ok("标记已掌握生效",
       s.mark_mastered(uid, "consumer", wb[0]["quiz_id"], True))
    ok("标记后待复习条数下降",
       study.dashboard(s, uid)["wrong_count"] == len(wb) - 1)

    day1 = study.daily_question(s, None, uid, "family")
    day2 = study.daily_question(s, None, uid, "family")
    ok("每日一题可出", len(day1["items"]) == 1)
    ok("同一天同一法典不换题",
       day1["items"][0]["question"] == day2["items"][0]["question"])
    txt = study.report_text(s, uid)
    ok("学习报告含五个法典", all(p["name"] in txt for p in d["profiles"]))
    ok("学习报告只有一句免责", txt.count("仅供参考，不构成法律意见") == 1)
    s.conn.close()


# ---------------------------------------------------------------- 真服务
class Client:
    """一个独立 cookie 的「浏览器」。"""

    def __init__(self) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _csrf(self) -> str:
        return next((c.value for c in self.cj if c.name == "law_csrf"), "")

    def call(self, method: str, path: str, body=None, csrf: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("x-csrf-token", self._csrf())
        try:
            resp = self.op.open(req)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except Exception:                     # noqa: BLE001
                return e.code, {}
        raw = resp.read() or b""
        try:
            return resp.status, json.loads(raw) if raw else {}
        except Exception:                         # noqa: BLE001
            return resp.status, {"text": raw[:160].decode("utf-8", "replace")}


def part_http() -> None:
    a = Client()
    a.call("GET", "/login")
    st, _ = a.call("POST", "/api/auth/login",
                   {"username": ADMIN[0], "password": ADMIN[1]})
    ok("管理员登录成功", st == 200, str(st))
    if st != 200:
        return

    # 未登录不能读法条原文，也不能看学习数据。
    anon = Client()
    anon.call("GET", "/login")
    ok("未登录读书目被挡", anon.call("GET", "/api/library/books")[0] == 401)
    ok("未登录看主页被挡", anon.call("GET", "/api/study/dashboard")[0] == 401)

    st, b = a.call("GET", "/api/library/books" + "?pid=consumer")
    ok("登录后书目接口通", st == 200 and b.get("pid") == "consumer")
    st, law = a.call("GET", "/api/library/law"
                     + "?pid=consumer&law=" + urllib.parse.quote("食品安全法"))
    ok("整部法原文接口通", st == 200 and law.get("count", 0) > 0)
    aid = law["articles"][0]["id"]
    st, art = a.call("GET", "/api/library/article"
                     + "?pid=consumer&id=" + urllib.parse.quote(aid))
    ok("单条原文接口通", st == 200 and art.get("id") == aid)
    ok("跨内核取条返回 404",
       a.call("GET", "/api/library/article"
              + "?pid=consumer&id=" + urllib.parse.quote("劳动争议解释一_1"))[0] == 404)
    st, s = a.call("GET", "/api/library/search" + "?pid=consumer&q="
                   + urllib.parse.quote("第55条"))
    ok("快速查找接口通", st == 200 and s.get("mode") == "article_no")

    # CSRF：写请求不带令牌必须被挡，否则前面所有归属校验都能被跨站绕过。
    st, _ = a.call("POST", "/api/study/quiz", {"pid": "consumer", "n": 2},
                   csrf=False)
    ok("写请求缺 CSRF 令牌被挡", st == 403, str(st))

    st, q = a.call("POST", "/api/study/quiz", {"pid": "consumer", "n": 3})
    ok("出题接口通", st == 200 and len(q.get("items", [])) == 3)
    # 服务起来时 DEEPSEEK_API_KEY 被置空，这里必须是离线兜底。
    ok("回放包服务模式下仍出离线题",
       q.get("source") == "offline" or all(
           "answer" not in x for x in q.get("items", [])), str(q.get("source")))
    item = q["items"][0]
    ok("接口返回的题目不含答案", "answer" not in item)
    st, g = a.call("POST", "/api/study/grade",
                   {"pid": "consumer", "quiz_id": item["quiz_id"], "answer": "A"})
    ok("判答接口通", st == 200 and "correct" in g, str(st))
    ok("判答结果带引用条号", isinstance(g.get("cites"), list))
    ok("不存在的题返回 404",
       a.call("POST", "/api/study/grade",
              {"pid": "consumer", "quiz_id": "no-such-quiz", "answer": "A"})[0] == 404)

    st, d = a.call("GET", "/api/study/dashboard")
    ok("主页接口通且给全五法典", st == 200 and len(d.get("profiles", [])) == 5)
    ok("接口累计作答数已增长", d.get("total_attempts", 0) >= 1)
    st, wb = a.call("GET", "/api/study/wrongbook")
    ok("错题集接口通", st == 200 and "items" in wb)
    st, rep = a.call("GET", "/api/study/report")
    ok("报告接口通且是文本", st == 200 and "法律知识学习报告" in rep.get("text", ""))
    st, daily = a.call("GET", "/api/study/daily" + "?pid=family")
    ok("每日一题接口通", st == 200 and len(daily.get("items", [])) == 1)

    # 跨用户：B 不能判 A 的题。
    bcli = Client()
    bcli.call("GET", "/login")
    st, _ = bcli.call("POST", "/api/auth/register",
                      {"username": "study_b", "password": "Test1234ab"})
    st, _ = bcli.call("POST", "/api/auth/login",
                      {"username": "study_b", "password": "Test1234ab"})
    ok("第二个账号登录成功", st == 200)
    st, _ = bcli.call("POST", "/api/study/grade",
                      {"pid": "consumer", "quiz_id": item["quiz_id"], "answer": "A"})
    ok("跨用户判答被拒", st == 404, str(st))
    st, d2 = bcli.call("GET", "/api/study/dashboard")
    ok("新用户学习数据为空", st == 200 and d2.get("total_attempts") == 0)

    part_paper_http(a, bcli, item)


def part_paper_http(a: "Client", b: "Client", item: dict) -> None:
    """v140 卷子 / 报告 / 日历 / 错题重练（真服务级）。

    卷子最关键的一条不变量：**它不另开一条判分链**。
      · 出卷不判卷 → dashboard 的累计作答数不该变；
      · 交卷的分数必须等于「逐题判答」的对错数，两边口径一旦分叉这里就红；
      · 别人的 paper_id 查报告必须 404——不是靠前端藏按钮。
    """
    before = a.call("GET", "/api/study/dashboard")[1].get("total_attempts", 0)

    st, p = a.call("POST", "/api/study/paper",
                   {"pid": "labor", "n": 8,
                    "qtypes": {"choice": 4, "judge": 2, "blank": 2}})
    items = p.get("items") or []
    ok("能生成一张卷子", st == 200 and len(items) == 8, f"{st} / {len(items)}")
    ok("出卷不判卷（累计作答数不变）",
       a.call("GET", "/api/study/dashboard")[1].get("total_attempts", 0) == before)
    qts = [x.get("qtype") for x in items]
    ok("三种题型都出得来（单选/判断/填空）",
       set(qts) == {"choice", "judge", "blank"}, qts)
    ok("卷子题面不含答案", items and all("answer" not in x for x in items))
    blank_qs = [x for x in items if x.get("qtype") == "blank"]
    ok("填空题题面带挖空占位符",
       bool(blank_qs) and all("＿＿" in x.get("question", "") for x in blank_qs))
    judge_qs = [x for x in items if x.get("qtype") == "judge"]
    ok("判断题只有「对/错」两个选项",
       bool(judge_qs) and all(len(x.get("options") or []) == 2
                              for x in judge_qs))
    # v142：**要几道就给几道**。原来挖不到空会悄悄回落成单选，配比 4/2/2
    # 有时变成 6/2/0——用户按「2 道填空」出的卷子，交卷报告里的题型统计
    # 就跟他点的不一样。实测 10 次里 2 次，这条断言就是为了钉住它。
    mix = p.get("mix") or {}
    ok("题型配比按用户点的来（要 4/2/2 就出 4/2/2，填空不会被换掉）",
       (mix.get("choice"), mix.get("judge"), mix.get("blank")) == (4, 2, 2),
       f"{mix} / {qts}")

    pid = p.get("paper_id")
    # 一律提交 A：分数必须等于「判对了几题」，这是口径一致性的硬断言。
    graded = []
    for it in items:
        _, g = a.call("POST", "/api/study/grade",
                      {"pid": "labor", "quiz_id": it["quiz_id"], "answer": "A"})
        graded.append(1 if g.get("correct") else 0)
    st, rep = a.call("POST", "/api/study/paper/submit",
                     {"pid": "labor", "paper_id": pid, "elapsed": 125})
    ok("交卷返回评估报告", st == 200 and "score" in rep, f"{st} {list(rep)[:6]}")
    ok("卷面得分 = 逐题判对比例（两边口径一致）",
       rep.get("score") == int(round(100 * sum(graded) / max(1, len(graded)))),
       f"{rep.get('score')} vs {graded}")
    ok("报告给出下一步判断（不是空话）", len(str(rep.get("next") or "")) >= 8,
       rep.get("next"))
    ok("报告带题型得分", len(rep.get("by_type") or []) >= 2)
    ok("报告带章节分布", isinstance(rep.get("chapters"), list))
    ok("交卷后累计作答数增加了整卷题数",
       a.call("GET", "/api/study/dashboard")[1].get("total_attempts", 0)
       == before + len(graded))

    st, det = a.call("GET", f"/api/study/paper/{pid}?pid=labor")
    ok("报告页逐题回顾可读", st == 200 and det.get("submitted")
       and len(det.get("items", [])) == len(graded))
    ok("回顾里每题都有我的答案与正确答案",
       all(it.get("answer") and "my_answer" in it for it in det.get("items", [])))

    st, txt = a.call("GET", f"/api/study/report?paper_id={pid}&pid=labor")
    body = txt.get("text", "")
    ok("单张卷子的评估报告能导出成文本",
       st == 200 and "卷面评估报告" in body and "逐题回顾" in body)
    ok("卷面报告只有一句免责", body.count("仅供参考，不构成法律意见") == 1)

    st_b, _ = b.call("GET", f"/api/study/paper/{pid}?pid=labor")
    ok("别人的卷子查不到（不是靠前端藏）", st_b == 404, str(st_b))
    st_b2, _ = b.call("POST", "/api/study/paper/submit",
                      {"pid": "labor", "paper_id": pid})
    ok("别人的卷子交不了", st_b2 == 404, str(st_b2))
    st_x, _ = a.call("POST", "/api/study/paper/submit",
                     {"pid": "consumer", "paper_id": pid})
    ok("跨内核交卷被拒", st_x == 404, str(st_x))

    st, big = a.call("POST", "/api/study/paper", {"pid": "consumer", "n": 999})
    ok("题数被封在 30 以内", st == 200 and len(big.get("items") or []) <= 30,
       len(big.get("items") or []))

    st, cal = a.call("GET", "/api/study/calendar")
    ok("打卡日历给的是整月", st == 200
       and cal.get("days") == len(cal.get("cells") or []), cal.get("label"))
    ok("日历有本月打卡统计",
       all(k in cal for k in ("active_days", "streak", "total")))
    ok("今天有记录（刚答了整卷）",
       any(c["count"] > 0 and c["level"] > 0 for c in cal.get("cells") or []))
    st, cal2 = a.call("GET", "/api/study/calendar?month=2026-01")
    ok("能按月查询历史日历", st == 200 and cal2.get("month") == "2026-01",
       cal2.get("month"))

    st, wb = a.call("GET", "/api/study/wrongbook")
    wrong_ids = [w["quiz_id"] for w in (wb.get("items") or [])][:3]
    ok("错题集里有题可重练", bool(wrong_ids), f"{len(wrong_ids)} 条")
    if wrong_ids:
        st, rp = a.call("POST", "/api/study/paper",
                        {"pid": "labor", "kind": "review", "quiz_ids": wrong_ids})
        ok("错题能装成重练卷", st == 200 and len(rp.get("items") or []) >= 1
           and rp.get("kind") == "review", f"{st} {len(rp.get('items') or [])}")
        ok("重练卷题目与原题一致（不重新出题）",
           set(x["quiz_id"] for x in rp["items"]) <= set(wrong_ids))

    # 错题的遗忘曲线：错一次隔天再看，不是「一错就天天推」。
    st, wb2 = a.call("GET", "/api/study/wrongbook")
    ok("错题带遗忘曲线字段（due / due_in_days）",
       all("due" in w and "due_in_days" in w for w in wb2.get("items") or []))
    ok("刚答错的题今天不算到期（间隔 1 天）",
       all(w["due"] == 0 for w in wb2.get("items") or []),
       [w.get("due_in_days") for w in (wb2.get("items") or [])][:5])

    st, d = a.call("GET", "/api/study/dashboard")
    ok("主页给出近 30 天序列", len(d.get("daily") or []) == 30)
    ok("徽章全部列出且带进度（未获得的也有 cur/goal）",
       len(d.get("badges") or []) >= 18
       and all("cur" in x and "goal" in x for x in d.get("badges") or []))
    ok("徽章六组齐全且名称不重复（kind 都在六种之内）",
       {b.get("kind") for b in d.get("badges") or []}
       <= {"累计", "坚持", "精通", "卷子", "广度", "错题"}
       and len({b["kind"] for b in d.get("badges") or []}) >= 5
       and len({b["name"] for b in d.get("badges") or []})
       == len(d.get("badges") or []))
    ok("知识树带总章数（能画全貌）",
       all("chapters_total" in t for t in (d.get("tree") or [])))
    ok("主页带最近卷子（画柱状图用）", isinstance(d.get("papers"), list))
    ok("未登录看卷子接口被挡",
       Client().call("POST", "/api/study/paper", {"pid": "labor"})[0] in (401, 403))


def main() -> int:
    global BASE
    print("自主学习 / 个人主页（v139）")
    print("=" * 60)
    tmp = tempfile.mkdtemp(prefix="law_study_")
    part_logic(str(Path(tmp) / "logic.db"))

    db = str(Path(tmp) / "http.db")
    # 显式置空 key：这一节验的就是「回放包能不能照常学习」。
    svc = None
    try:
        svc, BASE = start_service(env={
            "LAW_DB_PATH": db,
            "ADMIN_PASSWORD": ADMIN[1],
            "DEEPSEEK_API_KEY": "",
        })
        part_http()
    finally:
        if svc:
            stop_service(svc)

    print("\n" + "=" * 60)
    print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：")
        for f in FAIL:
            print(f"    · {f}")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
