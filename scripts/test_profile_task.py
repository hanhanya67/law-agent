# -*- coding: utf-8 -*-
"""个人档案 + 任务隔离 + 按任务聚合申请书的端到端测试。

为什么不调函数而是打真实 HTTP：
  这三块里真正容易坏的是「归属校验」和「前端传的 id 属于谁」——调
  server.task_detail() 是在测那个函数，不是测这条防线。用 urllib +
  CookieJar 模拟两个账号，才能证明「A 看不到 B 的任务」真的成立。

为什么用临时库：
  这个脚本要建任务、写会话、改档案。跑在 data/law.db 上等于往用户的
  真实咨询记录里塞测试数据，而且还删不干净。

跑法：
    ./.venv/Scripts/python.exe scripts/test_profile_task.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _svc import start_service, stop_service   # noqa: E402
from app.memory import MemoryStore            # noqa: E402
from app.schemas import CaseSlots             # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

BASE = ""
ADMIN = ("admin", "Admin123456")
PASS: list[str] = []
FAIL: list[str] = []


def ok(tag: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(tag)
    print(f"  [{'PASS' if cond else 'FAIL'}] {tag}" + (f"  {extra}" if extra else ""))


class Client:
    """一个「浏览器」。cookie 独立，才能模拟两个账号。"""

    def __init__(self) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _csrf(self) -> str:
        for c in self.cj:
            if c.name == "law_csrf":
                return c.value
        return ""

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        t = self._csrf()
        if t:
            req.add_header("x-csrf-token", t)
        try:
            resp = self.op.open(req)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except Exception:                     # noqa: BLE001
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}
        raw = resp.read() or b""
        try:
            return resp.status, (json.loads(raw) if raw else {})
        except Exception:                         # noqa: BLE001
            return resp.status, {"text": raw[:120].decode("utf-8", "replace")}

    def me(self) -> dict:
        return (self.call("GET", "/api/auth/me")[1] or {}).get("user") or {}


def part_profile(a: Client, uid: int) -> None:
    print("\n== 个人档案 ==")
    st, d = a.call("GET", "/api/auth/me")
    ok("新账号 onboarding=pending", st == 200 and d.get("onboarding") == "pending",
       f"onboarding={d.get('onboarding')}")
    ok("新账号 filled 为空", d.get("profile_filled") == [])

    st, d = a.call("GET", "/api/profile")
    ok("GET /api/profile 返回全字段占位",
       st == 200 and d.get("real_name") == "" and "labels" in d and "limits" in d)

    # 先跳过，再回来补 —— 顺序反过来也要成立
    st, d = a.call("POST", "/api/profile/skip")
    ok("跳过置为 skipped", st == 200
       and (d.get("profile") or {}).get("onboarding") == "skipped")

    prof = {
        "real_name": "张三", "id_card": "330102199001011234",
        "phone": "13800138000", "address": "杭州市西湖区文一西路 1 号",
        "region": "杭州市西湖区",
        "company_name": "杭州某某科技有限公司",
        "company_code": "91330100MA2XXXXX1A",
        "company_address": "杭州市滨江区江南大道 8 号",
        "company_legal_rep": "李四", "commission": "杭州市",
    }
    st, d = a.call("PUT", "/api/profile", {"profile": prof, "onboarding": "done"})
    ok("保存档案 -> done", st == 200
       and (d.get("profile") or {}).get("onboarding") == "done", f"status={st}")

    st, g = a.call("GET", "/api/profile")
    ok("读回姓名与身份证全号",
       g.get("real_name") == "张三" and g.get("id_card") == "330102199001011234",
       f"id_card={g.get('id_card')}")
    ok("filled 覆盖 10 项", len(g.get("filled") or []) == 10,
       f"n={len(g.get('filled') or [])}")

    st, d = a.call("GET", "/api/auth/me")
    ok("done 之后不再弹窗", d.get("onboarding") == "done")

    st, d = a.call("PUT", "/api/profile", {"profile": {"real_name": "长" * 31}})
    ok("超长姓名被拒", st == 400, f"code={d.get('code')}")
    st, d = a.call("PUT", "/api/profile", {"profile": {"nope": "x"}})
    ok("未知字段被拒", st == 400, f"code={d.get('code')}")
    st, d = a.call("PUT", "/api/profile", {"profile": {"phone": "1\n2"}})
    ok("换行被拒", st == 400, f"code={d.get('code')}")

    # 部分更新不能把别的字段清掉
    st, d = a.call("PUT", "/api/profile", {"profile": {"phone": "13900139000"}})
    ok("改一个字段不动其他字段",
       (d.get("profile") or {}).get("phone") == "13900139000"
       and (d.get("profile") or {}).get("real_name") == "张三")

    # 后台必须拿不到全号。这条是硬边界：多一个「管理员专用全号」字段，
    # 迟早会被某个导出脚本顺手打进 CSV。
    # 注意这层返回是 {"user": {...}}，不是裸字典。直接 det["profile"] 恒为 None，
    # 而 all(...) 在空 dict 上恒真——那种断言会假装通过，所以下面加了 bool(prof)。
    st, det = a.call("GET", f"/api/admin/users/{uid}")
    bprof = ((det.get("user") or {}).get("profile") or {}) \
        if isinstance(det, dict) else {}
    ok("后台详情带档案", st == 200 and bprof.get("real_name") == "张三",
       f"st={st} keys={sorted(bprof)}")
    ok("后台身份证已脱敏", bprof.get("id_card") == "330102********1234",
       str(bprof.get("id_card")))
    ok("后台档案不含全号字段",
       bool(bprof) and all("330102199001011234" not in str(v)
                           for v in bprof.values()))
    ok("后台详情带任务数", "n_tasks" in (det.get("user") or {}),
       f"n_tasks={(det.get('user') or {}).get('n_tasks')}")


def part_tasks(a: Client, b: Client) -> tuple[str, str]:
    print("\n== 任务 CRUD 与隔离 ==")
    st, d = a.call("POST", "/api/tasks", {"title": "某某公司拖欠工资"})
    ok("新建任务", st == 200
       and (d.get("task") or {}).get("title") == "某某公司拖欠工资")
    t1 = (d.get("task") or {}).get("task_id")

    st, d = a.call("POST", "/api/tasks", {"title": "社保未缴"})
    t2 = (d.get("task") or {}).get("task_id")
    ok("建第二个任务", st == 200 and bool(t2))

    st, d = a.call("GET", "/api/tasks")
    ok("列表含两个任务", st == 200 and len(d.get("items") or []) == 2,
       f"n={len(d.get('items') or [])}")

    st, d = a.call("PATCH", f"/api/tasks/{t1}", {"title": "拖欠工资与加班费"})
    ok("改名生效",
       st == 200 and (d.get("task") or {}).get("title") == "拖欠工资与加班费")

    st, d = a.call("PATCH", f"/api/tasks/{t1}", {"status": "archived"})
    ok("归档生效", st == 200 and (d.get("task") or {}).get("status") == "archived")
    st, d = a.call("GET", "/api/tasks")
    ok("归档后默认列表只剩 1 个", len(d.get("items") or []) == 1,
       f"n={len(d.get('items') or [])}")
    st, d = a.call("GET", "/api/tasks?include_archived=1")
    ok("显式要归档能看到 2 个", len(d.get("items") or []) == 2)
    a.call("PATCH", f"/api/tasks/{t1}", {"status": "active"})

    st, d = a.call("PATCH", f"/api/tasks/{t1}", {"status": "deleted"})
    ok("非法状态被拒", st == 400, f"code={d.get('code')}")

    # 越权：另一账号既看不到也改不了
    st, _ = b.call("GET", f"/api/tasks/{t1}")
    ok("别人看不到我的任务详情", st == 404)
    st, _ = b.call("PATCH", f"/api/tasks/{t1}", {"title": "hack"})
    ok("别人改不了我的任务", st == 403)
    st, d = b.call("GET", "/api/tasks")
    ok("别人的任务列表是空的", len(d.get("items") or []) == 0)
    st, _ = b.call("POST", "/api/doc/arbitration", {"task_id": t1})
    ok("别人不能拿我的任务生成申请书", st == 403)
    return t1, t2


def part_aggregate(a: Client, store: MemoryStore, uid: int,
                   t1: str, t2: str) -> None:
    print("\n== 按任务聚合 ==")
    store.ensure_session("sess_a1", uid, t1)
    store.ensure_session("sess_a2", uid, t1)
    store.save_message("sess_a1", "user",
                       "我从2023年6月入职杭州某某科技有限公司，一直没签书面劳动合同，月薪8000元。")
    # 夹具里也要有助手回复：真实会话一定是成对的。只塞用户消息的话
    # n_messages 和 n_turns 恰好相等，「把消息条数当轮数」这个 bug 就测不出来。
    store.save_message("sess_a1", "assistant", "（示例回答：未签合同可主张二倍工资差额。）")
    store.save_message("sess_a1", "user", "你好")
    store.save_message("sess_a2", "user",
                       "公司从2024年3月开始拖欠工资，到5月共欠三个月工资24000元。")
    store.save_message("sess_a2", "assistant", "（示例回答：拖欠工资可向劳动监察投诉。）")
    store.save_message("sess_a2", "user", "那我能拿多少？")
    store.save_message("sess_a2", "user",
                       "2024年5月20日主管口头通知我不用来了，没有书面通知。")
    store.save_message("sess_a2", "user", "我还有微信聊天记录和工资流水可以作证。")
    store.save_slots("sess_a1", CaseSlots(start_date="2023-06", monthly_wage=8000,
                                          has_contract=False))
    store.save_slots("sess_a2", CaseSlots(end_date="2024-05",
                                          leave_reason="公司口头辞退"))
    store.ensure_session("sess_b1", uid, t2)
    store.save_message("sess_b1", "user", "公司从2022年就没给我缴社保，我自己垫钱交的。")

    st, d = a.call("GET", f"/api/tasks/{t1}")
    t = d.get("task") or {}
    ok("两个会话都算进任务", t.get("n_sessions") == 2, f"n={t.get('n_sessions')}")
    ok("六轮用户提问", t.get("n_turns") == 6, f"n={t.get('n_turns')}")
    sl = d.get("slots") or {}
    ok("要素跨会话合并",
       sl.get("start_date") == "2023-06" and sl.get("monthly_wage") == 8000
       and sl.get("leave_reason") == "公司口头辞退" and sl.get("end_date") == "2024-05",
       str(sorted(sl.keys())))

    st, doc = a.call("POST", "/api/doc/arbitration", {"task_id": t1})
    ok("生成成功", st == 200)
    txt = doc.get("text") or ""
    src = doc.get("source") or {}
    ok("来源覆盖整个任务",
       src.get("task_id") == t1 and src.get("n_sessions") == 2, str(src))
    # 轮数按「用户提问」算，不是消息条数。这个字段曾经把 n_messages（含回答）
    # 贴成「轮」，10 条消息写成 10 轮，而任务卡片上写的是 5 轮——用户第一反应
    # 是「它多算了一半」。所以这里同时断言它等于用户消息数、且不等于消息总数。
    ok("来源轮数按提问算（不是消息条数）",
       src.get("n_turns") == 6 and src.get("n_messages") != src.get("n_turns"),
       str(src))
    ok("档案填入申请人姓名", "姓　　名：张三" in txt)
    ok("档案填入单位全称", "单位名称：杭州某某科技有限公司" in txt)
    ok("档案填完则 ready=True", doc.get("ready") is True)
    ok("任务要素填入入职时间", "2023年6月入职" in txt)

    stmts = doc.get("statements") or []
    ok("摘录合并且有 4 条以上", len(stmts) >= 4, f"n={len(stmts)}")
    texts = " ".join(s["text"] for s in stmts)
    ok("含第一段会话的事实", "一直没签书面劳动合同" in texts)
    ok("含第二段会话的事实", "口头通知我不用来了" in texts)
    ok("寒暄未进摘录", "你好" not in texts)
    ok("纯提问未进摘录", "那我能拿多少" not in texts)
    ok("另一任务内容未串入", "缴社保" not in texts and "我自己垫钱" not in texts)
    ok("摘录带日期", all(s.get("date") for s in stmts))
    ok("争议经过已替换为空模板", "按时间顺序摘录" in txt)

    st, d2 = a.call("POST", "/api/doc/arbitration", {"session_id": "sess_a2"})
    ok("session_id 回退模式仍可用",
       st == 200 and (d2.get("source") or {}).get("n_sessions") == 1,
       str(d2.get("source")))

    st, d = a.call("GET", "/api/tasks")
    # v104：未归属会话不再单列，而是由 /api/tasks 自动收进「历史咨询」任务。
    # 原来这里断的是 isinstance(list)——接口永远返回 [] 它也绿，等于没测。
    ok("任务列表不再返回未分类条目", d.get("unassigned") == [], d.get("unassigned"))

    # 放在最后：这一步会在 t1 里多开一个会话，前面的 n_sessions==2 就不成立了。
    # 验的是「同一任务里新开的会话能不能接着用之前记下的案情」——
    # 只读会话级槽位的话，用户点「新对话」就得把入职时间再说一遍，
    # 而任务面板上明写着「多次咨询共享案情要素」。承诺和实际不一致，
    # 比不承诺更糟：用户会以为自己记错了。
    store.ensure_session("sess_a3", uid, t1)      # 空槽位的新会话
    # /api/chat 是 SSE 流，这里不等内容，只等它跑完（call 会把整条流读完）。
    # 这一个问题要跑完整条检索链，约 20 秒——本套件从 3 秒变成 25 秒。
    # 仍然值：继承逻辑写在 run() 靠前的位置，用真问题才能保证它没被挪到
    # 意图路由之后（用「你好」走寒暄短路，挪走了也测不出来）。
    a.call("POST", "/api/chat", {"question": "那我这种情况到底能拿多少经济补偿？",
                                 "session_id": "sess_a3", "task_id": t1})
    st, s3 = a.call("GET", "/api/chat/session/sess_a3")
    sl3 = s3.get("slots") or {}
    ok("新会话继承任务要素",
       sl3.get("start_date") == "2023-06" and sl3.get("monthly_wage") == 8000,
       str(sorted(sl3.items())))
    # 落盘也要有：档案面板读的是会话级槽位，不写下去用户会看到
    # 「对话里明明记得，档案面板却是空的」。
    ok("继承的要素写进了会话", "start_date" in sl3)
    # 档案里的地区要能被用上。填过档案还被问「您在哪个城市」，是最容易
    # 让人得出「它什么都不记得」的那种细节，而且地区决定三倍封顶和当地口径。
    ok("档案里的地区进了槽位（不用再问一遍「您在哪个城市」）",
       bool(sl3.get("region")), str(sl3.get("region")))

    # 跨会话证据：同一个案子问到第三遍，前面查过的条文要留在任务上。
    # 不留的话每一轮都从零检索，等于每轮都要重新碰一次「这次能不能命中」的运气。
    ev_task = store.load_task_evidence(t1, limit=200)
    ok("任务上记下了这个案子查过的条文", len(ev_task) >= 1, f"{len(ev_task)} 条")
    ok("记的是整条正文，不是只存了个 id",
       bool(ev_task) and len((ev_task[0] or {}).get("content") or "") > 10)

    # 存储层的两条：同一条文重复记只留一行；正文以最后一次为准。
    # 这两条不依赖模型，所以在上面那条万一被拒答带红时仍然成立。
    fake = {"article_id": "测试法_1", "law_name": "测试法", "article_no": "1",
            "article_no_cn": "第一条", "content": "第一版正文" * 3}
    store.save_task_evidence(t1, [fake])
    store.save_task_evidence(t1, [dict(fake, content="第二版正文" * 3)])
    got = [e for e in store.load_task_evidence(t1, limit=200)
           if (e or {}).get("article_id") == "测试法_1"]
    ok("同一条文重复记只留一行", len(got) == 1, f"{len(got)} 行")
    ok("重复记时以最后一次正文为准",
       bool(got) and got[0].get("content") == "第二版正文" * 3)

    # 会话与 task_id 不一致必须拒。不拒的话要素会从另一个任务继承下来，
    # 再随 save_slots 写回本会话所属的任务——甲案的事实就漏进乙案了。
    st, d = a.call("POST", "/api/chat",
                   {"question": "那我这种情况到底能拿多少经济补偿？",
                    "session_id": "sess_a3", "task_id": t2})
    ok("会话与任务不匹配被拒", st == 400 and d.get("code") == "task_mismatch",
       f"status={st} code={d.get('code')}")


def part_delete(a, b, store: MemoryStore, uid: int, t1: str) -> None:
    """删除会话 / 删除任务（v104）。

    要点不是「接口返回 200」，而是两层：
      1) 子表不留孤儿 —— sessions 删了而 messages/traces 还在的话，
         历史搜索仍会把它们捞出来，点进去是个空会话；
      2) 归属校验 —— 能删别人的记录，等于任何人都能清空别人的案子。
    """
    print("\n== 删除会话 / 任务 ==")

    store.ensure_session("sess_del", uid, t1)
    store.save_message("sess_del", "user", "这条要被删掉")
    st, d = a.call("DELETE", "/api/chat/session/sess_del")
    ok("删除自己的会话返回成功", st == 200 and d.get("ok") is True, f"{st} {d}")
    ok("删完会话本身查不到", store.session_owner("sess_del") is None)
    ok("删完消息也一起清掉（不留孤儿）", store.count_messages("sess_del") == 0)

    # 越权：b 是另一个账号，删 a 的会话必须拒
    st, _ = b.call("DELETE", "/api/chat/session/sess_a1")
    ok("删别人的会话被拒（403）", st == 403, st)
    ok("被拒之后那条会话还在", store.session_owner("sess_a1") is not None)

    st, d = a.call("POST", "/api/tasks", {"title": "待删任务"})
    tdel = ((d.get("task") or {}).get("task_id") or "")
    store.ensure_session("sess_del2", uid, tdel)
    store.save_message("sess_del2", "user", "任务里的内容")
    st, d = a.call("DELETE", f"/api/tasks/{tdel}")
    ok("删除自己的任务返回成功", st == 200 and d.get("ok") is True, f"{st} {d}")
    ok("任务下的会话一并删除", store.session_owner("sess_del2") is None)
    st, _ = a.call("GET", f"/api/tasks/{tdel}")
    ok("删完再查这个任务返回 404", st == 404, st)

    st, _ = b.call("DELETE", f"/api/tasks/{t1}")
    ok("删别人的任务被拒（403）", st == 403, st)

    # v104：未归属会话不再单列，由 /api/tasks 自动收进「历史咨询」任务。
    store.ensure_session("sess_loose", uid, None)
    store.save_message("sess_loose", "user", "没有任务时说的话")
    st, d = a.call("GET", "/api/tasks")
    ok("任务列表不再返回「未分类」条目",
       d.get("unassigned") == [], d.get("unassigned"))
    tid = store.session_task("sess_loose")
    ok("未归属会话被自动收进一个任务", bool(tid), tid)
    ok("自动建的任务叫「历史咨询」",
       any(t.get("task_id") == tid and t.get("title") == "历史咨询"
           for t in (d.get("items") or [])),
       [t.get("title") for t in (d.get("items") or [])])


def part_memory(a, store: MemoryStore, uid: int) -> None:
    """v137：任务记忆与画像事实。

    这三件都容易「看起来做了、其实没落库」：goal 只写内存不写库、
    摘要每轮覆盖成最后一句、画像抽到「我是职工」这种假职业。
    所以逐条查库，不看接口返回值。
    """
    print("\n[v137] 任务记忆（goal / summary / 画像事实）")
    t = store.create_task(uid, "记忆测试")
    tid = t["task_id"]
    ok("任务行自带三个记忆字段", all(k in t for k in
                                ("goal", "summary", "summary_upto")), str(list(t)))
    ok("新任务的 goal 是空串而不是 None", t["goal"] == "", repr(t["goal"]))

    store.set_task_memory(tid, goal="追回 3 个月工资")
    m = store.task_memory(tid)
    ok("goal 落库", m["goal"] == "追回 3 个月工资", m)
    store.set_task_memory(tid, summary="已确认月薪 8000", summary_upto=4)
    m2 = store.task_memory(tid)
    ok("摘要更新不会把 goal 打回空值", m2["goal"] == "追回 3 个月工资", m2)
    ok("摘要与覆盖位置都落库",
       m2["summary"] == "已确认月薪 8000" and m2["summary_upto"] == 4, m2)

    # 画像事实：增 / 覆盖 / 删
    store.add_profile_facts(uid, {"city": {"value": "杭州", "source": "我在杭州"}})
    f = store.list_profile_facts(uid)
    ok("事实落库并带原话", f and f[0]["value"] == "杭州" and "杭州" in f[0]["source"], f)
    store.add_profile_facts(uid, {"city": {"value": "宁波", "source": "我搬到宁波了"}})
    f2 = store.list_profile_facts(uid)
    ok("同一项后来者覆盖（换城市是事实变更）",
       len(f2) == 1 and f2[0]["value"] == "宁波", f2)
    store.add_profile_facts(uid, {"city": {"value": "", "source": ""}})
    ok("传空值等于删除", store.list_profile_facts(uid) == [])

    # 接口层：另一个账号看不到我的事实
    st, d = a.call("GET", "/api/profile/facts")
    ok("事实接口可用", st == 200 and d.get("ok") is True, f"{st} {d}")
    st, d = a.call("POST", "/api/profile/facts",
                   {"key": "职业", "value": "外卖员"})
    ok("不认识的事实字段被拒", st == 400, f"{st} {d}")

    # 抽取规则：宁可不记，不许把立场记成职业
    from app import facts as _f
    ok("抽出城市与职业",
       _f.extract("我在杭州，我是外卖员").get("city", {}).get("value") == "杭州"
       and _f.extract("我在杭州，我是外卖员").get("occupation", {}).get("value") == "外卖员")
    ok("「我是职工」不被记成职业",
       "occupation" not in _f.extract("我是职工，公司不给加班费"))
    ok("「我在一家私企」记成单位性质而不是城市",
       "city" not in _f.extract("我在一家私企上班")
       and _f.extract("我在一家私企上班")["employer_type"]["value"] == "私企")
    ok("寒暄抽不出任何事实", _f.extract("你好") == {})


def main() -> int:
    global BASE
    tmp = tempfile.mkdtemp(prefix="law_v82_")
    db = os.path.join(tmp, "t.db")
    svc, BASE = start_service(env={"LAW_DB_PATH": db,
                                   "ADMIN_USERNAME": ADMIN[0],
                                   "ADMIN_PASSWORD": ADMIN[1]})
    try:
        a, b, r = Client(), Client(), Client()
        for c in (a, b, r):
            c.call("GET", "/login")
        st, _ = a.call("POST", "/api/auth/login",
                       {"username": ADMIN[0], "password": ADMIN[1]})
        if st != 200:
            raise SystemExit(f"管理员登录失败（{st}）")
        uid = int(a.me().get("id") or 0)
        st, _ = r.call("POST", "/api/auth/register",
                       {"username": "worker2", "password": "Test1234ab"})
        st, _ = b.call("POST", "/api/auth/login",
                       {"username": "worker2", "password": "Test1234ab"})
        if st != 200:
            raise SystemExit(f"第二账号登录失败（{st}）")

        part_profile(a, uid)
        t1, t2 = part_tasks(a, b)
        store = MemoryStore(db)
        part_aggregate(a, store, uid, t1, t2)
        part_delete(a, b, store, uid, t1)
        part_memory(a, store, uid)
    finally:
        stop_service(svc)

    print("\n" + "=" * 52)
    print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：")
        for f in FAIL:
            print(f"    · {f}")
    print("=" * 52)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
