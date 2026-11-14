# -*- coding: utf-8 -*-
"""
诊断：在线模式（LLM 改写）能不能救回口语问法。

前面 probe_vocab.py 测出裸 BM25 只有 8%、规则改写 42%。但那两个都是在评测
集这种"半术语问法"上不明显的——评测集的题本来就是照着法条措辞写的。

这个脚本只做一件事：拿两句口语去问 LLM 的 decompose，看它改写成什么，
再拿改写结果去 BM25，看目标条文能不能召回。只调 decompose，不跑完整链路，
12 条的成本在一分钱以内。

用法：
  python scripts/probe_decompose.py            # 走当前配置（在线）
  python scripts/probe_decompose.py --offline   # 强制走规则表对照
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CORPUS_PATH                     # noqa: E402
from app.retrieval import LawIndex                    # noqa: E402
from scripts.probe_vocab import PAIRS, hit_rank       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="强制规则表")
    ap.add_argument("-k", type=int, default=10)
    args = ap.parse_args()

    from app.llm import LLM

    llm = LLM()
    if args.offline:
        llm.online = False
    print(f"模式：{'在线 LLM 改写' if llm.online else '离线规则表'}\n")

    index = LawIndex(CORPUS_PATH)
    hit = 0
    for label, spoken, _legal, target in PAIRS:
        subs = llm.decompose(spoken, "")
        best = 0
        for s in subs:
            r = hit_rank(index, s, target, args.k)
            if r and (not best or r < best):
                best = r
        hit += bool(best)
        flag = "OK " if best else "MISS"
        print(f"{flag} {label:<10}{spoken}")
        for s in subs:
            print(f"      → {s}")
        print(f"      目标 {target} 位次 {best or '—'}")
    n = len(PAIRS)
    print(f"\n命中 {hit}/{n} = {hit / n:.0%}（top-{args.k}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
