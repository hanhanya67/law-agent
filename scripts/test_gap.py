# -*- coding: utf-8 -*-
"""
gap 文案的回归测试。零 API、零依赖，纯字符串。

测两件事：
  1. `_clean_gap` 能挡掉「本库未收录」这类**对全库的断言**（模型看不到全库，说了就是编）；
     但不能误伤正常的「这件事由《X》调整」。
  2. `_irrelevant_text` 在 gap 被删掉之后，不能留出「您问的这部分，。」这种残句。

用例都来自真实踩过的坑，不是编的：
  · h11 实测原文：「现由《最高人民法院关于审理劳动争议案件适用法律问题的解释（二）》
    及个案具体情况调整，本库未收录该部分内容」——解释（二）就在库里，还是这题的 gold。
  · c21 实测原文：「工资在破产清算中的清偿顺序和优先受偿规则，由《企业破产法》调整」
    ——这句是对的，必须保留。

用法：
  ./.venv/Scripts/python.exe scripts/test_gap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import _irrelevant_text          # noqa: E402
from app.llm import _clean_gap                  # noqa: E402
from app.schemas import AgentState, Evidence, Verdict  # noqa: E402

KEEP = [
    "工资在破产清算中的清偿顺序和优先受偿规则，由《企业破产法》调整",
    "产假天数的具体标准，由《女职工劳动保护特别规定》调整",
    "住房公积金的缴存与追缴，由《住房公积金管理条例》调整",
]
DROP = [
    "现由《最高人民法院关于审理劳动争议案件适用法律问题的解释（二）》及个案具体情况调整，本库未收录该部分内容",
    "库内没有相应规定",
    "本库中没有关于差旅费负担的条文",
    "该事项本系统未收录",
]


def main() -> None:
    fails = 0

    for s in KEEP:
        if _clean_gap(s) != s:
            print(f"FAIL 应保留却被删：{s}")
            fails += 1
    for s in DROP:
        if _clean_gap(s) != "":
            print(f"FAIL 应删除却被保留：{s}")
            fails += 1
    if _clean_gap("") != "":
        print("FAIL 空串应返回空串")
        fails += 1

    ev = Evidence(article_id="劳动合同法_44", law_name="中华人民共和国劳动合同法",
                  article_no="44", article_no_cn="四十四", content="……")

    # gap 存在：文案里要出现这句
    st = AgentState(session_id="t", question="厂子倒闭了", evidences=[ev])
    st.verdict = Verdict(coverage="none", kind="unknown",
                         gap="工资在破产清算中的清偿顺序，由《企业破产法》调整")
    txt = _irrelevant_text(st)
    if "由《企业破产法》调整" not in txt:
        print("FAIL gap 应出现在拒答文案里")
        fails += 1

    # gap 为空：不能出现「您问的这部分，。」这类残句
    st.verdict = Verdict(coverage="none", kind="unknown", gap="")
    txt2 = _irrelevant_text(st)
    for bad in ("您问的这部分，。", "您问的这部分，\n", "，。"):
        if bad in txt2:
            print(f"FAIL 文案里出现残句：{bad!r}")
            fails += 1

    print(f"\n{'通过' if not fails else '失败'}：{len(KEEP) + len(DROP) + 4 - fails}/"
          f"{len(KEEP) + len(DROP) + 4} 项")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
