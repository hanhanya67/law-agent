# -*- coding: utf-8 -*-
"""
实验：查询侧领域同义词扩展，能不能补上「中文生活词 → 法律术语」那道缝。

起因是同一个缺口被两套独立题集各撞一次：
  我怀孕了          → 条文写「女职工在孕期、产期、哺乳期」   → 搜不到
  厂子倒闭了        → 条文写「用人单位被依法宣告破产」       → 搜不到
  年终奖不发        → 条文写「劳动报酬」                     → 搜不到

这跟"接向量检索"不是一回事：不需要模型、不需要依赖，只是在检索式后面
追加几个法律术语。代价是可能把弱相关条文顶上来，所以这个脚本必须同时看
两件事：

  1. 三套题的 gold 召回升了多少（收益）
  2. 有没有哪条题反而掉下去（噪声）

只读，不改产品代码，零 API 成本。

用法：
  python scripts/exp_lexicon.py
  python scripts/exp_lexicon.py -k 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CORPUS_PATH            # noqa: E402
from app.retrieval import LawIndex           # noqa: E402

# 生活词 → 条文里的写法。只收「有真实失败案例撑着」的条目，
# 不做同义改写大全：多一条就多一份把弱相关条文顶上来的风险。
ALIAS: dict[str, list[str]] = {
    "怀孕": ["孕期"],
    "倒闭": ["被依法宣告破产", "破产"],
    "年终奖": ["劳动报酬"],
    "调岗": ["变更劳动合同"],
    "辞退": ["解除劳动合同"],
    "开除": ["解除劳动合同"],
    "炒鱿鱼": ["解除劳动合同"],
    "试工期": ["试用期"],
    "社保": ["社会保险"],
    "加班": ["延长工作时间"],
    "不签合同": ["未订立书面劳动合同"],
    "双倍工资": ["二倍工资"],
}


def expand(q: str) -> str:
    """命中的生活词，把对应的法律术语追加到检索式末尾。

    BM25 里每个 query 词独立贡献分数，所以追加词相当于"再问一遍这件事"，
    不会稀释原有信号——这也是选"追加"而不是"替换"的原因。
    """
    extras: list[str] = []
    for word, terms in ALIAS.items():
        if word in q:
            for t in terms:
                if t not in q and t not in extras:
                    extras.append(t)
    return q + (" " + " ".join(extras) if extras else "")


def gold_hit(index: LawIndex, q: str, gold: list[str], topk: int) -> bool:
    if not gold:
        return True
    ids = {a["id"] for _, a in index.search(q, topk=topk)}
    return all(g in ids for g in gold)


def load(path: str) -> list[dict]:
    p = ROOT / "eval" / path
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=10)
    args = ap.parse_args()

    index = LawIndex(CORPUS_PATH)
    suites = [
        ("口语留出 8（未调优）", load("questions_colloquial_holdout.jsonl")),
        ("口语 14", load("questions_colloquial.jsonl")),
        ("留出集 14（未调优）", load("holdout.jsonl")),
        ("主集 50", load("questions.jsonl")),
    ]

    total_before = total_after = 0
    print(f"gold 全命中判据：top-{args.k}\n")
    print(f"{'题集':<24}{'扩展前':>10}{'扩展后':>10}{'变化':>8}")
    print("-" * 56)

    detail = []
    for name, qs in suites:
        qs = [q for q in qs if q.get("gold")]
        if not qs:
            continue
        b = a = 0
        for q in qs:
            hb = gold_hit(index, q["question"], q["gold"], args.k)
            ha = gold_hit(index, expand(q["question"]), q["gold"], args.k)
            b += hb
            a += ha
            if hb != ha:
                detail.append((name, q["id"], q["question"], bool(hb), bool(ha)))
        total_before += b
        total_after += a
        d = a - b
        print(f"{name:<24}{b:>4}/{len(qs):<5}{a:>4}/{len(qs):<5}{d:>+8}")
    print("-" * 56)
    print(f"{'合计':<24}{total_before:>10}{total_after:>10}{total_after - total_before:>+8}")

    gained = [x for x in detail if x[4] and not x[3]]
    lost = [x for x in detail if x[3] and not x[4]]
    if gained:
        print(f"\n救回（{len(gained)} 条）：")
        for name, qid, q, _, _ in gained:
            print(f"  {qid:<5}{q[:34]:<36}{name}")
    if lost:
        print(f"\n反而掉了（{len(lost)} 条，这是扩展的代价）：")
        for name, qid, q, _, _ in lost:
            print(f"  {qid:<5}{q[:34]:<36}{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
