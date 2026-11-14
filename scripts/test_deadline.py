# -*- coding: utf-8 -*-
"""期限计算的边界测试。不依赖 LLM、不依赖服务、不花钱。

为什么这个脚本比其它测试更重要：金额算错用户自己能看出来，日期算错
用户会照着那个日期行动——早一天晚一天决定的是案子能不能立。

跑法：
    ./.venv/Scripts/python.exe scripts/test_deadline.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.deadline import (add_months, compute, compute_deadline,  # noqa: E402
                          parse_date)

PASS, FAIL = [], []
AS_OF = date(2026, 10, 29)          # 全部用例固定基准日，否则明天跑就红


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


def main() -> int:
    print("\n== 期间算术（民法典 201/202 条）==")
    ok("一年：起算日次年对应日为末日",
       compute_deadline(date(2024, 1, 1), months=12) == date(2025, 1, 1),
       str(compute_deadline(date(2024, 1, 1), months=12)))
    ok("一年：不是提前一天",
       compute_deadline(date(2024, 1, 1), months=12) != date(2024, 12, 31))
    ok("15 日：起始当日不计入（01-01 + 15 日 = 01-16）",
       compute_deadline(date(2024, 1, 1), days=15) == date(2024, 1, 16),
       str(compute_deadline(date(2024, 1, 1), days=15)))
    ok("闰日起算：2024-02-29 加一年取 2025-02-28（202 条没有对应日取月末）",
       add_months(date(2024, 2, 29), 12) == date(2025, 2, 28),
       str(add_months(date(2024, 2, 29), 12)))
    ok("月末起算：2024-01-31 加一月取 2024-02-29（闰年）",
       add_months(date(2024, 1, 31), 1) == date(2024, 2, 29),
       str(add_months(date(2024, 1, 31), 1)))
    ok("月末起算：2025-01-31 加一月取 2025-02-28（平年）",
       add_months(date(2025, 1, 31), 1) == date(2025, 2, 28),
       str(add_months(date(2025, 1, 31), 1)))
    ok("跨年加法：2025-11 加 3 月 = 2026-02",
       add_months(date(2025, 11, 15), 3) == date(2026, 2, 15),
       str(add_months(date(2025, 11, 15), 3)))

    print("\n== 日期解析 ==")
    ok("解析 2024-03-05", parse_date("2024-03-05") == date(2024, 3, 5))
    ok("解析 2024年3月5日", parse_date("2024年3月5日") == date(2024, 3, 5))
    ok("解析 2024/3/5", parse_date("2024/3/5") == date(2024, 3, 5))
    ok("只给年月按月初", parse_date("2024-03") == date(2024, 3, 1))
    ok("只给年份按年初", parse_date("2024") == date(2024, 1, 1))
    ok("「去年冬天」解析不出 → None", parse_date("去年冬天") is None)

    print("\n== 讨薪：关系状态三态 ==")
    # 这一组是本次修的 bug 的防回归。原实现把「知道之日」也算成候选，
    # 而它比终止之日更早，于是被选成「最保守」→ 误告用户已过期。
    r = compute("wage", "2025-01-01", relation_ended=True,
                end_text="2025-06-30", as_of=AS_OF)
    ok("已终止：从终止之日起算", r["conservative"]["onset"] == "2025-06-30",
       r["conservative"]["onset"])
    ok("已终止：**不**拿更早的「知道之日」当起算点",
       r["conservative"]["onset"] != "2025-01-01")
    ok("已终止：截止日 = 终止日 + 1 年",
       r["conservative"]["deadline"] == "2026-06-30",
       r["conservative"]["deadline"])
    ok("已终止：已过期要判 expired", r["conservative"]["level"] == "expired")

    r = compute("wage", "2025-01-01", relation_ended=False, as_of=AS_OF)
    ok("在职：标记为不受时效限制", r.get("unlimited") is True)
    ok("在职：**不**给截止日（法律上不存在这个期限）",
       r["conservative"] is None and not r["candidates"])
    ok("在职：说明离职后才开始算一年",
       any("离职" in n for n in r["notes"]))

    # 页面上的终止日期输入框被隐藏后值还在。用户先填过日期、再点回「还在职」
    # 重新算，后端如果采信这个残留值，就会给出一个离职的截止日——用户点了
    # 在职，拿到的却是离职的结论。明示的状态必须盖过残留字段。
    r = compute("wage", "2025-01-01", relation_ended=False,
                end_text="2025-06-30", as_of=AS_OF)
    ok("在职 + 残留终止日期：仍按不受时效限制",
       r.get("unlimited") is True and r["conservative"] is None,
       str(r.get("branch")))
    ok("在职 + 残留终止日期：说明那个日期没被采用",
       any("没有采用" in n for n in r["notes"]), str(r["notes"])[:90])

    r = compute("wage", "2025-01-01", relation_ended=None, as_of=AS_OF)
    ok("没说是否在职：不给截止日", r["conservative"] is None)
    ok("没说是否在职：标记为条件性", r.get("conditional") is True)
    ok("没说是否在职：**不**冤枉用户说「你说了已终止」",
       "你说了劳动关系已终止" not in "".join(r["notes"]),
       "".join(r["notes"])[:60])
    ok("没说是否在职：两种情形都摆出来",
       "还在职" in "".join(r["notes"]) and "已离职" in "".join(r["notes"]))

    r = compute("wage", "2025-01-01", relation_ended=True, as_of=AS_OF)
    ok("说了已终止但没给日期：必须警告偏乐观",
       any("偏乐观" in n or "缺" in n for n in r["notes"]),
       str(r["notes"])[:80])
    ok("说了已终止但没给日期：仍然给出结果而不是报错", r["ok"] is True)

    print("\n== 阶段措辞必须跟着走 ==")
    r = compute("arbitration_award", "2026-10-20", as_of=AS_OF)
    joined = "".join(r["notes"])
    ok("起诉期紧急时提示「起诉」", "起诉" in joined, joined[:60])
    ok("起诉期紧急时**不**再劝「递交仲裁申请」",
       "递交仲裁申请" not in joined and "把仲裁申请递上去" not in joined,
       joined[:60])
    ok("15 日起诉期算对", r["conservative"]["deadline"] == "2026-11-04",
       r["conservative"]["deadline"])

    # 一年期，起算在 2025-11-05 → 截止 2026-11-05，距基准日 7 天
    r = compute("dismissal", "2025-11-05", as_of=AS_OF)
    ok("仲裁阶段剩余 7 天时判 urgent", r["conservative"]["level"] == "urgent",
       r["conservative"]["level"])
    ok("仲裁阶段紧急时劝「递仲裁申请」而不是起诉",
       "仲裁" in "".join(r["notes"]) and "向法院起诉" not in "".join(r["notes"]),
       "".join(r["notes"])[:60])

    print("\n== 本库没有的法 ==")
    r = compute("injury", "2026-01-01", as_of=AS_OF)
    ok("工伤：标记为不支持", r["ok"] is False and r.get("unsupported") is True)
    ok("工伤：明确说本库未收录工伤保险条例",
       "未收录" in r.get("error", ""), r.get("error", ""))
    ok("工伤：给的一般规则标注为「未核验」",
       "没有经过本库核验" in r.get("hint", ""), r.get("hint", "")[:60])

    print("\n== 输入异常 ==")
    ok("乱日期报错不抛异常", compute("dismissal", "去年冬天", as_of=AS_OF)["ok"] is False)
    ok("起算日晚于今天要报错",
       compute("dismissal", "2027-01-01", as_of=AS_OF)["ok"] is False)
    ok("未知争议类型要报错",
       compute("nonsense", "2026-01-01", as_of=AS_OF)["ok"] is False)

    print("\n== 只给年月的估算提示 ==")
    r = compute("dismissal", "2025-03", as_of=AS_OF)
    ok("按月初估算时要告诉用户", any("月 1 日" in n or "月初" in n for n in r["notes"]),
       str(r["notes"])[:80])

    print("\n== 末日落在周末 ==")
    r = compute("arbitration_award", "2026-10-17", as_of=AS_OF)
    # 2026-10-17 + 15 天 = 2026-11-01，周日
    ok("末日是周日：给出顺延提示",
       "holiday_hint" in (r["conservative"] or {}),
       str(r["conservative"]))
    ok("末日是周日：**不自动顺延**日期（各地口径不一，不能替用户决定）",
       r["conservative"]["deadline"] == "2026-11-01",
       r["conservative"]["deadline"])

    print("\n== 引用必须落在语料库里 ==")
    from app.config import CORPUS_PATH                     # noqa: E402
    from app.retrieval import LawIndex                     # noqa: E402
    ix = LawIndex(CORPUS_PATH)
    missing = []
    for dispute in ("wage", "dismissal", "no_contract",
                    "arbitration_award", "arbitration_other", "employer_revoke"):
        rr = compute(dispute, "2025-06-01", relation_ended=True,
                     end_text="2025-06-30", as_of=AS_OF)
        for aid in rr["articles"]:
            if aid not in ix.by_id:
                missing.append(f"{dispute}→{aid}")
    ok("六种争议引用的条文全部真实存在", not missing, "、".join(missing))

    print("\n" + "=" * 58)
    print(f"  {len(PASS)} / {len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("  失败：\n" + "\n".join("    · " + f for f in FAIL))
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
