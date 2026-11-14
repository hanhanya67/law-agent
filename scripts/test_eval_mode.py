# -*- coding: utf-8 -*-
"""
评测里「这一条答案是谁写的」的判定回归测试。纯函数，零 API、零服务。

钉的是一件很安静的事：`llm.online` 会在一轮问答**中途**翻成 False（在线调用
失败即降级）。于是同一份评测里，前面的题是模型答的、后面的题是模板答的，
还有一条最脏的——开头在线、跑到一半掉线，答案前半是模型、后半是模板。

早先只在开跑前记一次状态，于是那条「半程」的题被记成 online：模板拼出来的
100% 会被当成模型成绩。这个错没有任何报错，只有看报告的人会信错。

用法：
  ./.venv/Scripts/python.exe scripts/test_eval_mode.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))          # run_eval 里的 `from equiv import ...`

import run_eval                                  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))


def test_mode_of() -> None:
    print("\n[1] 逐条来源判定")
    cases = [
        # (开跑前, 跑完后, 期望, 场景)
        (True, True, "online", "全程在线"),
        (False, False, "replay", "全程回放（没配 key / 早就降级了）"),
        (True, False, "partly", "开头在线、跑到一半降级 ← 最脏的一种"),
        (False, True, "replay", "开跑前就离线（中途被冷却重试救回来也算起始离线）"),
    ]
    for before, after, want, scene in cases:
        got = run_eval.mode_of(before, after)
        ok(f"{scene} → {want}", got == want, f"得到 {got}")

    # 反向钉死：partly 绝不能被判成 online。这行要是挂了，
    # 模板答案就又开始冒充模型成绩了。
    ok("半程不会被误判成 online",
       run_eval.mode_of(True, False) != "online",
       run_eval.mode_of(True, False))


def test_tag_covers() -> None:
    print("\n[2] 标签字典要盖住所有取值")
    # mode_of 多返回一个新值而 MODE_TAG 没跟上，评测跑到一半 KeyError 崩掉，
    # 前面烧掉的 API 钱就白花了。这里把两种取值来源对齐。
    for m in ("online", "replay", "partly"):
        ok(f"MODE_TAG 有 {m}", m in run_eval.MODE_TAG,
           run_eval.MODE_TAG.get(m, "<缺失>"))
    ok("在线不加尾巴（否则每行都拖个标记，正常情况反而像异常）",
       run_eval.MODE_TAG["online"] == "", repr(run_eval.MODE_TAG["online"]))
    ok("回放与半程的标记必须不同（看报告的人靠这个区分）",
       run_eval.MODE_TAG["replay"] != run_eval.MODE_TAG["partly"])
    # 合并旧基线时会遇到没有 mode 的条目，出报告时不能 KeyError
    ok("来源不明有兜底标签（合并旧明细时不崩）",
       run_eval.MODE_UNKNOWN_TAG != "", run_eval.MODE_UNKNOWN_TAG)
    ok("兜底标签与三种已知标签都不同（否则看不出是来源不明）",
       run_eval.MODE_UNKNOWN_TAG not in run_eval.MODE_TAG.values(),
       run_eval.MODE_UNKNOWN_TAG)


def test_summary() -> None:
    print("\n[3] 整场计数：中途降级不能被说成整份回放")
    # 真实形状：前 8 条在线、第 9 条跑到一半降级、后面 41 条全回放
    rows = ([{"mode": "online"}] * 8
            + [{"mode": "partly"}]
            + [{"mode": "replay"}] * 41)
    n_online, n_partly, n_replay = run_eval.mode_summary(rows)
    ok("在线条数保住了（不是 0）", n_online == 8, n_online)
    ok("半程单独计一条", n_partly == 1, n_partly)
    ok("回放条数对得上", n_replay == 41, n_replay)
    ok("三类相加等于总条数", n_online + n_partly + n_replay == len(rows))

    ok("全在线时回放为 0（报告才敢说「全部在线，可信」）",
       run_eval.mode_summary([{"mode": "online"}] * 5) == (5, 0, 0))
    ok("全回放时在线为 0",
       run_eval.mode_summary([{"mode": "replay"}] * 5) == (0, 0, 5))
    ok("空结果不炸", run_eval.mode_summary([]) == (0, 0, 0))
    # mode 是后加的字段，早期落盘的明细 json 里没有。这类条来源不明，
    # 宁可少算一条，也不能被算进「在线」让报告宣称可信。
    ok("缺 mode 字段的老明细不当成在线",
       run_eval.mode_summary([{"id": "s01"}])[0] == 0,
       run_eval.mode_summary([{"id": "s01"}]))


def test_rerun_cmd() -> None:
    print("\n[4] 半程条的补跑命令")
    detail = Path("eval/report_20261030_1200.json")
    out = Path("eval/report_20261030_1200.md")
    cmd = run_eval.rerun_cmd(["s01", "h07"], detail, out)
    ok("命令含全部半程 id（漏一个就补不回来）",
       all(x in cmd for x in ("--ids s01,h07",)), cmd)
    ok("命令指回本轮明细（--merge-into），否则合并成另一份报告",
       "--merge-into" in cmd and str(detail) in cmd, cmd)
    ok("命令带 --online（没配 key 时会在开头警告，不至于又跑一轮安静的回放）",
       "--online" in cmd)
    ok("空列表返回空串（没有半程条就别打印空命令）",
       run_eval.rerun_cmd([], detail, out) == "",
       repr(run_eval.rerun_cmd([], detail, out)))


def test_start_mode() -> None:
    print("\n[5] 开跑那一刻的在线状态（不是上一条留下的）")
    # maybe_recover() 是 AgentRunner.run() 在**内部**开头调的（app/agent.py）。
    # 调用方在 run() 之前问 llm.online，拿到的是上一条跑完时的状态：
    # 上一条降级、冷却已过、这一条开头被救回在线 → 会被误记成 replay，
    # 可它全程是在线答的。start_mode() 必须先恢复再问。

    class _FakeLLM:
        def __init__(self, online: bool, recoverable: bool):
            self.online = online
            self.recoverable = recoverable
            self.calls = 0

        def maybe_recover(self) -> bool:
            self.calls += 1
            if self.online or not self.recoverable:
                return False
            self.online = True          # 冷却已过，试探成功
            return True

    llm = _FakeLLM(True, True)
    ok("已在线：返回 True", run_eval.start_mode(llm) is True)
    ok("已在线：确实调了 maybe_recover（不调就取不到真实起点）", llm.calls == 1)

    # 关键一条：上一条降级、冷却已过 → 这一条开头被救回，必须判 online
    llm = _FakeLLM(False, True)
    before = run_eval.start_mode(llm)
    got = run_eval.mode_of(before, llm.online)   # 跑完仍在线（这一条没再失败）
    ok("被冷却重试救回的那条判 online，不判 replay", got == "online", got)

    # 冷却没过：救不回来，整条回放
    llm = _FakeLLM(False, False)
    before = run_eval.start_mode(llm)
    ok("冷却没过时仍是离线", before is False)
    ok("救不回来的那条判 replay", run_eval.mode_of(before, llm.online) == "replay")

    # start_mode 依赖 LLM 上有 maybe_recover；哪天改名会崩在评测中途，
    # 前面烧掉的 API 钱就白花了。这里提前拦住。
    from app.llm import LLM               # noqa: E402
    ok("LLM 上确实有 maybe_recover（改名会让 start_mode 中途崩）",
       hasattr(LLM, "maybe_recover"))


def test_merge() -> None:
    print("\n[6] 定点复跑的合并（报告要按完整集合出）")
    base = [{"id": "s01", "recall": 1.0, "mode": "partly"},
            {"id": "s02", "recall": 0.5, "mode": "replay"},
            {"id": "s03", "recall": 1.0, "mode": "replay"}]
    new = [{"id": "s01", "recall": 1.0, "mode": "online"}]

    got = run_eval.merge_results(base, new)
    ok("重跑的条被覆盖（partly → online）",
       got[0]["mode"] == "online", got[0]["mode"])
    ok("没重跑的条原样保留",
       [r["id"] for r in got[1:]] == ["s02", "s03"], [r["id"] for r in got])
    ok("完整条数按 base 走（报告才不会写成「题数：1」）",
       len(got) == 3, len(got))
    ok("顺序按 base（报告行序稳定，diff 才读得出变化）",
       [r["id"] for r in got] == ["s01", "s02", "s03"])

    # base 里没有的新 id：早先那版 `[new.get(r["id"], r) for r in base]`
    # 会把它们静默丢掉——重跑一条基线里不存在的题，结果就没了。
    got2 = run_eval.merge_results(base, [{"id": "h99", "mode": "online"}])
    ok("base 里没有的新 id 不会被丢",
       any(r["id"] == "h99" for r in got2), [r["id"] for r in got2])

    ok("空 new 返回 base 全量（不缩水）",
       [r["id"] for r in run_eval.merge_results(base, [])] == ["s01", "s02", "s03"])
    ok("空 base 也能用（首轮就传 --merge-into 不会白跑）",
       len(run_eval.merge_results([], new)) == 1)
    ok("不改动入参（base 是可复用的文件，被改脏下次就全错）",
       base[0]["mode"] == "partly", base[0]["mode"])


def test_recompute_shares_rule() -> None:
    print("\n[7] 离线重算与评测共用同一份口径")
    # recompute.py 是从 run_eval.py 导入 mode_summary 的。哪天有人图省事
    # 复制一份到 recompute 里改两笔，两份报告的「在线 N 条」就会对不上，
    # 而两边都是 markdown，diff 起来没人会注意到数字口径不一样。
    rc = importlib.import_module("recompute")
    ok("recompute 复用 run_eval 的 mode_summary（判定只留一份）",
       rc.mode_summary is run_eval.mode_summary)
    # 旧明细没有 mode 字段 → 必须落成 unknown，不能被默认当成在线
    r = {"id": "s01", "answer": "", "cites": [], "ev_ids": [],
         "refused": False, "status": "ok"}
    q = {"id": "s01", "question": "测试", "gold": [], "keywords": [],
         "type": "single"}
    from app.retrieval import LawIndex           # noqa: E402
    from app.config import CORPUS_PATH           # noqa: E402
    new, _how = rc.recompute_one(r, q, LawIndex(CORPUS_PATH))
    ok("重算结果落了 mode 字段，缺字段时为 unknown",
       new.get("mode") == "unknown", new.get("mode"))


def test_ab_split() -> None:
    print("\n[8] 抽取器 A/B 的来源分组")
    # ab_backfill 比的是**抽取器**（旧正则 vs 新抽取器），混着来源照样能比——
    # 抽取器不关心文本谁写的。所以主结论不变，只把数字按来源拆开，
    # 让读的人自己判断「多出来的引用值不值钱」这种比例该信多少。
    ab = importlib.import_module("ab_backfill")
    ok("online 归 online", ab.split_key("online") == "online")
    for m in ("replay", "partly", "unknown", None, ""):
        ok(f"{m!r} 归 other（不与在线混算）", ab.split_key(m) == "other",
           ab.split_key(m))


def test_mode_note() -> None:
    print("\n[9] 打开一条答案看时，先说清它是谁写的")
    ok("在线不返回文案（正常情况不该有噪音）",
       run_eval.mode_note("online") == "", repr(run_eval.mode_note("online")))
    for m, must in (("replay", "模板"), ("partly", "降级"),
                    ("unknown", "mode"), (None, "mode")):
        note = run_eval.mode_note(m)
        ok(f"{m!r} 的说明非空且点明来源", bool(note) and "模型" in note, note)


def main() -> int:
    test_mode_of()
    test_tag_covers()
    test_summary()
    test_rerun_cmd()
    test_start_mode()
    test_merge()
    test_recompute_shares_rule()
    test_ab_split()
    test_mode_note()
    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
