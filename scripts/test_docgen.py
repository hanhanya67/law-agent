# -*- coding: utf-8 -*-
"""申请书生成的边界测试。纯函数，不碰网络、不碰库。

最要紧的一条断言是「请求事项里不许出现具体金额」。这不是格式洁癖：
请求事项决定仲裁委审什么，金额错一个数就可能当庭变更请求；而模型算的
金额再合理，也不是当事人确认过的。这条测试就是拿来挡住后来者「顺手
把金额填上，看起来更贴心」的改动。

跑法：
    ./.venv/Scripts/python.exe scripts/test_docgen.py
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.docgen import arbitration, cn_date, filename     # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


def section(text: str, start: str, end: str) -> str:
    a = text.find(start)
    b = text.find(end, a)
    return text[a:b] if a >= 0 and b > a else ""


FULL = {"monthly_wage": 8500, "has_contract": False, "start_date": "2023-06",
        "end_date": "2026-08", "leave_reason": "公司辞退", "region": "杭州"}


def main() -> int:
    print("\n== 日期格式 ==")
    ok("年月", cn_date("2023-06") == "2023年6月", cn_date("2023-06"))
    ok("年月日", cn_date("2023-06-15") == "2023年6月15日", cn_date("2023-06-15"))
    ok("空值给占位", "＿" in cn_date(None))
    ok("乱字符串给占位", "＿" in cn_date("去年"))

    print("\n== 完整案情：要素必须落到正文 ==")
    d = arbitration(FULL)
    t = d["text"]
    ok("标题正确", d["title"] == "劳动人事争议仲裁申请书")
    ok("标记为可提交", d["complete"] is True)
    ok("没有缺失必备项", d["missing"] == [], str(d["missing"]))
    ok("入职时间填入", "2023年6月入职" in t)
    ok("离职时间填入", "2026年8月解除" in t)
    ok("月工资填入", "8500元" in t)
    ok("未签合同写进事实", "双方未订立书面劳动合同" in t)
    ok("离职方式填入", "公司辞退" in t)

    print("\n== 请求事项必须是空的（核心约束）==")
    req = section(t, "仲裁请求", "事实与理由")
    ok("请求区块存在", bool(req))
    ok("请求区块里不出现用户月薪", "8500" not in req, req[:120])
    # 除了「第 1/2/3 项」这种序号，不该有任何阿拉伯数字金额
    money = re.findall(r"\d[\d,]*\s*元", req)
    ok("请求区块里没有任何具体金额", not money, "、".join(money))
    ok("请求区块里保留待填占位", "＿＿＿＿元" in req)

    print("\n== 法律依据也不许代写 ==")
    law = section(t, "三、法律依据", "证据清单")
    ok("法律依据区块不作实质陈述",
       "不要从别处抄条号" in law or "取决于你主张什么" in law, law[:80])
    ok("法律依据区块里不出现具体条号",
       not re.search(r"第[一二三四五六七八九十百零]+条", law), law[:80])

    print("\n== 事实与理由不代写叙述 ==")
    fact = section(t, "二、争议经过", "三、法律依据")
    ok("争议经过是待填提示，不是成品叙述",
       "用你自己的话" in fact, fact[:80])

    print("\n== 缺要素时要报警，且指向对的字段 ==")
    d2 = arbitration({"monthly_wage": 6000})
    ok("缺项时 complete=False", d2["complete"] is False)
    ok("缺入职时间被识别", "start_date" in d2["missing"], str(d2["missing"]))
    ok("缺离职方式被识别", "leave_reason" in d2["missing"])
    joined = "".join(d2["warnings"])
    ok("提示说清入职时间为什么重要", "在职时长" in joined, joined[:80])
    ok("提示说清离职方式决定 N 还是 2N", "2N" in joined or "赔偿金" in joined)
    ok("月薪已知时不报月薪缺失", "monthly_wage" not in d2["missing"])
    ok("不确定合同状态时给提示",
       any("书面合同" in w for w in d2["warnings"]), str(d2["warnings"])[:80])

    print("\n== 完全空白也不能崩 ==")
    d3 = arbitration({})
    ok("空案情仍能产出骨架", len(d3["text"]) > 500)
    ok("空案情 complete=False", d3["complete"] is False)
    ok("三处必备项全报", set(d3["missing"]) == {"start_date", "monthly_wage",
                                                "leave_reason"},
       str(d3["missing"]))
    ok("占位符足够填", d3["text"].count("＿＿") > 10)

    print("\n== 纯文本要求 ==")
    ok("正文没有 markdown 星号", "**" not in t, t[max(0, t.find("**")) - 30:][:80])
    ok("落款日期留空（提交日由本人填）", "＿＿＿＿ 年 ＿＿ 月 ＿＿ 日" in t
       or "＿＿＿＿年＿＿月＿＿日" in t)

    print("\n== 检查清单要能落地 ==")
    ck = "".join(d["checklist"])
    ok("提到管辖", "管辖" in ck)
    ok("提到时效", "时效" in ck)
    ok("提到不收费", "不收费" in ck)
    ok("提到手写签名", "手写" in ck)
    ok("引用了管辖条文", "第二十一条" in ck)
    ok("引用了时效条文", "第二十七条" in ck)
    ok("引用了收费条文", "第五十三条" in ck)

    print("\n== 原话只作线索，且要截断 ==")
    d4 = arbitration(FULL, statement="公司从去年就开始拖欠工资，" * 30)
    ok("原话出现在争议经过", "起草线索" in d4["text"])
    ok("原话被截到 200 字以内",
       "公司从去年就开始拖欠工资" * 30 not in d4["text"])
    d5 = arbitration(FULL, statement="")
    ok("没有原话时不出现线索段", "起草线索" not in d5["text"])

    print("\n== 文件名 ==")
    fn = filename(date(2026, 10, 29))
    ok("文件名带日期", "20261029" in fn, fn)
    ok("文件名是 .txt", fn.endswith(".txt"), fn)

    print("\n== 检查清单里的条号必须真实存在 ==")
    from app.config import CORPUS_PATH                      # noqa: E402
    from app.retrieval import LawIndex                      # noqa: E402
    ix = LawIndex(CORPUS_PATH)
    cited = set(re.findall(r"第([一二三四五六七八九十百零]+)条", ck))
    ok("清单引用了 3 条", len(cited) == 3, str(cited))
    # 这几条都在《劳动争议调解仲裁法》里
    real = {ix.by_id[f"调解仲裁法_{_n}"]["article_no_cn"]
            for _n in ("21", "27", "53") if f"调解仲裁法_{_n}" in ix.by_id}
    ok("清单里的条号都能在语料库核到",
       all(f"第{n}条" in real for n in cited), f"引用 {cited} / 实有 {real}")

    print("\n" + "=" * 58)
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("  失败：\n" + "\n".join("    · " + f for f in FAIL))
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
