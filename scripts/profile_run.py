# -*- coding: utf-8 -*-
"""单条问答的耗时与 token 分解。

为什么要这个：
  --online 跑一条要 42 秒，但不知道钱和时间花在哪个环节。
  Agent 一次问答会调用 LLM 8~12 次（意图/分解/槽位/决策×N/定性/生成/自检），
  不给每个环节打点，就只能在「整体太慢」上瞎猜。

用法：
    .venv\\Scripts\\python.exe scripts/profile_run.py [问题]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm as llm_mod                      # noqa: E402
from app.agent import AgentRunner                         # noqa: E402
from app.config import CORPUS_PATH, DB_PATH, TOK_RETRY_CAP  # noqa: E402
from app.memory import MemoryStore                  # noqa: E402
from app.retrieval import LawIndex                  # noqa: E402

MARKS: list[tuple[str, float, int, int, int]] = []
_REC = {"prompt": 0, "completion": 0, "reason": 0}


def _patch() -> None:
    """给 LLM 的每个公开方法打点。"""
    orig_chat = llm_mod.LLM._chat

    def chat(self, system, user, **kw):
        t0 = time.perf_counter()
        r = orig_chat(self, system, user, **kw)
        dt = (time.perf_counter() - t0) * 1000
        u = getattr(r, "usage", None)
        p = getattr(u, "prompt_tokens", 0) or 0
        c = getattr(u, "completion_tokens", 0) or 0
        det = getattr(u, "completion_tokens_details", None)
        rt = (getattr(det, "reasoning_tokens", 0) or 0) if det else 0
        MARKS.append((_CURRENT[0], dt, p, c, rt))
        return r

    llm_mod.LLM._chat = chat

    for name in ("classify_intent", "decompose", "extract_slots", "decide",
                 "judge", "verify", "ask"):
        orig = getattr(llm_mod.LLM, name)

        def make(orig=orig, name=name):
            def w(self, *a, **k):
                _CURRENT[0] = name
                t0 = time.perf_counter()
                r = orig(self, *a, **k)
                # _chat 已经记过耗时，这里只兜底（回放模式不走 _chat）
                if llm_mod.LLM._chat.__name__ != "chat":
                    pass
                return r
            return w

        setattr(llm_mod.LLM, name, make())

    orig_gen = llm_mod.LLM.generate_stream

    def gen(self, state, emit):
        _CURRENT[0] = "generate"
        t0 = time.perf_counter()
        r = orig_gen(self, state, emit)
        dt = (time.perf_counter() - t0) * 1000
        # 流式不走 _chat，单独记一笔（token 数未知，标 -1）
        MARKS.append(("generate(流式)", dt, -1, len(r or "") // 2, -1))
        return r

    llm_mod.LLM.generate_stream = gen


_CURRENT = ["?"]


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else \
        "入职三个月了公司一直没跟我签书面合同，我可以主张什么"

    _patch()
    agent = AgentRunner(LawIndex(CORPUS_PATH), llm_mod.LLM(), MemoryStore(DB_PATH))
    events: list[str] = []

    def emit(e: str, d: dict) -> None:
        if e in ("tool_start", "tool_end", "verdict", "verify", "thinking"):
            events.append(f"{e}:{d.get('tool') or d.get('kind') or d.get('status') or ''}")

    print("=" * 78)
    print(f"问题：{q}")
    print("=" * 78)
    t0 = time.perf_counter()
    st = agent.run(q, emit=emit)
    total = (time.perf_counter() - t0) * 1000

    print(f"\n{'环节':<20}{'耗时(ms)':>10}{'输入':>8}{'输出':>8}{'思考':>8}")
    print("-" * 78)
    agg: dict[str, list[int]] = {}
    for name, dt, p, c, rt in MARKS:
        print(f"{name:<20}{dt:>10.0f}{p:>8}{c:>8}{rt:>8}")
        a = agg.setdefault(name, [0, 0, 0, 0, 0])
        a[0] += dt
        a[1] += max(p, 0)
        a[2] += max(c, 0)
        a[3] += max(rt, 0)
        a[4] += 1

    print("-" * 78)
    tp = tc = tr = 0
    for name, (s, p, c, rt, n) in agg.items():
        print(f"{name + ' ×' + str(n):<20}{s:>10.0f}{p:>8}{c:>8}{rt:>8}")
        tp += p
        tc += c
        tr += rt

    print("=" * 78)
    print(f"LLM 总耗时 {sum(m[1] for m in MARKS):.0f} ms | 端到端 {total:.0f} ms "
          f"| 调用 {len(MARKS)} 次")
    print(f"token：输入 {tp} + 输出 {tc}（其中思考 {tr}）= 合计 {tp + tc}")
    print(f"步骤：{st.intent} / {st.status} / 证据 {len(st.evidences)} 条 / "
          f"轨迹 {len(st.scratchpad)} 步")
    print(f"事件：{' → '.join(events)}")
    print("=" * 78)
    print("\n答案预览：")
    print((st.draft or "")[:600])


if __name__ == "__main__":
    main()
