# -*- coding: utf-8 -*-
"""
四套官方题集全离线回放，把「86 题全 100%」钉进一键回归。

为什么要有它：
  「四套主集 --replay 仍全 100%」是 README 里被引用最多的一句话（几乎每版都写），
  但它从来没进过 run_all —— 一直靠人手敲四条命令、再用眼睛看四张表。
  人手执行的检查迟早会退化成「上次跑过没问题」，而这句话恰恰是整份文档里
  最该被机器守着的一句。
  回放是零成本的（--replay 强制离线，一行模型都不调），四套合计 8 秒左右，
  放进回归的代价远比漏测低。

口径（与 eval/run_eval.py 的报告保持一致）：
    证据召回 / 要点覆盖 / 可溯源   应为 100%
    误拒                          应为 0%
  这几个数直接从报告的 markdown 里抠出来——不是为了图省事，而是因为它们
  就是 README 引用的那几个字面值；另算一套等于给「文档说的」和「脚本算的」
  留出分歧空间。明细 json 只在失分时用来点名是哪几道题。

跑法：
    ./.venv/Scripts/python.exe scripts/check_eval.py
    ./.venv/Scripts/python.exe scripts/check_eval.py --quiet   # 只报结论
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "_reports"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

# (简称, 题库, 期望题数)
SETS = [
    ("主集", "eval/questions.jsonl", 50),
    ("留出", "eval/holdout.jsonl", 14),
    ("口语", "eval/questions_colloquial.jsonl", 14),
    ("口语留出", "eval/questions_colloquial_holdout.jsonl", 8),
]

# 指标名 -> (正则, 期望值)。正则宽松一些：数字和百分号之间可能有空格。
METRICS = [
    ("召回", r"证据召回率\s+([\d.]+)\s*%", 100.0),
    ("要点", r"要点覆盖率\s+([\d.]+)\s*%", 100.0),
    ("可溯源", r"可溯源率\s+([\d.]+)\s*%", 100.0),
    ("误拒", r"误拒率\s+([\d.]+)\s*%", 0.0),
]

QUIET = "--quiet" in sys.argv[1:]
SELF_TEST = "--self-test" in sys.argv[1:]


def wpad(s: str, width: int) -> str:
    """按显示宽度补空格。中文占两格，`f"{x:<10}"` 是按字符数补的，
    所以「主集」和「口语留出」会被排成一样宽、表格看着是歪的。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


def got_metrics(md: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, pat, _ in METRICS:
        m = re.search(pat, md)
        if m:
            out[name] = float(m.group(1))
    return out


def bad_ids(rows: list[dict]) -> list[str]:
    """哪几道题拖了后腿。null / 缺失一律按「不达标」算，不放行。

    无答案题（must_refuse）整条跳过：它没有 gold，recall / kw_cov 天然是 None，
    拿它去比会把每一道「该拒没拒」都报成「召回 None」——而「该拒没拒」属于
    拒答准确率，报告里已经写明离线回放判不了这个（规则模拟不出语义），
    所以它也不在本门禁的口径内。报一条不算失分的失分题，比不报更误导。
    """
    bad = []
    for r in rows:
        if r.get("must_refuse"):
            continue
        if r.get("refused"):
            bad.append(f"{r.get('id')}（误拒）")
            continue
        short = []
        if (r.get("recall") or 0) < 1:
            short.append(f"召回{r.get('recall')}")
        if (r.get("kw_cov") or 0) < 1:
            short.append(f"要点{r.get('kw_cov')}")
        if (r.get("traceable") or 0) < 1:
            short.append(f"溯源{r.get('traceable')}")
        if short:
            bad.append(f"{r.get('id')}（{'/'.join(short)}）")
    return bad


def self_test() -> int:
    """检查这个检查脚本自己会不会「假装通过」。

    门禁脚本最怕的不是漏测，而是**明明不达标却退出 0**——那种情况下
    回归表全绿，人会以为守住了。这里用构造数据把三条判定都试一遍。
    """
    okc = 0
    bad = 0

    def ok(cond: bool, what: str) -> None:
        nonlocal okc, bad
        if cond:
            okc += 1
        else:
            bad += 1
            print(f"  [FAIL] {what}")

    md_good = ("题数：14\n证据召回率         100.0%  ██  n=12\n"
               "可溯源率          100.0%  ██  共 28 处引用\n"
               "要点覆盖率         100.0%  ██  n=12\n"
               "误拒率             0.0%  ··  n=12\n")
    v = got_metrics(md_good)
    ok(len(v) == 4 and v["召回"] == 100.0 and v["误拒"] == 0.0, "四个指标都抠得出来")

    # 数字和百分号之间多个空格也得认（排版改一次不该让门禁失明）
    ok(got_metrics("证据召回率   95.8 %")["召回"] == 95.8, "容忍空格变体")
    # 少一项必须表现为「缺项」，而不是静悄悄当成 0 分或满分
    ok("要点" not in got_metrics("题数：1\n证据召回率 100.0%\n"), "缺项能被发现")

    rows = [
        {"id": "a", "recall": 1.0, "kw_cov": 1.0, "traceable": 1.0},
        {"id": "b", "recall": 0.5, "kw_cov": 1.0, "traceable": 1.0},
        {"id": "c", "recall": 1.0, "kw_cov": 0.0, "traceable": 1.0},
        {"id": "d", "recall": 1.0, "kw_cov": 1.0, "traceable": 0.0},
        # 该拒的拒了 = 正常
        {"id": "e", "must_refuse": True, "refused": True, "recall": None},
        # 该拒的没拒 = 漏拒，不在本门禁口径内，不该被报成「召回 None」
        {"id": "f", "must_refuse": True, "refused": False, "recall": None,
         "kw_cov": None},
        # 不该拒的拒了 = 误拒，必须报
        {"id": "g", "must_refuse": False, "refused": True, "recall": 1.0,
         "kw_cov": 1.0, "traceable": 1.0},
    ]
    ids = bad_ids(rows)
    ok("b" in " ".join(ids) and "c" in " ".join(ids) and "d" in " ".join(ids),
       "召回/要点/溯源 三项失分都能点名")
    ok(any(i.startswith("g") and "误拒" in i for i in ids), "误拒能被点名")
    ok(not any(i.startswith("e") for i in ids), "该拒的拒了不误报")
    ok(not any(i.startswith("f") for i in ids), "漏拒不报成召回缺失")
    # None 不能当成「通过」
    ok(bad_ids([{"id": "h", "recall": None, "kw_cov": None, "traceable": None}]),
       "null 指标按不达标处理")

    print(f"  自检 {okc} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


def main() -> int:
    if SELF_TEST:
        return self_test()
    REPORTS.mkdir(parents=True, exist_ok=True)
    print("=" * 74)
    print("  四套官方题集回放（全离线，零成本）")
    print("=" * 74)
    print("  " + wpad("题集", 12) + wpad("题数", 6) + wpad("召回", 9)
          + wpad("要点", 9) + wpad("可溯源", 9) + wpad("误拒", 8) + "结果")
    print("  " + "-" * 70)

    fails: list[str] = []
    total_q = 0
    for label, qfile, want_n in SETS:
        stem = Path(qfile).stem
        md_path = REPORTS / f"_check_{stem}.md"
        cmd = [str(PY), str(ROOT / "eval" / "run_eval.py"),
               "--qfile", qfile, "--replay", "--out", str(md_path)]
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            print(f"  {label:<10} 超时（300s）")
            fails.append(f"{label}：超时")
            continue
        if r.returncode != 0 or not md_path.exists():
            tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            print(f"  {label:<10} 评测脚本失败：{(tail[-1] if tail else r.returncode)}")
            fails.append(f"{label}：脚本退出码 {r.returncode}")
            continue

        md = md_path.read_text(encoding="utf-8")
        m = re.search(r"题数：(\d+)", md)
        n = int(m.group(1)) if m else 0
        total_q += n
        vals = got_metrics(md)

        cells, ok_all = [], n == want_n
        if n != want_n:
            fails.append(f"{label}：题数 {n}，应为 {want_n}（题库被改过？）")
        for name, _, want in METRICS:
            if name not in vals:
                cells.append(wpad("??", 9))
                fails.append(f"{label}：报告里找不到「{name}」这一项（格式变了？）")
                ok_all = False
                continue
            v = vals[name]
            cells.append(wpad(f"{v:.1f}%", 9))
            # 小数点误差不留余地：这几个指标本来就该是整 100 / 整 0
            if abs(v - want) > 0.05:
                ok_all = False
                fails.append(f"{label}：{name} {v}%，应为 {want}%")

        if not ok_all:
            js = REPORTS / f"_check_{stem}.json"
            if js.exists():
                try:
                    rows = json.loads(js.read_text(encoding="utf-8"))
                    ids = bad_ids(rows if isinstance(rows, list) else [])
                    if ids:
                        fails.append(f"{label}：失分题 {'、'.join(ids[:8])}")
                except Exception:      # noqa: BLE001
                    pass

        mark = "通过" if ok_all else "**不达标**"
        print("  " + wpad(label, 12) + wpad(str(n), 6) + "".join(cells)
              + "  " + mark)
        if not QUIET and not ok_all:
            # 报告可能被指到项目外（--out 或测试里改 REPORTS），那时
            # relative_to 会抛 ValueError，把一条「不达标」的报告变成
            # 一条崩溃栈——最要紧的那行字反而没人看见。
            try:
                shown = md_path.relative_to(ROOT)
            except ValueError:
                shown = md_path
            print("  明细见 " + str(shown))

    print("  " + "-" * 70)
    print(f"  合计 {total_q} 题 / {len(SETS)} 套")

    if fails:
        print("\n失败：")
        for f in fails:
            print("  · " + f)
        return 1
    print("\n全部达标：召回 / 要点 / 可溯源 100%，误拒 0%。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
