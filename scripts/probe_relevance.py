# -*- coding: utf-8 -*-
"""验证「条文相关性」这道闸门的准确率。

背景：在线评测里 14 条无答案题错了 10 条——检索总能命中字面相近的条文，
模型拿到就当依据硬答。修法是在定性环节加一个 relevant 判断，判 false 直接拒答。

这个脚本专门量两件事：
  1. 该拒的有没有拒（无答案题）—— 拒答率
  2. 不该拒的有没有被拒（有答案题）—— 误拒率，这个比第一项更要紧，
     因为误拒会让系统显得「什么都没收录」

用法：
    .venv\\Scripts\\python.exe scripts/probe_relevance.py          # 全部 50 条
    .venv\\Scripts\\python.exe scripts/probe_relevance.py 8        # 只跑前 8 条
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import AgentRunner                       # noqa: E402
from app.config import CORPUS_PATH, DB_PATH             # noqa: E402
from app.llm import LLM                                 # noqa: E402
from app.memory import MemoryStore                      # noqa: E402
from app.retrieval import LawIndex                      # noqa: E402


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    rows = [json.loads(l) for l in
            (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    if limit:
        rows = rows[:limit]

    runner = AgentRunner(LawIndex(CORPUS_PATH), LLM(), MemoryStore(DB_PATH))

    print(f"{'id':<5}{'类型':<7}{'应拒':<5}{'实际':<8}{'rel':<6}{'证据':<5}{'耗时':<8}问题")
    print("-" * 96)
    hit = miss = false_reject = 0
    t0 = time.perf_counter()

    for q in rows:
        sid = f"probe_{q['id']}_{int(time.time() * 1000)}"
        st = runner.run(q["question"], session_id=sid)
        refused = st.status == "refused"
        rel = "" if st.verdict is None else ("T" if st.verdict.relevant else "F")
        ms = int(st.budget.elapsed() * 1000)
        want = bool(q.get("must_refuse"))
        ok = (refused == want)
        if want and refused:
            hit += 1
        elif want and not refused:
            miss += 1
        elif not want and refused:
            false_reject += 1
        mark = "" if ok else ("  ← 该拒没拒" if want else "  ← 误拒")
        print(f"{q['id']:<5}{q['type']:<7}{'是' if want else '否':<5}"
              f"{'拒答' if refused else st.status:<8}{rel:<6}"
              f"{len(st.evidences):<5}{ms:<8}{q['question'][:34]}{mark}")

    total = time.perf_counter() - t0
    print("-" * 96)
    n_ref = sum(1 for q in rows if q.get("must_refuse"))
    n_ans = len(rows) - n_ref
    print(f"无答案题 {n_ref} 条 → 正确拒答 {hit} 条，漏掉 {miss} 条"
          f"（拒答率 {hit / n_ref * 100 if n_ref else 0:.0f}%）")
    print(f"有答案题 {n_ans} 条 → 误拒 {false_reject} 条"
          f"（误拒率 {false_reject / n_ans * 100 if n_ans else 0:.0f}%）")
    print(f"总耗时 {total:.0f}s | 平均 {total / len(rows) * 1000:.0f}ms/条")


if __name__ == "__main__":
    main()
