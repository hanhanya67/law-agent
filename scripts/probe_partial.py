# -*- coding: utf-8 -*-
"""
三态判定探针：如果给模型「能答一部分」这个选项，它会不会把该拒的题放行？

要解决的问题：
  c21「厂子倒闭了，欠我的工资还能拿到吗」现在整题拒答。库里第四十四条只答
  「破产时劳动合同终止」，答不了「欠的工资能不能拿到」——用户连那半个有用的事实
  都拿不到。直觉的修法是把 relevant 从布尔改三态（full/partial/none），partial 时
  答能答的部分。

  但 relevant 是整套系统最有效的一道闸：无答案题（产假、工伤、公积金）全靠它挡住
  「拿相近条文硬套」。开「部分相关」的口子，最坏结果是这些题被判 partial 然后硬答，
  闸就白装了。

实验设计（关键在「只改一处」）：
  变体 prompt **直接用产品 JUDGE_SYSTEM 改写而来**——只把输出项 relevant 换成
  coverage 三档，其余陷阱、例外、案例一个字不动。第一版探针是另写的 prompt，
  结果 r05（工装费，证据里有「不得以其他名义收取财物」）被判 none——但产品判定
  一直是答对的。差别不在三态，在我漏抄了「一般性条款不算无关」那条规则。
  这类差异必须消掉，否则测的是我的 prompt，不是三态。

    A 组  必须判 none（该拒题）
    B 组  应当判 partial（真正的「半可答」）
    C 组  必须判 full（正常可答题，不能降级）

  判读：A 组全 none、C 组全 full、B 组给出 partial → 三态可分，值得产品化。
        A 组出现 partial → 放水，否掉。
        C 组出现 partial/none → 会误伤现在答对的题，否掉。

用法：
  ./.venv/Scripts/python.exe scripts/probe_partial.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import prompts                                 # noqa: E402
from app.config import CORPUS_PATH                      # noqa: E402
from app.llm import LLM, _describe_evidence             # noqa: E402
from app.retrieval import LawIndex                      # noqa: E402
from app.schemas import CaseSlots, Evidence             # noqa: E402

_TRIPLE = """

但答案不是「相关 / 不相关」两档，而是三档。把用户的问题当成若干个子问题，
逐个看条文能不能给出明确结论：

  full     用户问到的每个子问题，条文都能给出明确结论
  partial  至少一个子问题条文能给出明确结论，但至少还有一个不能
  none     一个子问题都答不了（条文只是字面相近）

判 partial 的前提是**有一个子问题能给出明确结论**——具体到用户能拿着它去办事，
不是一个"也许有关"的方向。举例：
  · 问「厂子倒闭了欠的工资还能拿到吗」，条文讲了追讨欠薪的投诉与支付令途径，
    但没讲破产清算里的清偿顺序——**partial**：前者能答，后者不能。
  · 问「公司要求穿工装，工装费谁出」，条文写「不得以其他名义向劳动者收取财物」
    ——**full**。它直接管住这个行为，不需要点名「工装费」三个字。条文讲得比用户
    问的更一般，不等于只答了一半。

coverage 为 partial 时，另外给两个字段：
  answered_part  哪个子问能答、结论是什么（一句话）
  missing_part   哪个子问答不了、缺什么（一句话）
其余情况两个字段都填空字符串。"""

# 只改「变量」：把 relevant 换成 coverage，其余规则、陷阱、案例全部保留。
_base = prompts.JUDGE_SYSTEM
_base = _base.replace('"relevant": true/false,', '"coverage": "full|partial|none",')
_base = _base.replace("relevant", "coverage")
_base = _base.replace("coverage 为 false", "coverage 为 none")
_base = _base.replace("判 false", "判 none")
PROBE_SYSTEM = _base + _TRIPLE


def load_ev(index: LawIndex, ev_ids: list[str]) -> list[Evidence]:
    out = []
    for aid in ev_ids:
        a = index.by_id.get(aid)
        if not a:
            continue
        out.append(Evidence(article_id=a["id"],
                            law_name=a["law_name"],
                            article_no=str(a["article_no"]),
                            article_no_cn=a["article_no_cn"],
                            content=a["content"],
                            status=a.get("status", "现行有效")))
    return out


def main() -> None:
    index = LawIndex(CORPUS_PATH)
    rows = {}
    for f in ("eval/report_v18.json", "eval/report_holdout_v15.json",
              "eval/report_colloquial_holdout_v3.json"):
        for r in json.load(open(ROOT / f, encoding="utf-8")):
            rows.setdefault(r["id"], r)

    qs = {}
    for f in ("eval/questions.jsonl", "eval/holdout.jsonl",
              "eval/questions_colloquial_holdout.jsonl"):
        for line in open(ROOT / f, encoding="utf-8"):
            if line.strip():
                q = json.loads(line)
                qs[q["id"]] = q

    groups = {
        "A_必须none": ["m10", "r07", "r12", "h13"],
        "B_应当partial": ["c21"],
        "C_必须full": ["m01", "m02", "v01", "r05", "r14"],
    }

    llm = LLM()
    if not llm.online:
        print("没配 key，这个实验必须用真实模型")
        return

    tally = {"full": 0, "partial": 0, "none": 0}
    for gname, ids in groups.items():
        print("=" * 78)
        print(gname)
        print("=" * 78)
        for qid in ids:
            q = qs.get(qid)
            r = rows.get(qid)
            if not q or not r:
                print(f"{qid}  缺数据，跳过")
                continue
            evs = load_ev(index, r.get("ev_ids") or [])
            if not evs:
                print(f"{qid}  证据为空（不会走到 judge），跳过")
                continue
            user = (f"用户问题：{q['question']}\n"
                    f"已知案情：{CaseSlots().brief()}\n\n"
                    f"可用条文：\n{_describe_evidence(evs, full=True)}")
            try:
                resp = llm._chat(PROBE_SYSTEM, user, max_tokens=1200, allow_think=False)
                data = json.loads(resp.choices[0].message.content)
            except Exception as e:                        # noqa: BLE001
                print(f"{qid}  调用失败：{e}")
                continue
            cov = str(data.get("coverage", "?")).strip()
            tally[cov] = tally.get(cov, 0) + 1
            print(f"\n{qid}  [{cov}]  {q['question']}")
            if cov == "partial":
                print(f"    能答：{data.get('answered_part')}")
                print(f"    答不了：{data.get('missing_part')}")
            print(f"    理由：{data.get('reason') or data.get('why')}")

    print("\n" + "=" * 78)
    print("分布：", tally)
    print("判读：A 全 none、C 全 full、B 给 partial → 三态可分；"
          "A 出现 partial = 放水；C 出现 partial/none = 误伤。")


if __name__ == "__main__":
    main()
