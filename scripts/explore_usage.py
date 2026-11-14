# -*- coding: utf-8 -*-
"""
**这是被否掉的方案，留在仓库里当证据，不参与评测流程。**

原本想用「条文原文与答案的字面重合度」判断某处引用到底有没有被实质使用，
用它替代「依据聚焦度」。跑出来的结论是这条路不成立：

  · 版本题全灭。v03「合同法一百一十四条已废止，由民法典五百八十五条承继」
    一个字都没复述条文，cov=0.02，但它恰恰是版本题唯一正确的答法。
  · 转述与凑数分不开。s09 的第三十九、四十条是模型转述第十四条时带出来的
    条号，cov=0.14/0.10；它们确实没被独立使用，但属于合法转述，不是缺陷。

谁想复现或改进，跑它就行。评测口径见 eval/run_eval.py 的「为什么撤掉依据聚焦度」。

用法：
  python scripts/explore_usage.py eval/report_v13.json eval/questions.jsonl s01 s09
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from app.cite import extract_cites_spans          # noqa: E402
from app.config import CORPUS_PATH               # noqa: E402
from app.retrieval import LawIndex               # noqa: E402
from run_eval import MODE_TAG, MODE_UNKNOWN_TAG, mode_note  # noqa: E402

# 句子边界：中文句号、问号、叹号、分号、换行。
# 分号算边界是因为答案里常见「甲；乙；丙」的并列列举，每段本来就该独立看。
_STOP = "。！？；\n"


def sentence_around(text: str, start: int, end: int,
                    extra_next: bool = True) -> tuple[str, int]:
    """取引用所在的那一句（含下一句，因为解释常跟在后面）。

    返回 (片段, 片段在全文里的起点)，后者用于把引用的位置映射进片段，
    从而能把引用文字本身摘掉——不摘的话「第四十条」这几个字会被算成
    对条文内容的命中，等于自己证明自己。
    """
    left = max(text.rfind(c, 0, start) for c in _STOP) + 1
    right = len(text)
    for c in _STOP:
        i = text.find(c, end)
        if i != -1:
            right = min(right, i)
    seg = text[left:right]
    if extra_next and right < len(text) - 1:
        nxt_right = len(text)
        for c in _STOP:
            i = text.find(c, right + 1)
            if i != -1:
                nxt_right = min(nxt_right, i)
        seg += text[right:min(nxt_right + 1, right + 120)]
    return seg, left


def ngrams(s: str, n: int = 2) -> set[str]:
    """只保留汉字，避免标点差异干扰。"""
    s = "".join(ch for ch in s if "\u4e00" <= ch <= "\u9fa5")
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（只算汉字）。"""
    a = "".join(ch for ch in a if "\u4e00" <= ch <= "\u9fa5")
    b = "".join(ch for ch in b if "\u4e00" <= ch <= "\u9fa5")
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    detail, qfile = sys.argv[1], sys.argv[2]
    want = set(sys.argv[3:])

    d = json.loads((ROOT / detail).read_text(encoding="utf-8"))
    rs = d["results"] if isinstance(d, dict) and "results" in d else d
    idx = LawIndex(CORPUS_PATH)

    rows = []
    for r in rs:
        if want and r["id"] not in want:
            continue
        ans = r.get("answer") or ""
        if not ans:
            continue
        print("=" * 74)
        tag = MODE_TAG.get(r.get("mode"), MODE_UNKNOWN_TAG)
        print(f"[{r['id']}]{tag} {r['question']}")
        note = mode_note(r.get("mode"))
        if note:
            # 先说来源再说引用：模板拼的答案读起来也像人话，
            # 不知道它是模板的话，会照着它去调 Prompt——那是跟空气较劲。
            print(f"  ⚠ {note}，这里的措辞不代表模型")
        for c in extract_cites_spans(ans, idx):
            art = c["article"]
            if art is None:
                print(f"  《{c['law']}》第{c['no']}条  → 库里没有")
                continue
            ctx, left = sentence_around(ans, c["start"], c["end"])
            # 摘掉引用文字本身（「《劳动合同法》第四十条」），否则条号自己就成了命中
            rel_s, rel_e = c["start"] - left, c["end"] - left
            clean = ctx[:rel_s] + ctx[rel_e:]
            body = art["content"]
            cov = len(ngrams(body) & ngrams(clean)) / max(1, len(ngrams(body)))
            lcs = lcs_len(body, clean)
            rows.append((cov, lcs, r["id"], c["id"]))
            print(f"  ── {c['id']}  cov={cov:.3f} lcs={lcs}")
            print(f"     条文: {body[:70]}".replace("\n", " "))
            print(f"     此处: {clean[:150]}".replace("\n", " "))
        print()

    print("=" * 74)
    print("按覆盖率升序（低 = 可能只是挂了个条号）")
    for cov, lcs, rid, cid in sorted(rows):
        print(f"  {cov:.3f}  lcs={lcs:<3} {rid:<5}{cid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
