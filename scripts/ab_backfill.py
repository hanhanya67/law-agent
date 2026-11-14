# -*- coding: utf-8 -*-
"""A/B 对照：产品层换用共用抽取器后，补核验的条文多了多少。

背景：改动前 `agent._backfill_citations` 只认书名号（`《X》第N条`），而评测层
`extract_cites` 认承前省略、裸法律名、存在性回退。两边口径不一致，结果是评测
报「122 处引用全部合法」，产品却只核验了其中一部分——指标在替产品兜底。

做法：拿已经存下来的模型答案（逐字不动），分别用旧正则和新抽取器跑一遍补核验，
差分出「以前漏核的引用」。零 API 成本。

用法：
    python scripts/ab_backfill.py eval/report_online_v9.json eval/questions.jsonl
    python scripts/ab_backfill.py eval/report_holdout.json     eval/holdout.jsonl
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from app.cite import (extract_cites, extract_cites_spans,  # noqa: E402
                      find_article, resolve_cites)
from app.config import CORPUS_PATH                               # noqa: E402
from app.retrieval import LawIndex                               # noqa: E402
from run_eval import (MODE_TAG, MODE_UNKNOWN_TAG,  # noqa: E402
                      mode_summary)

# 改动前产品层用的正则，原样保留，作为对照组
_OLD_RE = re.compile(r"《([^》]{2,40})》\s*第([零一二三四五六七八九十百千\d]+)条")


def split_key(mode: str | None) -> str:
    """这条答案算「在线」还是「其余」。

    这个 A/B 比的是**抽取器**：同一批文本，旧正则 vs 新抽取器，谁多抽出引用。
    所以混着来源也能比——抽取器不关心文本是谁写的。但「多出来的引用里有多少
    是 gold」这类比例会受答案风格影响，模板答案引得少、也规整，把它和模型答案
    混在一起算一个比例，读的人无从判断该信多少。所以分开计数、一起展示。
    """
    return "online" if mode == "online" else "other"


def old_backfill(answer: str, index: LawIndex, have: set[str]) -> tuple[list[str], list[str]]:
    filled, phantom = [], []
    for law, no in _OLD_RE.findall(answer):
        art = find_article(index, law, no)
        if art is None:
            label = f"《{law}》第{no}条"
            if label not in phantom:
                phantom.append(label)
            continue
        if art["id"] in have:
            continue
        filled.append(art["id"])
        have.add(art["id"])
    return filled, phantom


def main() -> int:
    detail_p = Path(sys.argv[1] if len(sys.argv) > 1 else "eval/report_online_v9.json")
    qfile = Path(sys.argv[2] if len(sys.argv) > 2 else "eval/questions.jsonl")
    if not detail_p.is_absolute():
        detail_p = ROOT / detail_p
    if not qfile.is_absolute():
        qfile = ROOT / qfile

    index = LawIndex(CORPUS_PATH)
    d = json.loads(detail_p.read_text(encoding="utf-8"))
    rows = d["results"] if isinstance(d, dict) and "results" in d else d
    gold_of = {json.loads(l)["id"]: json.loads(l).get("gold") or []
               for l in qfile.open(encoding="utf-8") if l.strip()}

    tot_old = tot_new = 0
    stat = {"online": [0, 0], "other": [0, 0]}     # [旧口径回填, 新口径回填]
    used_rows = []          # 真正进了 A/B 的条，来源分布要按它算，不能按全明细
    miss_by_q: Counter[str] = Counter()
    extra_in_gold: list[tuple[str, str]] = []
    phantom_diff: list[tuple[str, list[str], list[str]]] = []
    print(f"{'id':<8}{'旧补':>5}{'新补':>5}{'漏核':>5}   漏掉的条文")
    print("-" * 80)

    for r in rows:
        ans = r.get("answer") or ""
        if not ans or r.get("refused"):
            continue
        used_rows.append(r)
        have0 = set(r.get("ev_ids") or [])
        old_fill, old_ph = old_backfill(ans, index, set(have0))
        new_extra, new_ph = resolve_cites(ans, index, have0)

        old_ids = set(old_fill)
        new_ids = [a["id"] for a in new_extra]
        gained = [i for i in new_ids if i not in old_ids]
        tot_old += len(old_fill)
        tot_new += len(new_ids)
        bucket = stat[split_key(r.get("mode"))]
        bucket[0] += len(old_fill)
        bucket[1] += len(new_ids)
        gold = set(gold_of.get(r["id"], []))
        for g in gained:
            if g in gold:
                extra_in_gold.append((r["id"], g))
        miss_by_q[r["id"]] = len(gained)
        if old_ph != new_ph:
            phantom_diff.append((r["id"], old_ph, new_ph))
        if gained:
            tag = MODE_TAG.get(r.get("mode"), MODE_UNKNOWN_TAG)
            print(f"{r['id'] + tag:<8}{len(old_fill):>5}{len(new_ids):>5}"
                  f"{len(gained):>5}   " + ", ".join(gained))

    print("-" * 80)
    n_q = sum(1 for r in rows if r.get("answer") and not r.get("refused"))
    print(f"作答 {n_q} 条 | 旧口径回填 {tot_old} 条 → 新口径 {tot_new} 条 "
          f"（多出 {tot_new - tot_old} 条，涉及 {len(miss_by_q) - sum(1 for v in miss_by_q.values() if v == 0)} 题）")

    # 来源分开报。这个 A/B 比的是抽取器本身，混着来源照样能比（抽取器不关心
    # 文本谁写的）；但「多出来的引用值不值钱」这类比例会被答案风格带偏——
    # 模板答案引得少且规整，和模型答案混成一个比例，读的人无从判断该信多少。
    n_online, n_partly, n_replay = mode_summary(used_rows)
    n_unknown = len(used_rows) - n_online - n_partly - n_replay
    bits = [f"在线 {n_online}"]
    if n_partly:
        bits.append(f"半程 {n_partly}")
    if n_replay:
        bits.append(f"回放 {n_replay}")
    if n_unknown:
        bits.append(f"来源不明 {n_unknown}")
    print("来源：" + " / ".join(bits))
    if stat["other"][1]:
        print(f"  在线条  旧 {stat['online'][0]:>3} → 新 {stat['online'][1]:>3}"
              f"（+{stat['online'][1] - stat['online'][0]}）")
        print(f"  其余条  旧 {stat['other'][0]:>3} → 新 {stat['other'][1]:>3}"
              f"（+{stat['other'][1] - stat['other'][0]}）"
              f"   ← 模板/半程答案，这组数字不代表模型")
    if n_unknown:
        print(f"  ↑ 其中 {n_unknown} 条明细里没有 mode 字段（加字段之前跑的），"
              f"来源无法确认，上面的总数混着两种答案。")
    print(f"其中属于标准答案（gold）的 {len(extra_in_gold)} 条："
          f"{extra_in_gold if extra_in_gold else '无'}")
    print(f"幻觉判定有变化的题：{phantom_diff if phantom_diff else '无'}")

    # ---- 补核验后，可溯源率能到多少 ----
    # 这一步回答的是：「同一批旧答案，如果当时跑的是现在的产品代码，
    # 未核验引用会不会清零」。不算这一步，报告里那个 96.7% 会被读成产品缺陷，
    # 而实际上是上一版产品没抽出来的引用。
    n_cite = n_unv_before = n_unv_after = 0
    for r in rows:
        ans = r.get("answer") or ""
        if not ans or r.get("refused"):
            continue
        have0 = set(r.get("ev_ids") or [])
        cited = {c["id"] for c in extract_cites_spans(ans, index) if c["id"]}
        extra, _ = resolve_cites(ans, index, have0)
        after = have0 | {a["id"] for a in extra}
        n_cite += len(cited)
        n_unv_before += len(cited - have0)
        n_unv_after += len(cited - after)
    if n_cite:
        print()
        print(f"可溯源率（同一批答案，改动前后对比）：")
        print(f"  改动前产品口径  {(1 - n_unv_before / n_cite) * 100:5.1f}%"
              f"   （{n_unv_before}/{n_cite} 处引用没有出处）")
        print(f"  改动后产品口径  {(1 - n_unv_after / n_cite) * 100:5.1f}%"
              f"   （{n_unv_after}/{n_cite} 处引用没有出处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
