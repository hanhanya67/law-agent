# -*- coding: utf-8 -*-
"""
意图路由单测。只发 INTENT_SYSTEM 那一小段 prompt，不跑完整 agent 流程。

为什么单独测路由：
  s14「合同违约金过高能请求调低吗」在 v5 里被答成「不在本系统范围内」——
  这是 L0 路由判了 out_of_scope，跟检索、生成都没关系。可只要跑全流程验证，
  一条题就要 20 秒、两分钱。单独测路由：50 条加起来不到一分钱，且能一次
  把全部边界题扫出来。

判据来自题目上的 expect_intent 字段：
  out_of_scope  这道题本来就该被推出去（借钱、离婚、狗咬这类越界题）
  in_scope      属于劳动法范围，不得判 out_of_scope（判了就是误拒）
没写这个字段的题按 in_scope 处理。

用法：
  python scripts/probe_intent.py            # 全量 50 条
  python scripts/probe_intent.py --limit 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import prompts          # noqa: E402
from app.config import LLM_ENABLED  # noqa: E402
from app.llm import LLM          # noqa: E402

KINDS = ("statute_lookup", "case_consult", "calc", "clarify", "out_of_scope")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    qs = [json.loads(l) for l in (ROOT / "eval" / "questions.jsonl").open(encoding="utf-8")
          if l.strip()]
    if args.limit:
        qs = qs[:args.limit]

    llm = LLM()
    if not llm.online:
        print("[abort] 没有可用 API key，路由只能在线测")
        return 1
    print(f"模型：{getattr(llm, 'model', '?')} | 题数：{len(qs)} | LLM_ENABLED={LLM_ENABLED}\n")

    tally: dict[str, int] = {k: 0 for k in KINDS}
    bad = []
    for q in qs:
        txt = ""
        try:
            r = llm._chat(prompts.INTENT_SYSTEM,
                          f"已知案情：\n用户：{q['question']}",
                          max_tokens=16, allow_think=False)
            txt = (r.choices[0].message.content or "").strip()
        except Exception as e:                      # noqa: BLE001
            print(f"[{q['id']}] 调用失败：{e}")
            continue
        kind = next((k for k in KINDS if k in txt), "case_consult")
        tally[kind] = tally.get(kind, 0) + 1
        want_oop = (q.get("expect_intent") or "in_scope") == "out_of_scope"
        got_oop = (kind == "out_of_scope")
        ok = (want_oop == got_oop)
        if not ok:
            bad.append((q["id"], q["question"][:26], kind,
                        "该推出去却硬接了" if want_oop else "该接却推出去了"))
        print(f"{'  ' if ok else '!!'}[{q['id']:<4}] {kind:<14} {q['question'][:34]}")

    print("\n分布：", {k: v for k, v in tally.items() if v})
    if bad:
        print(f"\n路由判错 {len(bad)} 条：")
        for qid, qs_, kind, why in bad:
            print(f"  {qid}  判成 {kind}  —— {why}　「{qs_}」")
    else:
        print("路由判定全部符合预期 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
