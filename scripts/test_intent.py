# -*- coding: utf-8 -*-
"""
寒暄 / 补充信息的分流测试。不联网、不花钱。

为什么单独拎出来：
  这是「问一句你好，回来的却是上一题的答案」那个 bug 的守门员。
  根因是 `_looks_like_supplement` 把寒暄判成了「补充事实」，于是把上一轮的
  问题替换回来重答一遍。它跟有没有 API key 无关——回放模式下同样发生，
  而交付包默认就是回放模式，所以对方拿到手第一句话就会踩到。

  这类判定的危险在于**两种错法代价完全不同**：
    把寒暄误判成补充 → 用户看到一段与问题无关的旧答案（信任当场崩）
    把补充误判成新问题  → 多问一遍，用户说清就行（代价小）
  所以判定要往「宁可漏判」一侧偏，测试也要按这个方向钉。

跑法：
    ./.venv/Scripts/python.exe scripts/test_intent.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import (AgentRunner, _DEGRADED_TEXT, degraded_note,  # noqa: E402,F401
                       _is_greeting, _looks_like_supplement)
from app.config import CORPUS_PATH                          # noqa: E402
from app.llm import LLM                                     # noqa: E402
from app.memory import MemoryStore                          # noqa: E402
from app.retrieval import LawIndex                          # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))


# ------------------------------------------------------------------ 寒暄判定
def test_greeting() -> None:
    print("\n寒暄识别：")
    greetings = ["你好", "您好", "您好！", "你好呀", "hi", "Hi", "HELLO",
                 "在吗", "在不在", "谢谢", "谢谢你", "好的", "嗯", "收到",
                 "早上好", "hello there"]
    bad = [g for g in greetings if not _is_greeting(g)]
    ok("纯寒暄全部认得出来", not bad, "、".join(bad))

    # 反向：带法律问题的句子不能因为开头有「你好」就被当寒暄吞掉。
    # 这是最容易写错的一边——用 startswith 判断的话，「你好，公司拖欠
    # 我工资怎么办」会被整句丢掉，用户以为助手不答正事。
    keep = ["你好，公司拖欠我三个月工资怎么办",
            "谢谢，那我还需要准备什么材料",
            "在吗？我想问下没签合同能要二倍工资吗",
            "好的，那仲裁时效是多久",
            "公司拖欠我三个月工资",
            "我被辞退了，能拿赔偿金吗"]
    wrong = [k for k in keep if _is_greeting(k)]
    ok("带问题的句子不被误判成寒暄", not wrong, "、".join(wrong))


# ------------------------------------------------------------ 补充信息判定
def test_supplement() -> None:
    print("\n补充信息识别：")
    supp = ["我是杭州的，月薪8000", "以上情况属实", "有工资流水和考勤记录",
            "2024年6月15日离职的"]
    miss = [s for s in supp if not _looks_like_supplement(s)]
    ok("真补充仍然认作补充（不能为了修寒暄把这条路堵死）",
       not miss, "、".join(miss))

    # 寒暄以前会走进这条路，现在必须在入口就被截住。
    leak = [g for g in ["你好", "谢谢", "好的", "嗯", "收到"]
            if _looks_like_supplement(g)]
    ok("寒暄不再落进「补充」分支", not leak, "、".join(leak))

    q = ["公司拖欠工资怎么办", "没签合同能要二倍工资吗", "被辞退了怎么算赔偿"]
    wrong = [x for x in q if _looks_like_supplement(x)]
    ok("真问题不会被当成补充", not wrong, "、".join(wrong))


# --------------------------------------------------------------- 多轮行为
def test_multiturn() -> None:
    print("\n多轮：先问正事、再问「你好」")
    idx = LawIndex(CORPUS_PATH)
    store = MemoryStore(Path(tempfile.mkdtemp()) / "t.db")
    # 固定回放，理由与下面 test_degraded 那段完全相同，这里也踩一次：
    # 这个套件钉的是「寒暄不会被当成补充」「寒暄不进检索」这些**流程**判断，
    # 与模型答得好不好无关。带 key 时它连真模型跑三轮——1 秒变 35 秒、
    # 每次花钱，而且红绿取决于当天网络与余额。
    # 只置 online=False 不够，`run()` 开头的 maybe_recover() 会把它拉回来，
    # 所以同时把降级时刻设成现在，让恢复试探落在冷却期内。
    llm = LLM()
    llm.online = False
    llm._offline_since = time.time()
    runner = AgentRunner(idx, llm, store)
    sid = "intent-probe"

    first = "劳动合同法第82条关于未签书面合同二倍工资的规定是什么"
    r1 = runner.run(first, session_id=sid, emit=lambda e, d: None)
    ok("第 1 轮正常作答", r1.status in ("answered", "refused"),
       f"{r1.status} / {r1.intent}")

    r2 = runner.run("你好", session_id=sid, emit=lambda e, d: None)
    ok("第 2 轮识别为寒暄", r2.intent == "greet", r2.intent)
    ok("第 2 轮答案不是第 1 轮的原样重放",
       (r2.draft or "") != (r1.draft or ""),
       f"{(r2.draft or '')[:40]!r}")
    ok("寒暄不进检索（没有硬凑出来的依据）", len(r2.evidences) == 0,
       f"{len(r2.evidences)} 条")
    ok("寒暄的回复里给了下一步（告诉用户怎么开始）",
       ("问" in (r2.draft or "")) or ("说" in (r2.draft or "")),
       (r2.draft or "")[:60])

    # 寒暄之后紧接一个真问题，不能被上一轮的寒暄带偏
    r3 = runner.run("公司拖欠我三个月工资怎么办", session_id=sid,
                    emit=lambda e, d: None)
    ok("寒暄之后问正事仍然正常作答",
       r3.status in ("answered", "refused") and len(r3.evidences) >= 1,
       f"{r3.status} / {len(r3.evidences)} 条依据")
    ok("这一轮没有再被当成寒暄", r3.intent != "greet", r3.intent)

    # 全新会话第一次就问候，不该只回一个「在」——对方是什么都不知道的人，
    # 得告诉他能问什么。这条也是交付包第一屏的体验。
    r4 = runner.run("你好", session_id="intent-fresh", emit=lambda e, d: None)
    ok("新会话的寒暄给出能直接照着问的例子",
       "比如" in (r4.draft or "") and "拖欠" in (r4.draft or ""),
       (r4.draft or "")[:40])
    ok("新会话的寒暄说明了边界（查不到就说查不到）",
       "查不到" in (r4.draft or ""))


def test_degraded_note() -> None:
    """回放模式的提示。

    为什么值得单测：402 之后每一轮都由模板作答，而事件里原来一个字都没有，
    答案看着和平时一样。这条测试钉住两件事——回放必说、在线不乱说。
    """
    print("\n回放提示：")

    class _On:
        online = True

    class _Off:
        online = False

    class _NoAttr:
        pass          # 老代码里没有 online 属性的实现，不能因此报错

    ok("在线时不给提示", degraded_note(_On()) == "")
    ok("回放时给提示", degraded_note(_Off()) == _DEGRADED_TEXT)
    ok("拿不到状态时按在线处理（不误报）", degraded_note(_NoAttr()) == "")
    ok("提示语不自带法律结论", "不符合" not in _DEGRADED_TEXT
       and "应当" not in _DEGRADED_TEXT, _DEGRADED_TEXT[:20])

    # 端到端：强制离线跑一轮，事件流里必须正好有一条 degraded。
    idx = LawIndex(CORPUS_PATH)
    store = MemoryStore(Path(tempfile.mkdtemp()) / "d.db")
    llm = LLM()
    llm.online = False                       # 不依赖网络与余额，确定地走回放
    # 光置 False 不够：`run()` 开头会调 `maybe_recover()`，只要距上次降级超过
    # 冷却期就把它拉回在线。冷却期从 `_offline_since` 起算，新对象的初值是 0，
    # 于是「已过冷却」永远成立——账户里有钱时这一轮真会走在线，degraded 事件
    # 就不出现，测试变红。**测试不应该依赖账户余额**，所以把降级时刻设为现在，
    # 让恢复试探确定地落在冷却期内。
    llm._offline_since = time.time()
    runner = AgentRunner(idx, llm, store)
    seen = []
    runner.run("公司拖欠我两个月工资怎么办", session_id="deg-probe",
               emit=lambda e, d: seen.append((e, d)))
    kinds = [e for e, _ in seen]
    ok("回放答案会带 degraded 事件", kinds.count("degraded") == 1,
       f"出现 {kinds.count('degraded')} 次")
    # 「挂在正文末尾」的判据是：degraded 之后不再有正文类事件（token/note），
    # 且 done 还在后面。v116 起 degraded 与 done 之间会夹一个 extras——那是
    # 结构化办案包，不往答案卡里写一个字，不算正文。原来写成「紧跟 done」
    # 是把实现顺序当成了语义，加一个非正文事件就误报。
    di = kinds.index("degraded")
    tail = kinds[di + 1:]
    ok("提示语在 done 之前、且后面不再有正文事件（挂在正文末尾）",
       "done" in tail
       and not any(k in ("token", "note", "degraded") for k in tail),
       " / ".join(kinds[-4:]))
    ok("办案包事件在 done 之前发完（前端才收得到）",
       "extras" in tail and kinds.index("extras") < kinds.index("done"),
       " / ".join(kinds[-4:]))
    ok("存库的答案里没有掺提示语",
       all(_DEGRADED_TEXT not in (m.get("content") or "")
           for m in store.session_messages("deg-probe")))


def test_recover_wiring() -> None:
    """每轮开头要问一次「冷却过了吗」。

    这条线断了不会报错，只是永远不恢复——那种无声的失效最难发现，
    所以用一个计数替身把「调了几次」变成可断言的东西。
    """
    print("\n降级恢复的接线：")
    idx = LawIndex(CORPUS_PATH)
    store = MemoryStore(Path(tempfile.mkdtemp()) / "r.db")
    llm = LLM()
    llm.online = False
    calls = []
    llm.maybe_recover = lambda: calls.append(1) or False
    runner = AgentRunner(idx, llm, store)

    runner.run("你好", session_id="rec-1", emit=lambda e, d: None)
    ok("一轮开头问一次", len(calls) == 1, f"{len(calls)} 次")
    runner.run("你好", session_id="rec-1", emit=lambda e, d: None)
    ok("下一轮还会问（不是只问一次就忘）", len(calls) == 2, f"{len(calls)} 次")
    ok("恢复失败不影响正常作答", len(calls) == 2)


# ------------------------------------------------------- 越界 vs 信息不足
def test_out_of_scope() -> None:
    """这两种都「答不上来」，但该说的话完全不同。

    实测踩到的：问「如何烤蛋糕」，系统回「您是公司辞退、自己辞职，还是被迫离职？」
    ——用户问烤蛋糕，系统问离职方式。根因是短句（<8 字）映射不出法条术语时
    一律判 clarify，而 clarify 会无条件追问案情要素，从不问「这算不算劳动法问题」。

    修法是反向判定：映射不出术语 + 不含任何劳动法领域词 = 越界。
    比扩充越界词表靠得住——越界词穷举不完（今天是烤蛋糕，明天是养多肉）。
    """
    print("\n越界 vs 信息不足：")
    from app.llm import _guess_intent                        # noqa: E402

    for q in ("如何烤蛋糕", "养多肉", "今天天气", "红烧肉怎么做"):
        ok(f"「{q}」判越界", _guess_intent(q) == "out_of_scope", _guess_intent(q))
    # 反面：真的信息不足的短句不能被推到「越界」——那等于把能答的问题拒了。
    for q in ("被辞退了", "不给交社保", "老板跑路", "试用期"):
        ok(f"「{q}」不当成越界", _guess_intent(q) != "out_of_scope", _guess_intent(q))
    # 已能映射出法条术语的短句应该去检索（c08 零召回就是这条守住的）
    ok("「试用期能定多长」仍去检索",
       _guess_intent("试用期能定多长") == "case_consult",
       _guess_intent("试用期能定多长"))

    # 端到端：越界要给「只处理劳动法问题」的说明，而不是追问案情。
    store = MemoryStore(Path(tempfile.mkdtemp()) / "t.db")
    llm = LLM()
    llm.online = False
    llm.maybe_recover = lambda: False
    runner = AgentRunner(LawIndex(CORPUS_PATH), llm, store)
    st = runner.run("如何烤蛋糕", session_id="oos-1")
    ok("端到端判为越界", st.intent == "out_of_scope", st.intent)
    ok("越界答案说明只处理劳动法问题",
       "只处理劳动法相关的问题" in (st.draft or ""), (st.draft or "")[:80])
    ok("越界不再追问离职方式",
       "被迫离职" not in (st.draft or ""), (st.draft or "")[:80])
    ok("越界不调任何工具", not st.scratchpad, str([s.tool for s in st.scratchpad]))
    store.delete_session("oos-1")


def main() -> int:
    test_greeting()
    test_supplement()
    test_multiturn()
    test_degraded_note()
    test_recover_wiring()
    test_out_of_scope()

    print("\n" + "=" * 58)
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("  失败：\n" + "\n".join("    · " + f for f in FAIL))
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
