# -*- coding: utf-8 -*-
"""法典图书馆回归（v139）——纯函数级，不起服务、不花钱、不写库。

为什么值得单开一套：图书馆把「按内核分库」这件事从检索扩到了**读原文**。
最容易坏的不是渲染，是三条边界悄悄松掉：
  · 某个内核的书目里混进别的法的条文（串库）
  · 跨内核传 article_id 竟然取到了条文（越界读）
  · 「要点摘录」被当成全文显示（假话）
还有一条最要紧：**主索引一个字节都不能改**。library 只读 LawIndex，
这里用「加载前后 labor 的条文数与检索分数逐位一致」把这条钉死。

跑法：./.venv/Scripts/python.exe scripts/test_library.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import library                     # noqa: E402
from app import profiles as _profiles       # noqa: E402
from app.retrieval import get_profile_index  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                            # noqa: BLE001
    pass

PASS: list[str] = []
FAIL: list[str] = []


def ok(tag: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(tag)
    print(f"  [{'PASS' if cond else 'FAIL'}] {tag}" + (f"  {extra}" if extra else ""))


def main() -> int:
    print("法典图书馆（v139）")
    print("=" * 60)

    # ---------- 1. 主索引零改动 ----------
    labor_before = get_profile_index(_profiles.resolve("labor"))
    n_before = len(labor_before.articles)
    scores_before = [round(s, 6) for s, _ in
                     labor_before.search("公司拖欠工资 经济补偿", topk=5)]
    b_labor = library.books("labor")
    n_after = len(get_profile_index(_profiles.resolve("labor")).articles)
    scores_after = [round(s, 6) for s, _ in
                    get_profile_index(_profiles.resolve("labor")).search(
                        "公司拖欠工资 经济补偿", topk=5)]
    ok("主索引条文数加载前后一致", n_before == n_after, f"{n_before}")
    ok("主索引 BM25 分数逐位不变", scores_before == scores_after,
       f"{scores_before[:2]}")
    ok("劳动法内核书目非空", b_labor["law_count"] >= 8,
       f"{b_labor['law_count']} 部")
    ok("劳动法内核含劳动合同法",
       any(l["law_short"] == "劳动合同法" for l in b_labor["laws"]))

    # ---------- 2. 按内核分库，不串库 ----------
    pids = list(_profiles.ids())
    ok("五个内核都有书目", len(pids) == 5)
    all_books = {}
    for pid in pids:
        b = library.books(pid)
        all_books[pid] = b
        ok(f"{pid} 书目非空且计数自洽",
           b["law_count"] > 0 and b["article_count"] > 0
           and sum(l["count"] for l in b["laws"]) == b["article_count"],
           f"{b['law_count']} 部 / {b['article_count']} 条")
    con = all_books["consumer"]
    con_laws = {l["law_short"] for l in con["laws"]}
    ok("消费内核只含本领域法律",
       con_laws <= {"消费者权益保护法", "食品安全法"} and len(con_laws) >= 2,
       str(sorted(con_laws)))
    ok("消费内核不混劳动法条文", "劳动合同法" not in con_laws)
    # 反向对照：两个 pid 确实取到不同集合，否则上面的「不混」是假绿。
    ok("反向对照：labor 与 consumer 书目不同",
       {l["law_short"] for l in b_labor["laws"]} != con_laws)
    civ = all_books["civil"]
    ok("合同内核不含消保法",
       "消费者权益保护法" not in {l["law_short"] for l in civ["laws"]})

    # ---------- 3. 目录树 ----------
    first = con["laws"][0]
    ok("目录有章节", len(first["chapters"]) >= 1,
       f"{first['law_short']} {len(first['chapters'])} 章")
    ch_total = sum(c["count"] for c in first["chapters"])
    ok("目录条文数与该法总数一致", ch_total == first["count"],
       f"{ch_total}/{first['count']}")
    ok("目录每条都有 id 与中文条号",
       all(a.get("id") and a.get("no_cn")
           for c in first["chapters"] for a in c["articles"]))
    ok("未分章的条文归到「（未分章）」而非空串",
       all(c["path"] for c in first["chapters"]))
    # 每个目录 id 都必须能回库取到原文，否则「点了目录跳不到条」。
    miss = [a["id"] for c in first["chapters"] for a in c["articles"]
            if library.article("consumer", a["id"]) is None]
    ok("目录里每个 id 都能回库取到原文", not miss, f"缺 {len(miss)} 条")

    # ---------- 4. 原文与字段白名单 ----------
    lt = library.law_text("consumer", first["law_short"])
    ok("整部法原文条数一致", lt["count"] == first["count"])
    ok("原文非空", all(a["content"] for a in lt["articles"]))
    a0 = lt["articles"][0]
    ok("字段白名单：不漏内部字段",
       "hierarchy_level" not in a0 and set(a0) <= {
           "id", "law_name", "law_short", "article_no", "article_no_cn",
           "chapter_path", "content", "status", "effective_date",
           "content_type", "note", "source_url"})
    ok("跨内核取条被拒", library.article("consumer", "劳动争议解释一_1") is None)
    ok("跨内核取条：反向对照同 pid 能取到",
       library.article("labor", "劳动争议解释一_1") is not None)
    # 摘录不能冒充全文。
    tr = all_books["traffic"]
    excerpt = [l for l in tr["laws"] if l["law_short"] == "道路交通安全法"]
    ok("道交法 4 条标为要点摘录",
       bool(excerpt) and excerpt[0]["content_type"] == "要点摘录"
       and excerpt[0]["count"] == 4,
       f"{excerpt[0]['content_type'] if excerpt else '缺'}")
    ok("摘录带说明文字", bool(excerpt and excerpt[0]["note"]))
    ok("全文法不误标摘录",
       all(l["content_type"] == "全文" for l in con["laws"]))

    # ---------- 5. 条号解析与快速查找 ----------
    for raw, want in [("585", 585), ("第585条", 585),
                      ("第五百八十五条", 585), (" 第 70 条 ", 70)]:
        ok(f"条号解析 {raw!r}", library.parse_article_no(raw) == want,
           str(library.parse_article_no(raw)))
    for raw in ("退货", "", "第abc条"):
        ok(f"非条号 {raw!r} 不走直达", library.parse_article_no(raw) is None)
    s_no = library.search("consumer", "第55条")
    ok("条号直达命中且模式正确",
       s_no["mode"] == "article_no" and len(s_no["items"]) >= 1,
       s_no["note"])
    ok("条号直达的结果条号确实对得上",
       all(str(a["article_no"]) == "55" for a in s_no["items"]))
    s_miss = library.search("traffic", "第99999条")
    ok("点名未收录条号明说、不悄悄降级成关键词",
       s_miss["mode"] == "article_no" and not s_miss["items"]
       and "未收录" in s_miss["note"])
    s_kw = library.search("consumer", "退货")
    ok("关键词检索模式正确且有结果",
       s_kw["mode"] == "keyword" and len(s_kw["items"]) >= 1,
       s_kw["note"])
    ok("关键词结果全属本内核语料",
       all(a["law_short"] in con_laws for a in s_kw["items"]))
    ok("空查询不发出无意义检索",
       library.search("consumer", "  ")["mode"] == "empty")
    ok("非法 pid 回落劳动法",
       library.books("not-a-profile")["pid"] == "labor")

    print("\n" + "=" * 60)
    print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：")
        for f in FAIL:
            print(f"    · {f}")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
