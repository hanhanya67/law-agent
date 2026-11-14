# -*- coding: utf-8 -*-
"""
会话搜索 / 用户反馈 / 运行时状态：三块的离线回归。

为什么这三块放一个脚本：它们的共同点是「后端有逻辑、界面上看不见细节」——
搜索的转义与隔离、反馈的改判与撤销、状态的「没调用过就别报绿」，
这些都不是截图能证明的，也不是点两下能看出来的。

跑法：./.venv/Scripts/python.exe scripts/test_feedback.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import health                                  # noqa: E402
from app.memory import MemoryStore                      # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

PASS, FAIL, SKIP = [], [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))


def skip(name: str, why: str) -> None:
    """这条断言在当前环境下不成立，显式跳过，而不是硬凑成通过。

    为什么要它：运行态相关的断言只有在**配置了 key** 时才成立——没配 key
    时 `LLM_ENABLED` 为假，接口恒报 `disabled` / `replay`，而那本身就是
    正确行为。早先没有这个概念，全新环境（没有 .env）里这套件会红 7 条，
    看起来像「包坏了」，实际是「断言假设了有 key」。
    跳过比放宽断言（比如允许 disabled 也算过）诚实——后者会让真出问题时
    也绿。跳过了几条会在结尾单独列出来，不会藏进通过数里。
    """
    SKIP.append(name)
    print(f"  [SKIP] {name}  → {why}")


def llm_enabled() -> bool:
    """当前环境有没有配置模型。没有时运行态恒为 disabled/replay，
    与「配置之后怎么变」有关的那几条断言应当跳过。"""
    sys.path.insert(0, str(ROOT))
    from app.config import LLM_ENABLED  # noqa: E402
    return bool(LLM_ENABLED)


def fresh() -> MemoryStore:
    d = tempfile.mkdtemp()
    return MemoryStore(Path(d) / "t.db")


# ---------------------------------------------------------------- 会话搜索
def test_search() -> None:
    print("\n会话搜索：")
    m = fresh()
    m.ensure_session("a1", 1)
    m.ensure_session("a2", 1)
    m.ensure_session("b1", 2)
    m.save_message("a1", "user", "公司扣了我 100% 的工资，合法吗")
    m.save_message("a1", "assistant", "扣光工资通常不合法，但要看是否有合法扣减理由")
    m.save_message("a2", "user", "加班费怎么算")
    m.save_message("a2", "assistant", "工作日加班按 150% 支付")
    m.save_message("b1", "user", "别人的加班问题")
    m.save_message("b1", "assistant", "别人会话里的答案")

    r = m.search_sessions(1, "扣光")
    ok("搜到自己会话的答案正文", [x["session_id"] for x in r] == ["a1"],
       [x["session_id"] for x in r])
    ok("片段带上下文而不是只给一个词",
       "扣光" in r[0]["snippet"] and len(r[0]["snippet"]) > len("扣光"),
       r[0]["snippet"][:40])
    ok("标题取该会话首个提问", r[0]["title"].startswith("公司扣了我"), r[0]["title"][:20])

    ok("**搜不到别人的会话**", m.search_sessions(1, "别人") == [])
    ok("换个用户才搜得到", [x["session_id"] for x in m.search_sessions(2, "别人")] == ["b1"])

    ok("空关键词不返回全量", m.search_sessions(1, "") == [])
    ok("纯空格也算空", m.search_sessions(1, "   ") == [])

    # 通配符必须当字面量：不转义的话搜 % 会命中**所有**会话
    only_pct = {x["session_id"] for x in m.search_sessions(1, "%")}
    ok("搜 % 只命中真的含 % 的会话（a1 的 100%、a2 的 150%）",
       only_pct == {"a1", "a2"}, sorted(only_pct))
    ok("搜 _ 不会命中所有会话", m.search_sessions(1, "_") == [],
       m.search_sessions(1, "_"))
    ok("搜 100% 命中字面量",
       [x["session_id"] for x in m.search_sessions(1, "100%")] == ["a1"])

    multi = m.search_sessions(1, "不合法")
    ok("同一会话多处命中合并成一条", len(multi) == 1, len(multi))
    ok("记录命中次数", multi[0]["hits"] >= 1, multi[0]["hits"])


# ---------------------------------------------------------------- 用户反馈
def test_feedback() -> None:
    print("\n用户反馈：")
    m = fresh()
    m.ensure_session("s1", 1)

    ok("初始没有票", m.session_votes("s1") == {})
    m.save_feedback("s1", "公司辞退我怎么办", 1, 7, "up", "答案一")
    ok("投票落库", m.session_votes("s1") == {"公司辞退我怎么办": "up"})
    st = m.feedback_stats()
    ok("统计计入好评", (st["n"], st["up"], st["down"]) == (1, 1, 0), st)

    m.save_feedback("s1", "公司辞退我怎么办", 1, None, "down", "答案二")
    ok("同一问可以改判，不会多出一条", m.feedback_stats()["n"] == 1,
       m.feedback_stats()["n"])
    ok("改判后是差评", m.session_votes("s1") == {"公司辞退我怎么办": "down"})
    ok("改判保留原先的 turn_id（后传 None 不能把它冲掉）",
       m.feedback_stats(recent=1)["recent_bad"][0]["answer"] == "答案二")
    bad = m.feedback_stats(recent=3)["recent_bad"]
    ok("差评进 badcase 池，带问题和答案", bad and bad[0]["question"].startswith("公司辞退"),
       bad[0]["question"][:12] if bad else "空")

    m.save_feedback("s1", "公司辞退我怎么办", 1, None, "", "")
    ok("空值 = 撤销，不是存一条空票", m.session_votes("s1") == {})
    ok("撤销后统计归零", m.feedback_stats()["n"] == 0)

    for i in range(3):
        m.save_feedback("s1", f"问题{i}", 1, None, "up", "x")
    ok("多问各自一票", m.feedback_stats()["n"] == 3, m.feedback_stats()["n"])
    ok("好评率按票数算", m.feedback_stats()["up_rate"] == 1.0)

    long_a = "长" * 9000
    m.save_feedback("s1", "超长答案", 1, None, "down", long_a)
    row = [x for x in m.feedback_stats(recent=20)["recent_bad"]
           if x["question"] == "超长答案"][0]
    ok("超长答案被截断（这张表是索引，不是仓库）", len(row["answer"]) <= 4000,
       len(row["answer"]))


# ---------------------------------------------------------------- 状态
def test_health() -> None:
    print("\n运行时状态：")
    # 直接改模块内部状态做重置，不依赖执行顺序
    health._STATE.update({"last_ok": 0.0, "last_err": 0.0, "err_kind": "",
                          "err_msg": "", "calls": 0, "errors": 0})
    s = health.snapshot()
    ok("没调用过 → calls 为 0（上层据此报「尚未调用」而不是「正常」）",
       s["calls"] == 0, s["calls"])

    health.note_ok()
    s = health.snapshot()
    ok("成功一次 → 状态正常", s["ok"] is True and s["calls"] == 1, s)

    health.note_error(402, '{"error":"余额不足"}')
    s = health.snapshot()
    ok("402 判为余额不足", s["ok"] is False and s["kind"] == "quota", s["kind"])
    ok("错误文案可读", s["text"] == "余额不足", s["text"])
    ok("带上供应商原文（截断）", "余额不足" in s["detail"], s["detail"][:30])

    health.note_ok()
    ok("之后成功一次即恢复（不做连续 N 次平滑）",
       health.snapshot()["ok"] is True)

    health.note_error(401, "unauthorized")
    ok("401 判为密钥无效", health.snapshot()["kind"] == "auth")
    health.note_ok()
    health.note_error(kind="network", msg="timed out")
    ok("网络类错误单独归类", health.snapshot()["kind"] == "network")
    health.note_ok()
    health.note_error(503, "bad gateway")
    ok("5xx 判为服务端异常", health.snapshot()["kind"] == "server")


def test_status_payload() -> None:
    """接口层的键冲突：snap 里也有 text/detail，铺开顺序错了就会把文案冲掉。"""
    print("\n/api/status 组装：")
    sys.path.insert(0, str(ROOT))
    import app.server as srv  # noqa: E402

    # 没配 key 时 health_status() 恒为 replay，下面三条断言的是「开了模型之后
    # 运行态怎么变」，在没有 key 的环境里它们没有意义，跳过而不是放宽。
    live = llm_enabled()

    health._STATE.update({"last_ok": 0.0, "last_err": 0.0, "err_kind": "",
                          "err_msg": "", "calls": 0, "errors": 0})
    d = srv.health_status(user={"id": 1})
    if live:
        ok("state 与 text 同时存在且不自相矛盾",
           d["state"] == "idle" and d["text"] == "尚未调用", d)
    else:
        skip("state 与 text 同时存在且不自相矛盾", "未配置 key，运行态恒为 replay")

    health.note_error(402, "no money")
    d = srv.health_status(user={"id": 1})
    if live:
        ok("失败时文案是错误原因，不是空串",
           d["state"] == "error" and d["text"] == "余额不足", d["text"])
    else:
        skip("失败时文案是错误原因，不是空串", "未配置 key，运行态恒为 replay")

    health.note_ok()
    d = srv.health_status(user={"id": 1})
    if live:
        ok("成功时文案是「模型正常」",
           d["state"] == "ok" and d["text"] == "模型正常", d["text"])
    else:
        skip("成功时文案是「模型正常」", "未配置 key，运行态恒为 replay")
    ok("返回体可 JSON 序列化", json.dumps(d, ensure_ascii=False)[:1] == "{")


def test_meta_live() -> None:
    """登录页那行「LLM 在线」的诚实度。

    配置开了不等于现在能用：余额扣光、key 被停都发生在启动之后。
    只按开关写「在线」，用户注册进来看见每条答案都带「回放」，
    那就是一句假话——比不写更糟。
    """
    print("\n/api/meta 的运行态：")
    sys.path.insert(0, str(ROOT))
    import app.server as srv  # noqa: E402

    # `llm_live()` 在 LLM_ENABLED 为假时直接返回 disabled，不看快照——
    # 下面三条断言的是「配置之后运行态怎么变」，没配 key 时跳过。
    live = llm_enabled()
    if live:
        ok("一次都没调过不写「在线」，写 idle",
           srv.llm_live({"calls": 0, "ok": True})[0] == "idle")
        ok("最近一次失败就报 error，且带上类别",
           srv.llm_live({"calls": 3, "ok": False, "text": "余额不足"})
           == ("error", "余额不足"))
        ok("只有最近一次成功才报 ok",
           srv.llm_live({"calls": 3, "ok": True})[0] == "ok")
    else:
        for n in ("一次都没调过不写「在线」，写 idle",
                  "最近一次失败就报 error，且带上类别",
                  "只有最近一次成功才报 ok"):
            skip(n, "未配置 key，llm_live 恒为 disabled")

    d = srv.meta()
    ok("meta 同时给出配置态与运行态", {"mode", "llm_live"} <= set(d), sorted(d))
    ok("运行态取值在约定的四个里",
       d["llm_live"] in ("ok", "idle", "error", "disabled"), d["llm_live"])
    # 公开接口：只给「余额不足」这种类别，不给供应商返回的原文
    ok("公开页不带供应商原文", len(str(d.get("llm_live_note", ""))) <= 12,
       d.get("llm_live_note"))

    # 判定只留一份：/api/status 与 /api/meta 面对同一份快照不能给出矛盾的结论。
    # 两边各写一条 if 链时，改一处忘另一处就是「徽章说在线、登录页说回放」。
    trans = {"ok": "ok", "idle": "idle", "error": "error", "replay": "disabled"}
    bad = []
    for setup in ("idle", "error", "ok"):
        health._STATE.update({"last_ok": 0.0, "last_err": 0.0, "err_kind": "",
                              "err_msg": "", "calls": 0, "errors": 0})
        if setup == "error":
            health.note_error(402, "no money")
        elif setup == "ok":
            health.note_ok()
        st = srv.health_status(user={"id": 1})["state"]
        live = srv.llm_live(health.snapshot())[0]
        if trans.get(st) != live:
            bad.append(f"{setup}: {st}/{live}")
    ok("status 与 meta 对同一份快照结论一致", not bad, "、".join(bad))


def test_degrade_alert() -> None:
    """模型失败时，管理员概览的告警区必须自己跳出来。

    概览那行小字写着「LLM 在线」时，管理员看不出欠费；写清楚了但埋在一堆
    统计口径说明中间，也一样看不见。所以这条要么进告警区，要么不算说清楚。
    """
    print("\n管理员告警（模型失败）：")
    sys.path.insert(0, str(ROOT))
    import app.server as srv  # noqa: E402

    a = srv.degrade_alert("error", "余额不足")
    ok("失败时给 bad 级告警", bool(a) and a["level"] == "bad", a)
    ok("标题直接说「在回放」", bool(a) and "回放" in a["title"],
       (a or {}).get("title"))
    ok("详情带上原因类别", bool(a) and "余额不足" in a["detail"])
    ok("详情给出恢复方式（冷却窗口 + 可以重启）",
       bool(a) and "分钟" in a["detail"] and "重启" in a["detail"],
       (a or {}).get("detail", "")[:30])
    # 文案里的时间不能和真实冷却各写各的——改配置忘了改文案，就是又一次「说话不算数」
    from app.config import LLM_RECOVER_AFTER  # noqa: E402
    want = f"{max(1, LLM_RECOVER_AFTER // 60)} 分钟"
    ok("文案里的冷却时间与配置一致", bool(a) and want in a["detail"],
       f"期望出现 {want}")

    # 把「降级后确实不再调接口」钉死：哨兵客户端一被碰就抛，比看 online 难蒙混。
    from app.llm import LLM  # noqa: E402

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("降级后仍在调用接口")

    llm = LLM()
    llm.online = False
    llm._offline_since = time.time()          # 刚降级，冷却期内
    llm._client = _Boom()
    try:
        llm.classify_intent("公司拖欠我三个月工资怎么办", "")
        quiet = True
    except AssertionError:
        quiet = False
    ok("冷却期内不发任何试探调用", quiet and llm.online is False,
       f"online={llm.online}")
    ok("正常 / 未调用 / 未配置都不告警",
       all(srv.degrade_alert(x, "") is None
           for x in ("ok", "idle", "disabled")))


def test_recover() -> None:
    """降级 → 冷却 → 重试 → 又失败：这条链每步都要有明确行为。

    为什么单独测：这条链一旦走错，代价是**每一轮都白花一次失败调用**
    （热循环），或者**永远不恢复**（原来就是这样）。两种都不像 bug，
    一个像「怎么这么慢」，一个像「充值了怎么还不好」。
    """
    print("\n降级后自动恢复：")
    sys.path.insert(0, str(ROOT))
    from app.config import LLM_RECOVER_AFTER  # noqa: E402
    from app.llm import LLM  # noqa: E402

    class _Fatal(Exception):
        fatal = True

    llm = LLM()
    llm.online = False
    llm._offline_since = time.time()
    ok("冷却期内不重试", llm.maybe_recover() is False and llm.online is False)

    llm._offline_since = time.time() - (LLM_RECOVER_AFTER + 1)
    # `maybe_recover()` 开头就是 `if ... not LLM_ENABLED: return False`——
    # 没配 key 时「恢复」这件事根本不存在，这条断言无从谈起。
    if llm_enabled():
        ok("冷却期过后置回在线",
           llm.maybe_recover() is True and llm.online is True)
        ok("刚恢复不会连着再触发一次", llm.maybe_recover() is False)
    else:
        skip("冷却期过后置回在线", "未配置 key，maybe_recover 恒不试探")
        skip("刚恢复不会连着再触发一次", "同上")

    llm._downgrade(_Fatal("402"))
    ok("恢复后又失败会重新降级", llm.online is False)
    ok("失败当下不立刻重试（冷却从此刻重算）", llm.maybe_recover() is False)

    # 配置里根本没开模型时不该试探：那不是「故障」，是「就是这么设计的」
    import app.llm as llm_mod  # noqa: E402
    saved = llm_mod.LLM_ENABLED
    try:
        llm_mod.LLM_ENABLED = False
        llm._offline_since = 0.0
        ok("没配置 key 时不试探", llm.maybe_recover() is False)
    finally:
        llm_mod.LLM_ENABLED = saved


def main() -> int:
    print("=" * 58)
    print("  搜索 / 反馈 / 状态")
    print("=" * 58)
    test_search()
    test_feedback()
    test_degrade_alert()
    test_recover()
    test_health()
    test_status_payload()
    test_meta_live()
    print("\n" + "=" * 58)
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        for f in FAIL:
            print("  失败：" + f)
    if SKIP:
        # 跳过数单列，不混进通过数：混进去就是「看着全绿，其实有 7 条没跑」
        print(f"  跳过 {len(SKIP)} 条（未配置 key，运行态类断言不成立）：")
        for s in SKIP:
            print("    · " + s)
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
