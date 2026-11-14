# -*- coding: utf-8 -*-
"""
评测脚本。跑一遍评测集，输出五个指标。

为什么要这份东西：
  改检索策略、改 Prompt、加工具——每一步改完你都得回答一个问题：
  "这次是变好了还是变坏了？" 没有固定评测集，这个问题只能靠感觉回答。
  有了它，每次改动跑一遍，数字升了就是升了，降了就知道哪几条坏了。

五个指标的分工：
  证据召回率   检索层的问题（该找到的条文没找到）
  条款精确率   生成层的问题（引用了不在证据里的条文，也就是编）
  要点覆盖率   表达层的问题（找到了、引对了，但没说到关键点）
  拒答准确率   该拒的有没有拒
  误拒率       不该拒的有没有被拒掉
  ← 后两个必须成对看。一个"什么都拒答"的系统，拒答准确率能到 100%，
    但完全没用。只看一个指标可以作弊。

用法：
  python eval/run_eval.py                 # 回放模式，不需要 API key
  python eval/run_eval.py --limit 10      # 先跑 10 条快速看
  python eval/run_eval.py --online        # 配了 key 时用真实模型跑
  python eval/run_eval.py --out eval/report.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import AgentRunner, _find_article, law_same, norm_law_key  # noqa: E402
from app.cite import dedup_by_id as _dedup_by_id  # noqa: E402
from app.cite import extract_cites                # noqa: E402
from app.config import CORPUS_PATH, LLM_ENABLED, LLM_RECOVER_AFTER  # noqa: E402
from app.llm import LLM                        # noqa: E402
from app.memory import MemoryStore             # noqa: E402
from app.retrieval import LawIndex, cn2int     # noqa: E402
from equiv import expand_gold                  # noqa: E402  同目录

TYPE_LABEL = {"single": "单跳", "multi": "多跳", "version": "版本", "refuse": "无答案"}
# 逐条答案的来源标签。partly = 开头在线、跑到一半降级，两种来源混在同一段答案里。
MODE_TAG = {"online": "", "replay": "·回放", "partly": "·半回放"}
# 早期明细没有 mode 字段（合并旧基线时会遇到），兜底标签不能 KeyError 崩在出报告时。
MODE_UNKNOWN_TAG = "·来源不明"


def mode_of(was_online: bool, now_online: bool) -> str:
    """这一条答案的来源。`was_online` 是开跑前的状态，`now_online` 是跑完后的。

    开头在线、结束时掉线 = 跑到一半降级。抽成函数是因为这段判定错了很安静：
    判成 online，模板拼的答案会被当成模型成绩；判成 replay，又冤枉了前半段。
    """
    if not was_online:
        return "replay"
    return "online" if now_online else "partly"


def mode_summary(results: list[dict]) -> tuple[int, int, int]:
    """（在线条数, 半程条数, 回放条数）。报告的模式行与注意事项都按这个说话。

    不能用 `llm.online` 代替它——那是最后一条跑完时的状态，中途降级的评测
    会被整份说成「回放模式」，前面几十条在线的分数跟着背锅。
    """
    n_online = sum(1 for r in results if r.get("mode") == "online")
    n_replay = sum(1 for r in results if r.get("mode") == "replay")
    n_partly = sum(1 for r in results if r.get("mode") == "partly")
    # online 要显式数出来、不能用总数去减：mode 是后加的字段，早期落盘的明细
    # json 里没有它，一减就把那些来源不明的条算成「在线」，报告随即宣称可信。
    # 宁可少算一条，也不能把来源不明的分数说成模型成绩。
    return n_online, n_partly, n_replay


def start_mode(llm) -> bool:
    """这一条**真正**开跑时的在线状态。

    必须先自己调一次 `maybe_recover()`：`AgentRunner` 是在 `run()` 内部开头调的
    （app/agent.py），调用方在 run() 之前问，拿到的还是「上一条跑完时的状态」。
    于是「上一条降级、冷却已过、这一条开头被救回在线」会被记成 replay——
    可它全程是在线答的，那句「分数不代表模型能力」就冤枉它了。

    这里先恢复再问，与 run() 内部是同一个动作：`online` 已经是 True 时
    `maybe_recover()` 直接返回，不会多试一次、也就不会多烧一次调用。
    """
    llm.maybe_recover()
    return llm.online


def mode_note(mode: str | None) -> str:
    """一条答案的来源说明，给「打开一条答案看」的场景用。

    看单条答案时最先要知道的是它谁写的：模板拼的答案读起来也像人话，
    照着它去调 Prompt 等于对着空气使劲。在线不返回文案——正常情况不该有噪音。
    """
    return {
        "online": "",
        "replay": "回放：模板加检索条文拼的，没有模型生成的结论",
        "partly": "半程：开头在线、跑到一半降级，前半是模型写的、后半是模板拼的",
    }.get(mode or "unknown",
          "来源不明：明细里没有 mode 字段（加字段之前跑的），"
          "无法确定这是不是模型写的")


def merge_results(base: list[dict], new: list[dict]) -> list[dict]:
    """把本次重跑的几条并回完整基线，返回完整集合。

    两件事容易错，都静默：
      1. 题数要按 base 走。只在落盘 json 时合并、报告仍用本次 results 的话，
         复跑 1 条出来的报告会写「题数：1」，所有 `n=` 都按 1 算，
         读起来像整套评测只有一道题。
      2. base 里没有的新 id 必须追加。`[new.get(r["id"], r) for r in base]`
         会把它们直接丢掉——重跑一条基线里不存在的题，结果就没了。
    """
    new_by_id = {r["id"]: r for r in new}
    base_ids = {r["id"] for r in base}
    return ([new_by_id.get(r["id"], r) for r in base]
            + [r for r in new if r["id"] not in base_ids])


def rerun_cmd(ids: list[str], detail: Path, out: Path) -> str:
    """给半程条拼一条能直接粘去跑的定点复跑命令。空列表返回空串。

    半程条是跑到一半降级留下的，答案混着模型写的和模板拼的。故障排除后
    只重跑这几条就够了，不必整轮重烧一遍 API。
    """
    if not ids:
        return ""
    return (f"./.venv/Scripts/python.exe eval/run_eval.py"
            f" --ids {','.join(ids)}"
            f" --merge-into {detail}"
            f" --out {out} --online")
    # 带 --online 不是多余：没配 key 或降级未恢复时它会在开头就警告出来，
    # 免得又跑一轮安静的回放、把模板答案再当一次模型成绩。


def norm_law(s: str) -> str:
    """法律名归一。与 app.agent.norm_law_key 保持一致：去「中华人民共和国」、去括号。

    括号必须去掉：答案写「《解释（一）》」或「《解释一》」，gold 的 law_short 是
    「劳动争议解释一」，不归一就判不成同一部法，合法引用会被误判成幻觉条号。
    """
    return norm_law_key(s)


# 这些尾巴说明「短名 + 尾巴」是另一部法，不是同一个法的全称
_SUFFIX_BLOCK = ("实施", "条例", "办法", "规定", "细则", "决定", "解释", "通知")


def law_match(a: str, b: str) -> bool:
    """判断答案里写的法律名与 gold 的法律名是不是同一部法。

    不能简单用 a in b or b in a：那样《劳动合同法》会吃掉《劳动合同法实施条例》，
    而两部法条号 1–38 完全重叠。s11 的 gold 正是实施条例第 27 条，一旦模型答
    《劳动合同法》第 27 条（讲合同无效），就会被误判成"答对了"。
    """
    return law_same(norm_law(a), norm_law(b))


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


# 要点覆盖率的尺子问题：同一个结论有两种合法写法，答案写「80%」而关键词写
# 「百分之八十」，就会被判成"没说"。这不是模型漏说，是尺子在扣分。
# 处理办法是把两边都归一化成阿拉伯数字形式再比，而不是去猜模型会用哪种写法。
_PCT_CN = re.compile(r"百分之([零一二三四五六七八九十百千两]+)")
_CN_DIGIT = "零一二三四五六七八九十百千两"
_NUM_UNIT = re.compile(rf"([{_CN_DIGIT}]+)\s*(小时|个月|个|年|日|天|周|月|元|倍|次|人|份|级)")


def _norm_num(s: str) -> str:
    """把「百分之八十」「三个月」「三十日」统一成「80%」「3个月」「30日」。"""
    s = _PCT_CN.sub(lambda m: f"{cn2int(m.group(1))}%", s)

    def _u(m: re.Match) -> str:
        n = cn2int(m.group(1))
        return f"{n}{m.group(2)}" if n is not None else m.group(0)

    return _NUM_UNIT.sub(_u, s)


def kw_hit(text: str, kw: str) -> bool:
    """关键词命中判断：原文直配，或数字写法归一化后命中。"""
    if kw in text:
        return True
    return _norm_num(kw) in _norm_num(text)


def bar(x: float | None, width: int = 18) -> str:
    if x is None:
        return ""
    n = int(round(x * width))
    return "█" * n + "·" * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--ids", default="", help="只跑指定 id，逗号分隔（用于定点复跑）")
    ap.add_argument("--merge-into", default="",
                    help="把本次结果合并进已有的明细 json（定点复跑后补回完整基线）")
    ap.add_argument("--online", action="store_true", help="强制用真实模型（需配 key）")
    ap.add_argument("--replay", action="store_true",
                    help="强制回放（不调模型、零成本）。配了 key 的机器上，"
                         "只有它能让评测真的跑离线")
    ap.add_argument("--out", default="", help="markdown 报告输出路径")
    ap.add_argument("--qfile", default="eval/questions.jsonl",
                    help="题库路径（相对项目根）。留出集用 eval/holdout.jsonl")
    args = ap.parse_args()

    qfile = ROOT / args.qfile
    if not qfile.exists():
        print(f"[error] 找不到题库：{qfile}")
        return 2
    questions = [json.loads(line) for line in qfile.open(encoding="utf-8") if line.strip()]
    all_questions = list(questions)     # 过滤前的完整题库，定点复跑合并后要用
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        missing = want - {q["id"] for q in questions}
        if missing:
            print(f"[warn] 题目里没有这些 id：{sorted(missing)}")
        questions = [q for q in questions if q["id"] in want]
    if args.limit:
        questions = questions[:args.limit]

    if args.online and args.replay:
        print("[error] --online 与 --replay 互斥")
        return 2

    index = LawIndex(CORPUS_PATH)
    store = MemoryStore(ROOT / "data" / "eval.db")
    llm = LLM()
    if args.replay:
        # 只把 online 置 False 不够：run() 开头的 maybe_recover() 会按冷却期把它拉回
        # 在线，于是「离线基线」里混进模型答案，而报告仍按回放记分。这是我踩过的坑
        # （report_replay_v34_main.md 实际是在线）。LLM_RECOVER_AFTER <= 0 是
        # maybe_recover 自己的语义：小于等于 0 就永不恢复，用它最干净。
        llm.online = False
        import app.llm as _llm_mod
        _llm_mod.LLM_RECOVER_AFTER = 0
        if LLM_ENABLED:
            print("[info] --replay：已配 key，但强制离线，不产生任何 API 费用")
    if args.online and not llm.online:
        print("[warn] 传了 --online 但没有可用 key，仍以回放模式运行")
    runner = AgentRunner(index, llm, store)

    run_id = time.strftime("%H%M%S")
    results = []
    t_start = time.time()

    print(f"\n跑评测：{len(questions)} 条 | 起始模式：{'在线' if llm.online else '回放'}"
          f"（这只是配置看上去的状态，跑到一半可能降级，以逐条标签为准）"
          f" | 语料：{index.stats()['articles']} 条\n")
    print(f"{'ID':<5}{'类型':<6}{'召回':<7}{'溯源':<7}{'覆盖':<7}{'状态':<9}{'耗时':<7}失败点")
    print("-" * 92)

    for q in questions:
        t0 = time.time()
        # 逐条记模式：`llm.online` 会在一轮中途翻成 False（在线调用失败即降级），
        # 于是同一次评测里前半段是模型答的、后半段是模板答的。只报一个总模式，
        # 等于把两种来源的分数混成一个数——那种数最容易被当成「模型退化了」。
        was_online = start_mode(llm)
        st = runner.run(q["question"],
                        session_id=f"ev{run_id}_{q['id']}",
                        emit=lambda e, d: None)
        ms = int((time.time() - t0) * 1000)
        # 跑完再看一次：开头在线、结束时掉线，说明这一条是**跑到一半降级的**。
        # 它的答案是前半模型写、后半模板拼，两种来源混在同一段文字里没法拆，
        # 标成 online 会让人以为可信，标成 replay 又冤枉了前半段。单列一类。
        mode = mode_of(was_online, llm.online)

        got = {e.article_id for e in st.evidences}
        gold = [g for g in q["gold"] if g in index.by_id]
        # 等义条文：同一件事的并行规定，命中任一即算找到（见 eval/equiv.py）
        recall = len(expand_gold(gold, set(got))) / len(gold) if gold else None

        cites = extract_cites(st.draft, index=index)
        # 同一部法可能被写成全称、简称、裸名三种，抽出来是三条不同的字符串，
        # 但解析到的条文 id 相同 —— 去重必须按 id，不能按名字。
        cites = _dedup_by_id(cites, index)
        # 判断「答的是不是标准答案那一条」必须走条文 id，不能比法律名字符串。
        # 原因：gold 用的是 law_short（如「劳动争议解释一」），而答案写全称
        # 「最高人民法院关于审理劳动争议案件适用法律问题的解释（一）」。
        # 短名砍掉了中间的「案件适用法律问题的」几个字，任何子串判定都会失败，
        # 于是 h11 明明引中了 gold 的两条，判等仍然失败。
        #
        # 注意：precision 只作**观测值**落盘，不再进汇总表打分。
        # 撤销理由写在汇总段的「为什么撤掉依据聚焦度」，一句话概括：
        # 它的分母是「模型引了几条」，而 gold 只标核心那 1–2 条，
        # 于是它量的是「引得多不多」，不是「引得准不准」。
        gold_ids = set(gold)
        correct = sum(
            1 for law, no in cites
            if (a := _find_article(index, law, no)) is not None and a["id"] in gold_ids)
        precision = correct / len(cites) if (cites and gold_ids) else None

        # 把「引用」拆成三类，比单一的「精确率」有用得多：
        #   phantom    条号在库里根本不存在  → 真幻觉，必须为 0
        #   unverified 条文存在但没进本次证据 → 生成后补核验要消灭这类
        #   extra      存在且在证据里、但不在 gold → 只是依据发散，不算错
        # 早先把三类混成一个「精确率」，还把说明写成「是不是真在证据里」，
        # 与实现（对比 gold）不符，导致「引用错误」被误读成大批幻觉。
        ev_ids = {e.article_id for e in st.evidences}
        phantom = 0
        unverified = 0
        cite_rows = []          # [法律名, 条号, 库内 id 或 None]，落盘供离线重算
        for law, no in cites:
            art = _find_article(index, law, no)
            cite_rows.append([law, no, art["id"] if art else None])
            if art is None:
                phantom += 1
            elif art["id"] not in ev_ids:
                unverified += 1

        kw = q["keywords"]
        kw_cov = (sum(1 for k in kw if kw_hit(st.draft, k)) / len(kw)) if kw else None

        # 可溯源率：引用的条文里，有多少条能在本次证据集合里找到出处。
        # 与 precision 的区别是它**不需要标准答案**——只看「这句引用有没有出处」，
        # 是纯粹的事实判定，没有任何阈值。未核验的引用是生成后补核验该消灭的对象。
        traceable = (1.0 - unverified / len(cites)) if cites else None

        refused = st.status == "refused"
        problems = []
        if gold and (recall is None or recall < 1):
            miss = [g for g in gold if g not in got]
            problems.append("漏召 " + ",".join(miss))
        # 只报幻觉，不再报「引用发散」：多引几条合法条文不是缺陷。
        # 详见汇总段「为什么撤掉依据聚焦度」。
        if phantom:
            problems.append(f"幻觉引用 {phantom}/{len(cites)}")
        if q.get("must_refuse") and not refused:
            problems.append("该拒没拒")
        if not q.get("must_refuse") and refused:
            problems.append("误拒")
        if kw_cov is not None and kw_cov < 1 and not refused:
            miss_kw = [k for k in kw if not kw_hit(st.draft, k)]
            problems.append("缺要点 " + ",".join(miss_kw))

        results.append({"id": q["id"], "type": q["type"], "question": q["question"],
                        "recall": recall, "precision": precision, "kw_cov": kw_cov,
                        "traceable": traceable,
                        "n_cites": len(cites), "phantom": phantom,
                        "unverified": unverified,
                        # 落盘这两个字段，改标签后才能离线重算而不必再烧一轮 API：
                        #   ev_ids  本次证据集合 → 可重算 recall / unverified
                        #   cites   引用明细（含库内 id）→ 可重算 precision / phantom
                        "ev_ids": sorted(ev_ids),
                        "cites": cite_rows,
                        "gold": q["gold"], "keywords": q["keywords"],
                        "must_refuse": bool(q.get("must_refuse")),
                        "refused": refused, "ms": ms, "status": st.status,
                        "mode": mode,
                        "zero_hit": len(st.evidences) == 0,
                        "steps": len(st.scratchpad), "problems": problems,
                        "answer": st.draft})

        print(f"{q['id']:<5}{TYPE_LABEL[q['type']]:<6}"
              f"{pct(recall):<7}{pct(traceable):<7}{pct(kw_cov):<7}"
              f"{('拒答' if refused else '作答') + MODE_TAG[mode]:<9}"
              f"{ms:<7}" + "；".join(problems))

    n_new = len(results)
    # 定点复跑：先把这几条并回完整基线，**再**出报告。
    # 合并放在报告之后的话，报告就只统计本次跑的几条（题数：1），
    # 而它的 json 是完整的——两份东西对不上，读报告的人会被带偏。
    base_p = ROOT / args.merge_into if args.merge_into else None
    if base_p:
        base = json.loads(base_p.read_text(encoding="utf-8"))
        results = merge_results(base, results)
        # 题库也要跟着换回完整的：汇总里 `next(q for q in questions ...)`
        # 是按 id 找题的，questions 还停在 --ids 过滤后的那几条的话，
        # 遇到没重跑的题直接 StopIteration 崩掉（真机复跑才撞得到）。
        questions = all_questions

    n_online, n_partly, n_replay = mode_summary(results)
    # 输出路径提前定下来：报告里的「重跑这几条」要给出能直接粘的命令，
    # 命令里得带上本轮明细 json 的路径（--merge-into 的基线），晚了就拼不出来。
    out = Path(args.out) if args.out else ROOT / "eval" / f"report_{time.strftime('%Y%m%d_%H%M')}.md"
    detail = out.with_suffix(".json")
    partly_ids = [r["id"] for r in results if r.get("mode") == "partly"]

    if n_replay:
        # 「回放」有两种来路，说法不能混：--replay 是我们主动关的，
        # 其余是被在线调用失败逼降的。混着说会让人以为机器出过故障。
        why = ("本次是 --replay 强制离线" if args.replay
               else "在线调用失败后自动降级")
        print(f"\n注意：{n_replay}/{len(results)} 条是**回放**（{why}），"
              f"标了「{MODE_TAG['replay']}」的行，分数不代表模型能力。")
    if n_partly:
        print(f"更要注意：{n_partly} 条标了「{MODE_TAG['partly']}」——开头在线、跑到一半降级，"
              f"答案是模型写的和模板拼的混在一起，无法拆分，这两头的分数都不能用。")

    # ---------------- 汇总 ----------------
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    factual = [r for r in results if not any(
        q["id"] == r["id"] and q.get("must_refuse") for q in questions)]
    refuse_set = [r for r in results if r["refused"] or any(
        q["id"] == r["id"] and q.get("must_refuse") for q in questions)]
    must_refuse = [r for r in results
                   if next(q for q in questions if q["id"] == r["id"]).get("must_refuse")]
    should_answer = [r for r in results
                     if not next(q for q in questions if q["id"] == r["id"]).get("must_refuse")]

    m_recall = avg([r["recall"] for r in should_answer])
    m_kw = avg([r["kw_cov"] for r in should_answer])
    m_refuse = avg([1.0 if r["refused"] else 0.0 for r in must_refuse])
    m_false = avg([1.0 if r["refused"] else 0.0 for r in should_answer])
    # 引用质量：把「幻觉」与「未核验」拆开统计
    n_cites = sum(r["n_cites"] for r in should_answer)
    n_phantom = sum(r["phantom"] for r in should_answer)
    n_unverified = sum(r["unverified"] for r in should_answer)
    m_legal = 1.0 - n_phantom / n_cites if n_cites else None
    # 可溯源率按引用加权，不按题平均：一道题引 8 条其中 3 条无出处，
    # 与另一道题引 1 条其中 0 条无出处，权重要体现在引用数上。
    m_trace = (1.0 - n_unverified / n_cites) if n_cites else None
    # 引用密度只作观测，不打分：它描述行为，不描述好坏。
    dense = n_cites / len(should_answer) if should_answer else None
    # 只有"检索零命中"这一类的拒答，在回放模式下才是可信的：
    # 它不依赖语义判断，只是"没东西可说就不说"。其余无答案题要靠模型
    # 判断"检索到的条文到底答不答得上问题"，那是语义问题，规则模拟不出来。
    zh = [r for r in must_refuse if r["zero_hit"]]
    m_zero = avg([1.0 if r["refused"] else 0.0 for r in zh])
    total_ms = int((time.time() - t_start) * 1000)

    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("汇总")
    lines.append("=" * 60)
    rows = [
        ("证据召回率", m_recall, f"n={len(should_answer)}，该找到的条文找到没有"),
        ("可溯源率", m_trace,
         f"共 {n_cites} 处引用，其中 {n_unverified} 处没有出处（应为 100%）"),
        ("引用合法率", m_legal,
         f"共 {n_cites} 处引用，其中 {n_phantom} 处条号库里不存在（应为 100%）"),
        ("要点覆盖率", m_kw, f"n={len(should_answer)}，关键结论说没说全"),
        ("零命中拒答率", m_zero, f"n={len(zh)}，检索完全无结果时是否正确拒答（可信）"),
        ("误拒率", m_false, f"n={len(should_answer)}，不该拒的有没有被拒（越低越好）"),
    ]
    for name, val, note in rows:
        lines.append(f"{name:<12}{pct(val):>8}  {bar(val)}  {note}")
    lines.append("")
    if n_unverified:
        lines.append(f"未核验引用 {n_unverified} 处：条文真实存在、但没进本次证据"
                     f"（生成后补核验本应清零，残留说明没补上）")
    else:
        lines.append("未核验引用 0 处：答案引用的每一条都在证据集合内，可逐句溯源")
    if dense is not None:
        lines.append(f"引用密度（观测值，不打分）平均每题 {dense:.1f} 条")
    if n_replay == 0 and n_partly == 0:
        lines.append(f"拒答准确率（全量 {len(must_refuse)} 条）{pct(m_refuse)}"
                     f"  ← 全部在线，可信")
    else:
        lines.append(f"拒答准确率（全量 {len(must_refuse)} 条）{pct(m_refuse)}"
                     f"  ← 含 {n_replay} 条回放 + {n_partly} 条半程，"
                     f"本项不可信，见下方说明")
    lines.append(f"\n总耗时 {total_ms / 1000:.1f}s | 平均 {total_ms // max(len(results), 1)}ms/条")

    # ---------------- 失败明细 ----------------
    fails = [r for r in results if r["problems"]]
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"失败明细（{len(fails)} 条）")
    lines.append("=" * 60)
    n_dirty_fail = sum(1 for r in fails if r.get("mode") != "online")
    if n_dirty_fail:
        lines.append(f"其中 {n_dirty_fail} 条不是在线跑的，行尾标了来源："
                     f"这些「失败」是模板或半程答案的问题，"
                     f"照着它们去调模型等于查错方向。")
    for r in fails:
        tag = MODE_TAG.get(r.get("mode"), MODE_UNKNOWN_TAG)
        lines.append(f"[{TYPE_LABEL[r['type']]}] {r['id']}{tag}  {r['question']}")
        lines.append(f"    {'；'.join(r['problems'])}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("指标口径说明")
    lines.append("=" * 60)
    lines.append(
        "【为什么撤掉「依据聚焦度」】\n"
        "  它原先算的是「答案引用的条文里，有多少落在标准答案内」。问题在分母：\n"
        "  分母是「模型引了几条」，而一道题的标准答案通常只标 1–2 条核心条文。\n"
        "  法律答案本来就该沿条文线索扩展（劳动合同法四十条 → 实施条例二十条 →\n"
        "  四十八、八十七条），这些扩展条文全都会被算成「不聚焦」。\n\n"
        "  实测：h03 问「没提前三十天通知要多给一个月工资吗」，标准答案只标四十条，\n"
        "  模型引了九条——代通知金的计算标准、违法解除的两倍赔偿金、赔偿金与经济\n"
        "  补偿不并用、程序违法如何翻转定性，逐条核过没有一条是凑数。聚焦度判它 11%。\n\n"
        "  也试过两条改良路线，都不成立：\n"
        "    1. 用「条文原文与答案的字面重合」判引用是否被实质使用 → 版本题全灭。\n"
        "       「合同法一百一十四条已废止，由民法典五百八十五条承继」这种答案本来\n"
        "       就不复述条文内容，重合度接近 0，但它恰恰是版本题唯一正确的答法。\n"
        "    2. 用「引用图连边 / 相邻条数」判扩展是否相关 → 阈值没有法律依据。\n"
        "       而且引用图只在同法内建边，实施条例、司法解释、民法典这些跨法依循\n"
        "       恰恰是最有价值的引用，一条都连不上。\n\n"
        "  结论：这个指标缺的不是精度，是**成立的前提**——它要求 gold 穷举所有应当\n"
        "  引用的条文，而那等于要标注人替模型想清楚每一条推理路径。撤掉，不修。\n\n"
        "【可溯源率】替代它。每处引用要么能在本次证据集合里找到出处，要么找不到，\n"
        "  纯事实判定、无阈值、与 gold 标得全不全无关。这项应为 100%；\n"
        "  低于 100% 只有两种解释：生成后补核验没兜住，或模型引了没检索过的条文。\n\n"
        "【引用合法率】衡量「有没有编」。引用的条号在语料库里到底存不存在，应为 100%。\n"
        "  低于 100% 才是真幻觉，必须逐条查。\n\n"
        "【证据召回率】只看标准答案里的条文有没有被检索到，与答案写得好不好无关。\n"
        "【要点覆盖率】按每题 keywords 命中统计，检查关键结论（如「二倍」「30%」）是否说全。\n"
        "【引用密度】平均每题引几条，只作行为观测。它不评价好坏：引得多可能是答得全，\n"
        "  也可能是证据池大。判据是让它稳定在一个量级，突然翻倍才需要查。\n")

    # 不能用 `llm.online` 判断整份报告的模式——它只是**最后一条跑完**时的状态。
    # 跑到一半降级的评测，末尾 online=False，可前面几十条是真的模型答的，
    # 整份被说成「回放模式」，那些分数就白跑了。按逐条计数说话。
    if n_replay or n_partly:
        lines.append("")
        lines.append(
            f"注意：本轮 {len(results)} 条里，在线 {n_online} / 半程 {n_partly} / "
            f"回放 {n_replay}。\n"
            f"回放条分数由规则模拟产生，反映的是**检索与流程**的健康度，不是模型能力；\n"
            f"其中「拒答准确率（全量）」本质不可信——判断「检索到的条文答不答得上问题」\n"
            f"是语义问题，规则模拟不出来。\n"
            f"半程条更麻烦：开头在线、跑到一半降级，答案前半是模型、后半是模板，\n"
            f"混在同一段文字里拆不开，这 {n_partly} 条不进任何结论。\n"
            f"配好 key 后加 --online 重跑，才是在测模型。\n")
        if partly_ids:
            lines.append(
                f"半程条 id：{','.join(partly_ids)}\n"
                f"排除故障后只重跑这几条、并回本轮明细即可（不必整轮重烧 API）：\n"
                f"  {rerun_cmd(partly_ids, detail, out)}\n"
                f"前提要说清：故障没排除就重跑，只会再得到一批回放答案；"
                f"冷却（LLM_RECOVER_AFTER={LLM_RECOVER_AFTER}s）没过的话，"
                f"连重试都不会发生。\n")

    report = "\n".join(lines)
    print(report)
    if n_replay == 0 and n_partly == 0:
        mode_txt = "**在线模型**"
    elif n_online == 0:
        mode_txt = "**回放模式（未配置 API key 或调用失败）**"
    else:
        mode_txt = ("**混合**（在线 %d / 半程 %d / 回放 %d 条，中途降级）"
                    % (n_online, n_partly, n_replay))
    header = (f"# 评测报告\n\n生成时间：{time.strftime('%Y-%m-%d %H:%M')}  \n"
              f"模式：{mode_txt}  \n"
              f"题数：{len(results)}（单跳 {sum(1 for r in results if r['type'] == 'single')} / "
              f"多跳 {sum(1 for r in results if r['type'] == 'multi')} / "
              f"版本 {sum(1 for r in results if r['type'] == 'version')} / "
              f"无答案 {sum(1 for r in results if r['type'] == 'refuse')}）  \n"
              f"语料：{index.stats()['articles']} 条条文\n\n")
    if base_p:
        header += (f"> 本次只重跑了 {n_new} 条，已并入基线；"
                   f"下面的数字按完整 {len(results)} 条算。\n")
    if n_replay or n_partly:
        header += (f"> 本轮有 {n_replay} 条回放、{n_partly} 条半程，这些数字反映的是"
                   f"**检索与流程**的健康度，不是模型能力。\n"
                   f"> 配好 key 后加 `--online` 重跑，与本报告对比，"
                   f"才能看出模型带来的增量。\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    # 不要再套 ``` 围栏：header 里没有开，单写一个闭合会破坏 markdown 渲染
    out.write_text(header + "\n" + report + "\n", encoding="utf-8")

    # 明细落盘：以后想换口径重算，直接读这个 json，不必再花一轮 API 钱重跑
    if base_p:
        # 必须写回 base_p：早先只写了 detail（--out 的同名 json），
        # 结果合并后的完整明细落在一个临时文件里，基线 json 一个字没动，
        # 打印却说「已合并」——这种静默失败最难查。
        text = json.dumps(results, ensure_ascii=False, indent=2)
        base_p.write_text(text, encoding="utf-8")
        detail.write_text(text, encoding="utf-8")
        print(f"\n本次重跑 {n_new} 条，已并入 {base_p}"
              f"（完整 {len(results)} 条，报告按完整集合出）")
    else:
        detail.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"\n报告已写入 {out}")
    print(f"明细已写入 {detail}")
    if partly_ids:
        print(f"\n其中 {len(partly_ids)} 条跑到一半降级（{','.join(partly_ids)}），"
              f"分数不能用。故障排除后补跑这几条：")
        print(f"  {rerun_cmd(partly_ids, detail, out)}")
        print("  故障没排除就跑，只会再得一批回放答案；冷却没过连重试都不会发生。")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
