# -*- coding: utf-8 -*-
"""
诊断：词汇不匹配到底有多严重。

做法是同一件事问两遍——
  A 口语版：劳动者真实会怎么说（"公司随便给我调岗"）
  B 术语版：法条里怎么写（"变更劳动合同约定的内容"）

三组对照（2026-10-24 实测，top-10）：
  裸 BM25        1/12 =  8%   ← 没人改写时的裸底
  规则改写后     5/12 = 42%   ← 离线路径（没配 key）的真实能力
  术语版         12/12=100%   ← 上限

结论（决定了后续不做向量检索）：
  · 给出正确术语后全部能召回，位次 1–8 —— 瓶颈 100% 在「口语→术语」翻译，
    不在检索排序。所以接向量库是花大钱修一个不是瓶颈的地方。
  · 规则改写只有 42%，根因是它**关键词触发**：用户不说"加班"，它就不翻译成
    "延长工作时间"。要求用户先说出术语、才帮他翻译成术语，逻辑上自相矛盾。
  · 真正的解法是让 LLM 做翻译（实测 12/12）。项目里已有这一步（decompose），
    但原先的结果没传给决策层——见 README「口语问法」一节。

只读，不改产品代码，不花 API。

用法：
  python scripts/probe_vocab.py
  python scripts/probe_vocab.py -k 10      # 只看 top-10 命中率
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CORPUS_PATH                     # noqa: E402
from app.retrieval import LawIndex                    # noqa: E402

# (标签, 口语问法, 术语问法, 目标条文 id)
PAIRS = [
    ("调岗",       "公司可以随便给我调岗吗",
                   "用人单位变更劳动合同约定的内容，应当协商一致并采用书面形式",
     "劳动合同法_35"),
    ("辞退",       "老板突然让我别来了",
                   "用人单位违反本法规定解除劳动合同",
     "劳动合同法_87"),
    ("不签合同",   "上班半年了还没跟我签合同",
                   "用人单位自用工之日起超过一个月不满一年未与劳动者订立书面劳动合同",
     "劳动合同法_82"),
    ("加班费",     "晚上留下来干活给不给钱",
                   "安排劳动者延长工作时间，支付不低于工资的百分之一百五十的工资报酬",
     "劳动法_44"),
    ("社保",       "公司社保一分钱没交",
                   "用人单位未依法为劳动者缴纳社会保险费",
     "劳动合同法_38"),
    ("赔偿金算法", "被开除了能拿多少钱",
                   "用人单位违法解除劳动合同，应当依照经济补偿标准的二倍支付赔偿金",
     "劳动合同法_87"),
    ("离职补偿",   "干了三年走了给几个月工资",
                   "经济补偿按劳动者在本单位工作的年限，每满一年支付一个月工资",
     "劳动合同法_47"),
    ("试用期",     "试工期能定多长",
                   "劳动合同期限三年以上固定期限的，试用期不得超过六个月",
     "劳动合同法_19"),
    ("拖欠工资",   "工资老是不发",
                   "用人单位拖欠或者未足额支付劳动报酬",
     "劳动合同法_30"),
    ("仲裁时效",   "多久以后就不能去告了",
                   "劳动争议申请仲裁的时效期间为一年",
     "调解仲裁法_27"),
    ("无固定期限", "干久了能不能要个长期合同",
                   "连续订立二次固定期限劳动合同，劳动者提出订立无固定期限劳动合同",
     "劳动合同法_14"),
    ("竞业限制",   "不让我去同行上班给不给补偿",
                   "对负有保密义务的劳动者，在竞业限制期限内按月给予经济补偿",
     "劳动争议解释一_36"),
]


def hit_rank(index: LawIndex, query: str, target: str, topk: int) -> int:
    """目标条文在检索结果里的位次，0 表示没进 topk。"""
    for i, (_, a) in enumerate(index.search(query, topk=topk), 1):
        if a["id"] == target:
            return i
    return 0


def hit_rank_rewritten(index: LawIndex, query: str, target: str, topk: int) -> int:
    """先过产品自带的规则改写，再检索，取最好位次。

    这是「离线路径」（没配 key）真实的召回能力：decompose 走不了模型，
    只走 _rewrite_queries 的规则表。之所以必须单独测，是因为裸 BM25 的
    8% 是没有人改写的裸底，不能拿它当产品现状。
    """
    from app.llm import _rewrite_queries

    best = 0
    for sub in _rewrite_queries(query):
        r = hit_rank(index, sub, target, topk)
        if r and (not best or r < best):
            best = r
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", type=int, default=10, help="命中率按 top-k 统计")
    args = ap.parse_args()

    index = LawIndex(CORPUS_PATH)
    print(f"语料 {len(index.articles)} 条 / top-{args.k} 命中率\n")
    print(f"{'场景':<12}{'裸BM25':>8}{'规则改写':>8}{'术语版':>8}   口语问法")
    print("-" * 84)

    a_hit = b_hit = c_hit = 0
    rows = []
    for label, spoken, legal, target in PAIRS:
        ra = hit_rank(index, spoken, target, args.k)
        rc = hit_rank_rewritten(index, spoken, target, args.k)
        rb = hit_rank(index, legal, target, args.k)
        a_hit += bool(ra)
        c_hit += bool(rc)
        b_hit += bool(rb)
        rows.append((label, ra, rc, rb, spoken))
        print(f"{label:<12}{ra or '—':>8}{rc or '—':>8}{rb or '—':>8}   {spoken}")

    n = len(PAIRS)
    print("-" * 84)
    print(f"裸 BM25 命中 {a_hit}/{n} = {a_hit / n:.0%}   ← 没人改写时的裸底")
    print(f"规则改写命中 {c_hit}/{n} = {c_hit / n:.0%}   ← 离线路径的真实能力")
    print(f"术语版命中 {b_hit}/{n} = {b_hit / n:.0%}   ← 上限（问句即条文原文，天然占优）")

    missed = [r for r in rows if not r[1] and r[2]]
    if missed:
        print(f"\n规则改写救回的（{len(missed)} 条）：")
        for label, ra, rc, rb, spoken in missed:
            print(f"  {label}：裸 BM25 进不了 top-{args.k} → 改写后排第 {rc}")
    still = [r for r in rows if not r[2]]
    if still:
        print(f"\n规则改写也救不回（{len(still)} 条，这才是真正的缺口）：")
        for label, ra, rc, rb, spoken in still:
            print(f"  {label}：改写后仍进不了 top-{args.k}（术语版排第 {rb or '—'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
