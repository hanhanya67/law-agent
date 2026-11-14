# -*- coding: utf-8 -*-
"""
等义表净收益：逐题对比「不加等义表」与「加等义表」的召回。

存在的意义：README 里要说清「96.5% / 100% / 100% / 100% 这组数字是怎么来的」。
只报加表后的数，读者无法判断是模型变好了还是尺子放松了。这个脚本把两个数
并排放，并逐条列出「哪一题的哪条 gold 是被等义条文接住的」——可核。

用法：
  ./.venv/Scripts/python.exe scripts/exp_equiv_gain.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from app.config import CORPUS_PATH                      # noqa: E402
from app.retrieval import LawIndex                      # noqa: E402
from equiv import EQUIV, expand_gold                    # noqa: E402

# 这里**不用管答案来源**（mode 字段），与 ab_backfill 不同：
# 本脚本只读明细里的 ev_ids（检索到了哪些条文），一个字的答案文本都不碰。
# 回放模式替换的是 LLM 决策，检索本身照跑——等义扩展能多接住几条 gold，
# 在回放条上和在在线条上是同一个检索器跑出来的，结论一样成立。
# 记在这里是因为「要不要按来源拆」每加一个字段就得重判一遍，别再推第二次。
PAIRS = [
    ("主集50",    "eval/report_v18.json",                    "eval/questions.jsonl"),
    ("留出集14",  "eval/report_holdout_v15.json",            "eval/holdout.jsonl"),
    ("口语集14",  "eval/report_colloquial_v18.json",         "eval/questions_colloquial.jsonl"),
    ("口语留出8", "eval/report_colloquial_holdout_v2.json",  "eval/questions_colloquial_holdout.jsonl"),
]


def main() -> None:
    index = LawIndex(CORPUS_PATH)
    print("（只读明细里的 ev_ids，不碰答案文本；回放条与在线条的检索是同一个"
          "检索器跑的，故不按来源拆分）\n")
    for name, det, qf in PAIRS:
        rows = {r["id"]: r for r in json.load(open(ROOT / det, encoding="utf-8"))}
        qs = [json.loads(line) for line in open(ROOT / qf, encoding="utf-8") if line.strip()]
        n = base = after = 0
        fixed = []
        for q in qs:
            r = rows.get(q["id"])
            if not r:
                continue
            gold = [g for g in (q.get("gold") or []) if g in index.by_id]
            got = set(r.get("ev_ids") or [])
            if not gold or not got:
                continue
            b = len(set(gold) & got) / len(gold)
            a = len(expand_gold(gold, got)) / len(gold)
            n += 1
            base += b
            after += a
            if a > b:
                miss = [g for g in gold if g not in got]
                ext = {e for g in miss for e in EQUIV.get(g, ()) if e in got}
                fixed.append(f'{q["id"]}: {sorted(miss)} <- {sorted(ext)}')
        if not n:
            print(f"{name:10s} 无可重算条目")
            continue
        print(f"{name:10s} n={n}  不加等义 {base / n * 100:5.1f}%  ->  加等义 {after / n * 100:5.1f}%")
        for f in fixed:
            print("    ", f)


if __name__ == "__main__":
    main()
