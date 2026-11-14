# -*- coding: utf-8 -*-
"""
离线重算。用一份已有的评测明细（含模型原始答案），配上修正后的标签，重算全部指标。

为什么需要它：
  评测跑一轮 50 条要花 20 分钟、一元多钱。但如果只是「标签标错了」——gold 给错、
  keywords 写成中文全称、某题其实该拒——那就完全没有重跑模型的必要：
  模型的答案一个字没变，变的是评分标准。这时候该做的是拿旧答案按新标准算分。

  实测踩过的三个标签错误：
    s03  关键词写「百分之一百五十」，模型写「150%」→ 误报缺要点
    m10  gold 里的两条都答不了「加班费基数」→ 误报误拒（模型拒答本来是对的）
    r05  工装费其实被劳动合同法第9条直接涵盖 → 误报「该拒没拒」

重算能力取决于明细里存了什么：
  新版明细（run_eval.py 已加）存了 ev_ids 与 cites，可 100% 精确重算。
  旧版明细（如 v5）没有 ev_ids，只能降级推断，无法确定的条目会被标成「需重跑」，
  绝不拿旧数字冒充新数字。

用法：
  python eval/recompute.py --detail eval/report_online_v5.json \\
                           --out eval/report_online_v6.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agent import _find_article                  # noqa: E402
from app.config import CORPUS_PATH                   # noqa: E402
from app.retrieval import LawIndex, cn2int           # noqa: E402
from equiv import expand_gold                        # noqa: E402
from run_eval import (TYPE_LABEL, bar, extract_cites,  # noqa: E402
                      kw_hit, law_match, mode_summary, pct)


def recompute_one(r: dict, q: dict, index: LawIndex) -> tuple[dict, str]:
    """
    按新标签重算一条。返回 (指标 dict, 重算方式说明)。

    重算方式：
      exact     明细里有 ev_ids，全部精确重算
      inferred  旧明细，recall 由「gold 全被引用 + 无未核验」推断
      partial   旧明细，且 gold 无法从引用推断 → recall 记为 None 并列入需重跑
    """
    ans = r.get("answer") or ""
    cites = extract_cites(ans, index=index)   # 内部已按条文 id 去重
    gold = [g for g in (q.get("gold") or []) if g in index.by_id]
    ev_ids = r.get("ev_ids")
    ev_set = set(ev_ids) if ev_ids is not None else None

    # ---- 引用三分类：phantom 只要语料库就能重算，与证据集合无关 ----
    cite_rows, phantom = [], 0
    for law, no in cites:
        art = _find_article(index, law, no)
        cite_rows.append([law, no, art["id"] if art else None])
        if art is None:
            phantom += 1
    cited_ids = {c[2] for c in cite_rows if c[2]}

    # ---- unverified：必须有 ev_ids 才算得准 ----
    if ev_set is not None:
        unverified = sum(1 for c in cite_rows if c[2] and c[2] not in ev_set)
    else:
        unverified = r.get("unverified")

    # ---- recall ----
    # 这里的关键判断是「标准答案到底有没有动」，而不是「能不能重算」：
    #   旧明细（v5 那批）没存 ev_ids，如果 gold 一个字没改，那么明细里记着的
    #   recall 依然成立，直接沿用即可。只有当 gold 被改过、才必须重算。
    #   早先无差别重算，把 6 条 m 题（gold 未改但未被引用、证据集不可知）
    #   的召回记成 None 并从平均里踢掉，硬生生把召回率抬到 100%——这是
    #   典型的"分母少了所以分数变好看"，必须避免。
    if "gold" in r:                                  # 新格式：直接比对
        gold_changed = (r["gold"] or []) != (q.get("gold") or [])
    else:                                            # 旧格式：用旧 recall 是否为空反推
        gold_changed = ((r.get("recall") is None) != (len(gold) == 0))

    how = "exact" if ev_set is not None else "carry"
    if not gold:
        recall = None                                # 该拒答的题，召回不适用
    elif ev_set is not None:
        recall = len(expand_gold(gold, ev_set)) / len(gold)
        how = "exact"
    elif not gold_changed:
        recall = r.get("recall")                     # 标准答案没动，旧召回仍然有效
    elif len(expand_gold(gold, cited_ids)) == len(gold) and r.get("unverified") == 0:
        # gold 被改过，但每一条都被答案引用，且当时引用无一未核验 → gold 必在证据集内
        recall = 1.0
        how = "inferred"
    else:
        recall = None                                # 证据集不可知，不猜
        how = "partial"

    # ---- 引用质量：两把不需要标准答案的尺子 ----
    # 按条文 id 判等，不比法律名字符串（理由见 run_eval.py 同处注释）
    gold_ids = set(gold)
    correct = sum(1 for _, _, aid in cite_rows if aid in gold_ids)
    # precision 只作观测值保留（落盘供历史对比），不再进汇总表打分。
    # 撤销理由见 run_eval.py 汇总段：它的分母是「模型引了几条」，
    # 而 gold 只标核心 1–2 条，量出来的是「多不多」，不是「准不准」。
    precision = correct / len(cites) if (cites and gold_ids) else None
    # 可溯源率：引用的条文里有多少能在本次证据集合里找到出处。
    # 与 precision 的区别是它不需要标准答案，纯事实判定，没有阈值。
    traceable = (1.0 - unverified / len(cites)) if (cites and unverified is not None) else None

    # ---- 要点覆盖率：直接从答案全文重算，权威来源就是模型原话 ----
    kw = q.get("keywords") or []
    kw_cov = (sum(1 for k in kw if kw_hit(ans, k)) / len(kw)) if kw else None

    refused = r.get("refused", False)
    must_refuse = bool(q.get("must_refuse"))
    problems = []
    if gold and (recall is None or recall < 1):
        if how == "partial":
            problems.append("召回无法离线重算（旧明细缺证据集）")
        else:
            problems.append("漏召 " + ",".join(g for g in gold if g not in cited_ids))
    if phantom:
        problems.append(f"幻觉引用 {phantom}/{len(cites)}")
        # 「引用发散」不再报：多引几条合法条文不是缺陷
    if must_refuse and not refused:
        problems.append("该拒没拒" + ("（边界题，见附注）" if q.get("boundary") else ""))
    if not must_refuse and refused:
        problems.append("误拒")
    if kw_cov is not None and kw_cov < 1 and not refused:
        problems.append("缺要点 " + ",".join(k for k in kw if not kw_hit(ans, k)))

    return {
        "id": q["id"], "type": q.get("type"), "question": q["question"],
        "recall": recall, "precision": precision, "kw_cov": kw_cov,
        "traceable": traceable,
        "n_cites": len(cites), "phantom": phantom, "unverified": unverified,
        "refused": refused, "ms": r.get("ms"), "status": r.get("status"),
        "zero_hit": r.get("zero_hit"), "steps": r.get("steps"),
        # 来源。旧明细没有这个字段（它是后加的）→ 记 unknown，
        # 汇总时 unknown 与回放一样不进「可信」样本，避免老数据冒充模型成绩。
        "mode": r.get("mode") or "unknown",
        "problems": problems, "boundary": bool(q.get("boundary")),
        "blocked_by": q.get("blocked_by") or "",
        "ev_ids": sorted(ev_set) if ev_set is not None else None,
        "cites": cite_rows, "gold": q.get("gold") or [],
        "keywords": kw, "must_refuse": must_refuse,
        "answer": ans,
    }, how


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", default="eval/report_online_v5.json")
    ap.add_argument("--questions", default="eval/questions.jsonl")
    ap.add_argument("--baseline", default="",
                    help="可选：旧版汇总数字的 markdown，用于对比")
    ap.add_argument("--out", default="eval/report_recomputed.md")
    ap.add_argument("--assume-online", action="store_true",
                    help="这份明细当时是在线跑的（明细里没有 mode 字段时用）。"
                         "报告会写明来源来自命令行声明，不是明细记录。")
    args = ap.parse_args()

    detail_path = ROOT / args.detail
    old = json.loads(detail_path.read_text(encoding="utf-8"))
    old_by_id = {r["id"]: r for r in old}
    qs = [json.loads(l) for l in (ROOT / args.questions).open(encoding="utf-8")
          if l.strip()]
    index = LawIndex(CORPUS_PATH)
    by_id = {r["id"]: r for r in old}

    results, modes, changes = [], {}, []
    for q in qs:
        r = by_id.get(q["id"])
        if r is None:
            print(f"[warn] 明细里没有 {q['id']}，跳过")
            continue
        new, how = recompute_one(r, q, index)
        results.append(new)
        modes[q["id"]] = how

        # 只记录真正发生了变化的条目。
        # 旧明细里没有 gold/keywords 字段，若拿它跟新标签比会全部判「改了」，
        # 台账变成 50 行噪音。所以以「题型 + 实测指标」为准——这两个字段旧明细都有。
        diff = []
        if r.get("type") and r["type"] != q.get("type"):
            diff.append(f"题型 {r['type']}→{q.get('type')}"
                        f"（{'需拒答' if q.get('must_refuse') else '有答案'}）")
        if r.get("recall") != new["recall"]:
            diff.append(f"召回 {pct(r.get('recall'))}→{pct(new['recall'])}")
        if r.get("kw_cov") != new["kw_cov"]:
            diff.append(f"要点覆盖 {pct(r.get('kw_cov'))}→{pct(new['kw_cov'])}")
        if r.get("unverified") != new["unverified"]:
            diff.append(f"未核验 {r.get('unverified')}→{new['unverified']}")
        if diff:
            changes.append((q["id"], diff))

    # ---------------- 汇总（口径与 run_eval.py 完全一致）----------------
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    must_refuse = [r for r in results if r["must_refuse"]]
    should_answer = [r for r in results if not r["must_refuse"]]

    # 旧明细没有 mode 字段（那字段是后加的），默认算「来源不明」、不进可信样本：
    # 无法确认的东西不能冒充在线。确实知道它是在线跑的，用 --assume-online 声明，
    # 报告里会写清这是声明、不是记录。
    assumed = 0
    if args.assume_online:
        for r in results:
            if r["mode"] == "unknown":
                r["mode"] = "online"
                assumed += 1

    # 来源分开算。检索层的指标（召回、溯源、合法率、零命中拒答）不依赖语义，
    # 回放条也算数；语义层的（要点覆盖、拒答准确率、误拒）在回放条上是规则模拟
    # 出来的，混进汇总就等于把模板答案当成模型成绩。后者只取在线条。
    n_online, n_partly, n_replay = mode_summary(results)
    n_unknown = len(results) - n_online - n_partly - n_replay   # 旧明细没有 mode
    sa_online = [r for r in should_answer if r["mode"] == "online"]
    mr_online = [r for r in must_refuse if r["mode"] == "online"]
    n_dirty = len(results) - n_online      # 半程 + 回放 + 来源不明

    def dirty_note(base: str) -> str:
        # 一条在线样本都没有时更得说明：光秃秃一个「—」会被当成脚本算崩了
        if n_dirty == 0:
            return base
        return (f"{base}（另有 {n_dirty} 条回放/半程/来源不明未计入，"
                f"它们算不出模型能力）")

    m_recall = avg([r["recall"] for r in should_answer])
    m_kw = avg([r["kw_cov"] for r in sa_online])
    m_refuse = avg([1.0 if r["refused"] else 0.0 for r in mr_online])
    m_false = avg([1.0 if r["refused"] else 0.0 for r in sa_online])
    n_cites = sum(r["n_cites"] for r in should_answer)
    n_phantom = sum(r["phantom"] for r in should_answer)
    n_unverified = sum(r["unverified"] or 0 for r in should_answer)
    m_legal = 1.0 - n_phantom / n_cites if n_cites else None
    # 按引用加权，不按题平均（理由见 run_eval.py）
    m_trace = (1.0 - n_unverified / n_cites) if n_cites else None
    dense = n_cites / len(should_answer) if should_answer else None
    # 零命中拒答不依赖语义（"没东西可说就不说"），回放条也算数，故按全量。
    zh = [r for r in must_refuse if r["zero_hit"]]
    m_zero = avg([1.0 if r["refused"] else 0.0 for r in zh])

    # 剔除边界题后的拒答准确率，作为参考口径（同样只取在线条）
    strict = [r for r in mr_online if not r["boundary"]]
    m_refuse_strict = avg([1.0 if r["refused"] else 0.0 for r in strict])

    partial = [r["id"] for r in results if modes[r["id"]] == "partial"]
    inferred = [r["id"] for r in results if modes[r["id"]] == "inferred"]

    lines = [""]
    lines.append("=" * 66)
    lines.append("汇总（离线重算）")
    lines.append("=" * 66)
    rows = [
        ("证据召回率", m_recall, f"n={len(should_answer)}，该找到的条文找到没有"),
        ("可溯源率", m_trace,
         f"共 {n_cites} 处引用，其中 {n_unverified} 处没有出处（应为 100%）"),
        ("引用合法率", m_legal,
         f"共 {n_cites} 处引用，其中 {n_phantom} 处条号库里不存在（应为 100%）"),
        ("要点覆盖率", m_kw, dirty_note(f"n={len(sa_online)}，关键结论说没说全")),
        ("零命中拒答率", m_zero,
         f"n={len(zh)}，检索完全无结果时是否正确拒答（不靠语义，回放条也算数）"),
        ("误拒率", m_false, dirty_note(f"n={len(sa_online)}，不该拒的有没有被拒"
                                   f"（越低越好）")),
    ]
    for name, val, note in rows:
        lines.append(f"{name:<12}{pct(val):>8}  {bar(val)}  {note}")
    lines.append("")
    if n_unverified:
        lines.append(f"未核验引用 {n_unverified} 处：条文真实存在、但没进本次证据")
        lines.append("  ↑ 这批是「旧答案 + 新抽取器」才暴露出来的引用——当时的产品层只认书名号，")
        lines.append("    没把它们抽出来，所以既没进证据也没记成幻觉。用当前代码跑同一批答案，")
        lines.append("    补核验会把它们全部取回，可溯源率达到 100%。验证方法：")
        lines.append("      python scripts/ab_backfill.py <本报告的 detail 文件> <题库>")
    else:
        lines.append("未核验引用 0 处：答案引用的每一条都在证据集合内，可逐句溯源")
    if dense is not None:
        lines.append(f"引用密度（观测值，不打分）平均每题 {dense:.1f} 条")
    # 来源分布先说清，否则下面几个 "—" 会被当成脚本算崩了
    if n_dirty or assumed:
        bits = [f"在线 {n_online}"]
        if n_partly:
            bits.append(f"半程 {n_partly}")
        if n_replay:
            bits.append(f"回放 {n_replay}")
        if n_unknown:
            bits.append(f"来源不明 {n_unknown}（明细里没有 mode 字段，多为旧版）")
        lines.append("")
        lines.append(f"本明细 {len(results)} 条的来源：" + " / ".join(bits))
        if n_dirty:
            lines.append("  语义类指标（要点覆盖、拒答准确率、误拒率）只取在线条：")
            lines.append("  回放条是规则模拟的、半程条混着两种来源，混进去等于把模板答案")
            lines.append("  当成模型成绩。检索类指标按全量算，它们不依赖语义。")
        if assumed:
            lines.append(f"  · 其中 {assumed} 条按 --assume-online 当作在线计入——"
                         f"这是**命令行声明**，不是明细记录的值。")
    lines.append(f"拒答准确率（在线 {len(mr_online)} 条）{pct(m_refuse)}")
    if not mr_online and must_refuse:
        lines.append(f"  ↑ 本明细没有在线的需拒答题（{len(must_refuse)} 条都不是在线跑的），"
                     f"该项无从算起，不是 0%")
    if strict and len(strict) != len(mr_online):
        lines.append(f"拒答准确率（剔除边界题，n={len(strict)}）{pct(m_refuse_strict)}")

    # ---- 误拒归因：把「库不行」和「模型不行」拆开 ----
    # 一个笼统的误拒率 17% 看不出该怎么改。按归因拆开后往往发现：
    # 绝大多数误拒是语料缺口造成的，补库即可消掉，模型本身没错。
    false_rej = [r for r in should_answer if r["refused"]]
    if false_rej:
        from collections import Counter
        tag = Counter(r["blocked_by"] or "unexplained" for r in false_rej)
        label = {"corpus": "语料缺口（待补库）",
                 "router": "意图路由误判（待改规则）",
                 "unexplained": "无法归因（真·模型缺陷）"}
        lines.append("")
        lines.append(f"误拒 {len(false_rej)} 条的归因：")
        for k in ("corpus", "router", "unexplained"):
            if tag.get(k):
                ids = [r["id"] for r in false_rej if (r["blocked_by"] or "unexplained") == k]
                lines.append(f"  · {label[k]:<16}{tag[k]:>2} 条  {','.join(ids)}")

    # ---------------- 标签修改台账 ----------------
    lines.append("")
    lines.append("=" * 66)
    lines.append(f"本次重算的标签修改（{len(changes)} 条）")
    lines.append("=" * 66)
    for qid, diff in changes:
        lines.append(f"[{qid}] " + "；".join(diff))

    # ---------------- 失败明细 ----------------
    fails = [r for r in results if r["problems"]]
    lines.append("")
    lines.append("=" * 66)
    lines.append(f"失败明细（{len(fails)} 条）")
    lines.append("=" * 66)
    for r in fails:
        lines.append(f"[{TYPE_LABEL.get(r['type'], r['type'])}] {r['id']}  {r['question']}")
        lines.append(f"    {'；'.join(r['problems'])}")

    # ---------------- 能重算到什么程度 ----------------
    lines.append("")
    lines.append("=" * 66)
    lines.append("重算能力说明")
    lines.append("=" * 66)
    n_exact = sum(1 for m in modes.values() if m == "exact")
    n_carry = sum(1 for m in modes.values() if m == "carry")
    if n_exact:
        lines.append(f"按证据集合重算 {n_exact} 条（明细里存了 ev_ids，全部指标可算）")
    if n_carry:
        lines.append(f"沿用旧召回 {n_carry} 条（gold 一个字没改，"
                     f"同一 gold + 同一证据集 = 同一结果，无需重算）")
    if inferred:
        lines.append(f"推断重算 {len(inferred)} 条（{','.join(inferred)}）："
                     f"gold 全被答案引用且当时无未核验引用，故 gold 必在证据集合内")
    if partial:
        lines.append(f"无法重算 {len(partial)} 条（{','.join(partial)}）："
                     f"gold 未被引用，证据集合不可知，召回记为 None 且不计入平均")
    lines.append("")
    lines.append("=" * 66)
    lines.append("指标口径")
    lines.append("=" * 66)
    lines.append(
        "本版撤掉了「依据聚焦度」。它的分母是「模型引了几条」，而 gold 只标 1–2 条\n"
        "核心条文，量出来的是「引得多不多」而不是「引得准不准」。替代它的是\n"
        "「可溯源率」：每处引用能否在本次证据集合里找到出处，纯事实判定、无阈值。\n"
        "完整论证见 run_eval.py 汇总段的「为什么撤掉依据聚焦度」。")
    lines.append("")
    lines.append(f"注意：本报告**不是重跑模型**，模型答案与 `{detail_path.name}` 中的逐字相同。")
    lines.append("变的是评分标准（标签修正）。要验证模型本身有没有变好，得看对应的在线评测报告。")

    report = "\n".join(lines)
    print(report)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# 评测报告（离线重算）\n\n"
              f"生成时间：{__import__('time').strftime('%Y-%m-%d %H:%M')}  \n"
              f"数据来源：`{args.detail}` 中的模型原始答案  \n"
              f"评分依据：`{args.questions}` 修正后的标签  \n"
              f"题数：{len(results)}（单跳 {sum(1 for r in results if r['type'] == 'single')} / "
              f"多跳 {sum(1 for r in results if r['type'] == 'multi')} / "
              f"版本 {sum(1 for r in results if r['type'] == 'version')} / "
              f"无答案 {sum(1 for r in results if r['type'] == 'refuse')}）  \n"
              f"语料：{index.stats()['articles']} 条条文\n\n"
              "> **这不是重跑模型。** 模型的答案与上一轮逐字相同，改的只是评分标准。\n"
              "> 用途是：把「标错了」和「模型答错了」区分开，避免为标签错误付一次 API 账单。\n")
    out.write_text(header + "\n" + report + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out}")
    print(f"明细已写入 {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
