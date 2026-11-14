#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent 循环的同参数去重（v132）。

为什么单独一个套件：这个 bug **只有真 key 才暴露**，而所有既有回归都强制
回放（run_all 清空 DEEPSEEK_API_KEY），回放模式的决策是规则写死的、永远
不会重复调用。于是「9 步里 6 步是同一批查询」这件事在 33 个套件里一条断言
都没有。真 key 实测两条问题都是「3 条查询 × 3 轮 = 9 步」，步数预算全烧在
重复上，calculate_compensation / check_validity 一次都轮不到。

这里用一个假的 LLM 把「重复决策」稳定复现出来，不联网、不花钱：

  decide 前两轮返回**完全相同**的两个检索调用 → 第二轮应当整轮被跳过，
  循环在第 2 轮结束时立刻退出（不是等 no_progress 累计到 2）。
  然后 judge 抛 SystemExit 把流程截断在「循环刚结束」那一刻——
  后面的定性/生成/自检与本套件无关，让它们跑只会引入别的依赖。

断言方式：数 scratchpad。去重生效是 2 步，失效是 4 步。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import AgentRunner, _call_key                 # noqa: E402
from app.config import CORPUS_PATH                            # noqa: E402
from app.memory import MemoryStore                            # noqa: E402
from app.retrieval import LawIndex                            # noqa: E402
from app.schemas import Decision, ToolCall                    # noqa: E402
from app.tools import all_schemas                             # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"    {detail}" if detail != "" and not cond else ""))


class _Stop(Exception):
    """循环跑完了，别再往下走定性/生成。"""


class FakeLLM:
    """只实现 run() 走到「循环结束」所需的那几个方法。"""

    online = True
    fallback_reason = ""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.decide_calls = 0

    def maybe_recover(self):
        return None

    def extract_slots(self, question):
        return {}

    def classify_intent(self, question, brief):
        return "case_consult"

    def ask(self, keys, question):
        return "能再具体说说吗？"

    def decompose(self, question, brief):
        return []                       # 不走 L1.5 的确定性预检索

    def decide(self, state, schemas):
        self.decide_calls += 1
        if self.rounds:
            return self.rounds.pop(0)
        return Decision(is_final=True)

    def judge(self, state):
        # 把「循环刚结束」的 state 留下来：这是本套件唯一要观察的东西。
        self.captured = state
        raise _Stop()

    def generate_stream(self, state, emit):
        raise _Stop()

    def verify(self, draft, evidences, index):
        raise _Stop()


def _runner(llm):
    index = LawIndex(CORPUS_PATH)
    tmp = tempfile.mkdtemp(prefix="loopchk_")
    store = MemoryStore(Path(tmp) / "t.db")
    return AgentRunner(index, llm, store)


def _run(llm, question="公司拖欠我工资能要什么"):
    """跑完「循环」就中断，返回被 judge 捕获下来的 state。"""
    runner = _runner(llm)
    try:
        runner.run(question, session_id="loopchk")
    except _Stop:
        return llm.captured
    raise AssertionError("流程没有被 judge 截断，与本套件的前提不符")


def main() -> int:
    print("=" * 62)
    print("Agent 循环：同参数工具调用去重")
    print("=" * 62)

    # ---- 1. 指纹本身 ----
    print("\n== 调用指纹 _call_key ==")
    a = ToolCall(name="search_statute", args={"query": "拖欠工资", "topk": 6})
    b = ToolCall(name="search_statute", args={"topk": 6, "query": "拖欠工资"})
    c = ToolCall(name="search_statute", args={"query": "拖欠工资", "topk": 9})
    d = ToolCall(name="get_article", args={"query": "拖欠工资", "topk": 6})
    ok("参数书写顺序不同 → 同一个指纹", _call_key(a) == _call_key(b))
    ok("参数值不同 → 不同指纹", _call_key(a) != _call_key(c))
    ok("工具名不同 → 不同指纹", _call_key(a) != _call_key(d))

    # ---- 2. 重复决策只执行一次 ----
    print("\n== 连续两轮发同样的检索 ==")
    dup = Decision(tool_calls=[
        ToolCall(name="search_statute", args={"query": "拖欠工资 支付", "topk": 6}),
        ToolCall(name="search_statute", args={"query": "未订立书面劳动合同 二倍工资", "topk": 6}),
    ])
    llm = FakeLLM([dup, dup, dup])
    st = _run(llm)
    tools = [(s.tool, (s.args or {}).get("query")) for s in st.scratchpad]
    ok("两轮重复只留下 2 步（去重生效）", len(st.scratchpad) == 2, tools)
    ok("没有同参数的第二个副本",
       len(tools) == len({(t, q) for t, q in tools}), tools)
    # 第二轮整轮被跳过 → 立刻退出，不会再去问模型第三轮。
    ok("整轮重复后立即退出（不再白烧一轮决策）", llm.decide_calls == 2,
       f"decide_calls={llm.decide_calls}")

    # ---- 3. 不同参数的调用不该被误杀 ----
    print("\n== 同工具、不同参数：两个都要跑 ==")
    r1 = Decision(tool_calls=[
        ToolCall(name="search_statute", args={"query": "拖欠工资", "topk": 6})])
    r2 = Decision(tool_calls=[
        ToolCall(name="search_statute", args={"query": "经济补偿 标准", "topk": 6})])
    llm2 = FakeLLM([r1, r2, r2])
    st2 = _run(llm2, question="拖欠工资和经济补偿都想要")
    ok("两条不同检索都执行了", len(st2.scratchpad) == 2,
       [(s.tool, s.args) for s in st2.scratchpad])
    # 3 次而不是 2 次：第三轮必须**问一次模型**才知道它要发什么，问完发现
    # 全是重复才退出。那一次决策省不掉——该省的是「执行」，不是「询问」。
    ok("第三轮全是重复后退出", llm2.decide_calls == 3, f"decide_calls={llm2.decide_calls}")

    # ---- 4. 不重复时行为不变 ----
    print("\n== 正常决策（不重复）不受影响 ==")
    u1 = Decision(tool_calls=[
        ToolCall(name="search_statute", args={"query": "拖欠工资", "topk": 6})])
    u2 = Decision(tool_calls=[
        ToolCall(name="search_statute", args={"query": "二倍工资", "topk": 6})])
    u3 = Decision(is_final=True)
    llm3 = FakeLLM([u1, u2, u3])
    st3 = _run(llm3, question="拖欠工资和二倍工资")
    ok("两轮不同检索各自保留", len(st3.scratchpad) == 2)
    ok("模型主动收工时正常退出", llm3.decide_calls == 3,
       f"decide_calls={llm3.decide_calls}")

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print(f"  ✗ {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
