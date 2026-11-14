# -*- coding: utf-8 -*-
"""
实验：BM25 从纯 bigram 改成 bigram + unigram 混合，能不能扛住术语变体。

起因是一个具体漏召：
  query 写「违法解除劳动合同 赔偿金」  → 命中不了《劳动合同法》第 87 条
  87 条原文写「用人单位违反本法规定解除或者终止劳动合同」
  「违法」和「违反」只共享一个「违」字，bigram 层面零重合，BM25 给 0 分。

中文术语变体（违法/违反、辞退/解除、劳动报酬/工资）是常态，靠穷举同义词表
治不了。unigram 至少让它们共享单字，代价是可能引入噪声——所以这个脚本要
同时看两件事：
  1. 口语探针 12 条能不能捞回来（收益）
  2. 主集 50 条 gold 的 top-10 召不召得回（有没有把真正相关的挤下去）

只读，不改产品代码，零 API 成本。

用法：
  python scripts/exp_hybrid_bm25.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CORPUS_PATH, GENERIC_BOOST, GENERIC_LAWS   # noqa: E402
from app.retrieval import LawIndex, cn2int, tokenize              # noqa: E402
from scripts.probe_vocab import PAIRS                             # noqa: E402

_CN = r"[\u4e00-\u9fff]+"


def tokenize_hybrid(text: str, uni: bool = True) -> list[str]:
    """bigram 为主，补一层单字通道。

    单字 token 加 "u" 前缀，避免与 bigram 撞名（"合同"是 bigram，"u合"是单字）。
    单字本身出现在几乎所有条文里，idf 很小，BM25 会自动压低它的权重，
    不需要手动调系数——这是它比"人工同义词表"更省事的地方。
    """
    toks = tokenize(text)
    if uni:
        for m in re.finditer(_CN, text):
            toks.extend("u" + ch for ch in m.group())
    return toks


class BM25X:
    def __init__(self, docs: list[str], uni: bool = True,
                 k1: float = 1.5, b: float = 0.75):
        self.k1, self.b, self.uni = k1, b, uni
        self.tfs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for d in docs:
            tf = Counter(tokenize_hybrid(d, uni))
            self.tfs.append(tf)
            self.lens.append(sum(tf.values()))
            for w in tf:
                df[w] += 1
        self.n = len(docs)
        self.avgdl = (sum(self.lens) / self.n) if self.n else 0.0
        self.idf = {w: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def search(self, query: str, topk: int = 5) -> list[tuple[float, int]]:
        qt = tokenize_hybrid(query, self.uni)
        if not qt or not self.n:
            return []
        scores: list[tuple[float, int]] = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.lens[i] or 1
            for w in qt:
                f = tf.get(w)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += self.idf.get(w, 0.0) * f * (self.k1 + 1) / denom
            if s > 0:
                scores.append((s, i))
        scores.sort(key=lambda x: x[0], reverse=True)
        return scores[:topk]


def build_search(index: LawIndex, bm25: BM25X):
    """复刻 LawIndex.search 的过滤与加权，只换打分器。"""
    def _search(query: str, topk: int = 10, only_valid: bool = True,
                law_hint: str = "") -> list[tuple[float, dict]]:
        hits = bm25.search(query, topk=max(topk * 6, 30))
        out = []
        for score, i in hits:
            a = index.articles[i]
            hinted = bool(law_hint) and (law_hint in a["law_name"]
                                         or law_hint in a["law_short"]
                                         or a["law_short"] in law_hint)
            if only_valid and (a.get("status") or "现行有效") not in ("现行有效", "部分失效"):
                if not hinted:
                    continue
            if law_hint and not hinted:
                continue
            w = GENERIC_BOOST if a["law_short"] in GENERIC_LAWS else 1.0
            out.append((score * w, a))
        out.sort(key=lambda x: -x[0])
        return out[:topk]
    return _search


def evaluate(search, queries: list[tuple[str, str]], topk: int = 10) -> tuple[int, list[str]]:
    hit, missed = 0, []
    for label, q, target in queries:
        rank = 0
        for i, (_, a) in enumerate(search(q, topk=topk), 1):
            if a["id"] == target:
                rank = i
                break
        if rank:
            hit += 1
        else:
            missed.append(label)
    return hit, missed


def main() -> int:
    index = LawIndex(CORPUS_PATH)

    main_qs = []
    with open(ROOT / "eval" / "questions.jsonl", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            q = json.loads(line)
            for g in q.get("gold") or []:
                main_qs.append((f"{q['id']}:{g}", q["question"], g))

    # 口语探针：用未改写的原句（最坏情况），不用规则改写
    spoken = [(label, q, tgt) for label, q, _legal, tgt in PAIRS]

    for uni in (False, True):
        bm25 = BM25X([index._doc_text(a) for a in index.articles], uni=uni)
        search = build_search(index, bm25)
        h_spoken, miss_spoken = evaluate(search, spoken)
        h_main, miss_main = evaluate(search, main_qs)
        tag = "bigram+unigram" if uni else "纯 bigram（现状）"
        print(f"\n【{tag}】")
        print(f"  口语探针  {h_spoken}/{len(spoken)} = {h_spoken / len(spoken):.0%}"
              f"{'  漏：' + ','.join(miss_spoken) if miss_spoken else ''}")
        print(f"  主集 gold {h_main}/{len(main_qs)} = {h_main / len(main_qs):.0%}"
              f"{'  漏：' + ','.join(miss_main) if miss_main else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
