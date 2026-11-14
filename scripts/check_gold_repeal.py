#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""题库也会过期：检查有没有 gold / 证据引用了已废止或失效的条文。

为什么要有它：语料会过期这件事早就有人管（`import_law_text.py` 会给条文打
`status` 与 `repealed_note`），但**题库是另一份数据，从来没人对它做同样的检查**。
标注 gold 的时候那条文还有效，过两年废止了，gold 就变成了一道错题——
而且它不会报错，只会让模型答对了却拿不到分。

判据：gold / ev_ids 里出现的条文，只要 `status != 现行有效` 就列出来。
不是一律判失败——有一类题是**故意**引用废止条文的：

    「合同法第 114 条关于违约金过高的规定，这条还有效吗」

考的恰恰就是「这条已经废止，由民法典第 585 条承继」。所以已知的版本题要
**点名放行**，写在这个文件里；任何**没被点名的新出现**都判失败。
这就是「已知例外必须写名字」——不写名字的例外等于没有例外。

用法：
    python scripts/check_gold_repeal.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

# 故意引用废止条文的「版本题」：考的就是「这条废止了，由哪条承继」。
# 判据不是「我觉得它是版本题」，而是它的 keywords 里就写着「废止」。
KNOWN_VERSION_CASES = {"v01", "v03", "v04", "v06"}

# 已知的过期 gold：标注时条文还有效，后来失效了，但**还没改**。
# 为什么不顺手改：改 gold 等于改评分标准，得单独一轮做，而且要把同一批题
# 全部重看一遍。这里只负责「别让它悄悄增多」，并把名字留在显眼的地方。
KNOWN_STALE_GOLD = {"h11"}      # 解释一_32 第一款自 2025-09-01 废止，见 README

QUESTION_FILES = [
    ("主集", "eval/questions.jsonl"),
    ("留出集", "eval/holdout.jsonl"),
    ("口语集", "eval/questions_colloquial.jsonl"),
    ("口语留出", "eval/questions_colloquial_holdout.jsonl"),
    ("判定用例", "eval/judge_cases.jsonl"),
]


def main() -> int:
    corpus = {}
    for line in (ROOT / "data" / "statutes.jsonl").open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            corpus[r["id"]] = r

    found, unknown = [], []
    for set_name, rel in QUESTION_FILES:
        f = ROOT / rel
        if not f.exists():
            continue
        for line in f.open(encoding="utf-8"):
            if not line.strip():
                continue
            q = json.loads(line)
            qid = q.get("id")
            gold = list(q.get("gold") or [])
            ev = list(q.get("ev_ids") or [])
            for gid in gold + ev:
                r = corpus.get(gid)
                if not r or r.get("status") == "现行有效":
                    continue
                role = "gold" if gid in gold else "证据"
                if gid in gold and gid in ev:
                    role = "gold+证据"
                rec = (set_name, qid, gid, r.get("status"), role,
                       (r.get("repealed_note") or "")[:60])
                if rec not in found:
                    found.append(rec)
                if qid not in KNOWN_VERSION_CASES and qid not in KNOWN_STALE_GOLD:
                    if rec not in unknown:
                        unknown.append(rec)

    print("=" * 66)
    print("  题库失效审计：gold / 证据里引用了非现行有效条文的题")
    print("=" * 66)
    if not found:
        print("  没有。题库里的每一条 gold 与证据都指向现行有效条文。")
        return 0

    for set_name, qid, gid, status, role, note in found:
        tag = ("版本题" if qid in KNOWN_VERSION_CASES
               else "已知过期" if qid in KNOWN_STALE_GOLD else "未登记")
        print(f"  [{tag}] {set_name:5s} {qid:5s} {gid:22s} {status:5s} {role}")
        if note:
            print(f"          {note}")
    print()
    print(f"  共 {len(found)} 处：版本题 {sum(1 for x in found if x[1] in KNOWN_VERSION_CASES)}，"
          f"已知过期 {sum(1 for x in found if x[1] in KNOWN_STALE_GOLD)}，"
          f"未登记 {len(unknown)}")

    if unknown:
        print("\n  未登记的必须处理：要么是新的版本题（加进 "
              "KNOWN_VERSION_CASES），")
        print("  要么是真的过期了（加进 KNOWN_STALE_GOLD 并把原因写进注释），")
        print("  不能放着不管——它现在不会报错，只会让模型答对了却拿不到分。")
        return 1

    print("\n  全部已登记。这条检查的意义是：下次再有条文失效，它会响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
