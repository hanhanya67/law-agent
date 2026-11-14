# -*- coding: utf-8 -*-
"""快照漂移检查：`.snapshot_v*/` 里的副本，还是不是工作区现在的样子？

为什么需要它：
  `.snapshot_v82/` 是**交付快照**——决定答案质量与界面行为的源文件副本，
  不含数据与虚拟环境。它的价值完全建立在「和工作区一致」这一个前提上。
  而这个前提没有任何东西在守：改完代码顺手拷过去是人的动作，忘了没人知道。
  于是快照会安静地老下去，等到有人拿它恢复时，装回去的是一份错的代码，
  而且看起来一切正常——**这正是这个项目里反复出现的那类故障**。

  它和「语料缺失不报错」是同一个病：状态不一致，但不抛异常。
  区别只是这次不一致发生在文件之间，而不是文件与程序之间。

怎么判断：
  按**文件名**在仓库里找同名源文件（快照里是扁平目录，为了好拷回去）。
  找不到 → 说明快照里躺着一个已经不存在的文件的副本；
  找到多个 → 说不清是哪个，报出来让人自己看，不猜。

跑法：
    ./.venv/Scripts/python.exe scripts/check_snapshot.py
    ./.venv/Scripts/python.exe scripts/check_snapshot.py --self-test
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只排除「肯定不是源码、也不该进快照」的目录。data/ 与 eval/ 必须留着——
# 快照里可能带样例数据，排除掉会被误判成「源文件已删除」。
# _shot 例外：那是浏览器缓存与截图，里面甚至有 Edge 自己的钱包页 README。
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git",
             ".mypy_cache", ".pytest_cache", "_shot"}
# v132：.txt 必须在这里。交付说明.txt 是收货方唯一会逐字读的文档，把它列进
# MUST_SNAPSHOT 之后却被判成「仓库里已没有这个文件」——因为索引根本不收 .txt，
# cands 永远为空。**清单和索引是两处白名单，只改一处等于没改。**
SUFFIX = {".py", ".html", ".js", ".css", ".md", ".bat", ".cmd",
          ".json", ".jsonl", ".txt"}


def digest(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:12]


def latest_snapshot() -> Path | None:
    """只比**最新**那一个。

    这条是一开始写错的地方：我让脚本检查所有 `.snapshot_v*`，结果 v19 / v79 /
    v80 / v81 全红。可历史快照本来就该是旧的——拿它跟今天的代码比，等于问
    「v19 为什么没有今天的改动」，答案永远是没有。
    更糟的是后果：一个永远红的门禁，用两天就没人看了。
    """
    def ver(p: Path) -> int:
        try:
            return int(p.name.rsplit("_v", 1)[1])
        except (IndexError, ValueError):
            return -1
    snaps = [p for p in ROOT.glob(".snapshot_v*") if p.is_dir()]
    if not snaps:
        return None
    return max(snaps, key=ver)


def repo_index() -> dict[str, list[Path]]:
    """文件名 → 仓库里的全部同名文件（通常只有一个）。

    用 os.walk 而不是 rglob：rglob 会一路走完 .venv（几万个文件）才开始过滤，
    这个脚本要在回归里跑，不能为了查十几个文件去遍历一整个虚拟环境。

    注意 data/ 与 eval/ **不能排除**：快照里可能带样例数据（v81 就带了
    statutes.jsonl 和几份探针题集），排除掉它们会被误判成「源文件已删除」。
    排除目录只留那些「肯定不是源码也不该进快照」的。
    """
    import os
    idx: dict[str, list[Path]] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".snapshot")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix not in SUFFIX:
                continue
            idx.setdefault(fn, []).append(p)
    return idx


def snapshot_files(snap: Path) -> list[Path]:
    out = []
    for p in sorted(snap.iterdir()):
        if p.is_file() and p.name != "MANIFEST.txt":
            out.append(p)
    return out


# 清单里的文件名到实际路径的查找范围
SOURCE_DIRS = ("app", "scripts", "static")


# 反向检查（工作区有、快照没有）的**显式清单**。
#
# 为什么不能扫目录：快照是精选集，不是全量镜像。scripts/ 下几十个一次性脚本
# （_add_gsbx、ab_backfill、check_key…）本来就不该进快照，按目录反查会一次
# 刷出几十行「新增文件没进快照」——那种门禁第二天就没人看了。
#
# 代价是：新加关键文件时，要在这里补一行。忘补的表现是「不报」，与忘拷同一类
# 疏忽，所以清单本身也得靠人维护。但它至少把「哪些文件算关键」这件事写下来了。
MUST_SNAPSHOT = (
    # app/
    "admin.py", "agent.py", "auth.py", "config.py", "dbutil.py", "docgen.py",
    "llm.py", "memory.py", "prompts.py", "server.py", "vision.py", "_api.py",
    "extras.py", "mcp_server.py", "cases.py", "persona.py",
    "netprobe.py", "websearch.py", "profiles.py", "domains.py",
    # v124：案件级附件（程序表 + 五内核文书骨架）及其回归套件。
    "casekit.py", "test_casekit.py",
    # v136：这两份一直在改来改去，却从来没进清单——verdict.py 是答案的
    # 依据层（rules / advice 全在这里）、deadline.py 是时效计算的全部规则。
    # 改它们时快照一声不响，等真出问题才发现「门禁只守了一半」。
    "verdict.py", "deadline.py",
    # v137：成本与周期（三类证据分级）、用户画像（只记用户说过的事实）。
    # 这两份直接决定卡片上的数字，进清单比靠人记着更可靠。
    "costs.py", "facts.py",
    # v138：接入内核自己的槽位表（字段表 = slot_labels，追问顺序 = slot_priority）。
    # 漏掉这份 = 四个接入内核的追问顺序、追问说法没人守；labor 那套 CaseSlots
    # 在 agent.py 里，清单上只有这两份才盯得住接入内核。
    "dslots.py", "test_dslots.py",
    # v139：法典图书馆（只读语料、分库不串）与自主学习（出题/判答/进度）。
    # 三个新页面背后的规则全在这两份里，脱离清单就是「界面在、规则没人守」。
    "library.py", "study.py", "test_library.py", "test_study.py",
    # scripts/
    "check_bat.py", "check_corpus.py", "check_env.py", "check_eval.py",
    "check_pages.py", "check_snapshot.py", "check_ui.py", "pack_src.py",
    "run_all.py", "shot_pages.py", "test_intent.py", "test_profile_task.py",
    "test_doc.py", "test_calc.py", "test_vision.py", "_svc.py",
    "test_extras.py", "test_mcp.py", "build_cases.py",
    "test_cases.py", "test_persona.py", "test_network.py", "cases.jsonl",
    "probe_network_live.py", "test_profiles.py", "domain_statutes.jsonl",
    # v120：消费·食安语料的来源与合并脚本。语料本身在快照里，但「这 217 条
    # 是怎么来的、凭什么说是全文」全靠这两个脚本——只留语料不留来源，
    # 等于让人无法核验，也和 build_cases.py 的处理不一致。
    "fetch_consumer_food.py", "import_domain_laws.py",
    # 静态页与根目录
    "index.html", "login.html", "admin.html", "ui.js", "README.md",
    "启动-网页.bat", "mcp_config.json",
    # v132：交付说明是收货方唯一会逐字读的那份文档，和 README 同级重要。
    # 它一直没进清单，于是改了它快照也不会拦——这次手动把它拷进快照，
    # 反而被当成「快照里有、清单里没有」判成 GONE，才发现清单本身漏了它。
    "交付说明.txt",
    # 在线验收与循环去重这两套是 v132 新增的验证手段，改坏了要能被快照拦住。
    "check_live.py", "test_loop.py",
)


def source_files() -> dict[str, list[Path]]:
    """清单里的文件 → 工作区里的实际路径。找不到的记成空列表，交由调用方报。"""
    out: dict[str, list[Path]] = {}
    for name in MUST_SNAPSHOT:
        hits: list[Path] = []
        for sub in SOURCE_DIRS:
            base = ROOT / sub
            if base.is_dir():
                # 用 rglob 而不是拼固定路径：ui.js 在 static/assets/ 下面，
                # 拼 `static/ui.js` 会找不到，然后被误报成「清单该更新了」。
                hits.extend(p for p in base.rglob(name) if p.is_file())
        # 判例数据与接入内核语料在 data/ 下。不把 data 加进 SOURCE_DIRS：
        # data/raw/ 里有一份同名 README.md，rglob 会把它也算进来，
        # 「先取最浅一层」于是变成歧义，根目录那份反而被判成 dup。
        # v120：domain_statutes.jsonl 也是数据侧关键文件（4 个接入内核的
        # 条文全靠它），漏了它等于「快照里没有可交付的语料」却一路绿灯。
        if name in ("cases.jsonl", "domain_statutes.jsonl"):
            p = ROOT / "data" / name
            if p.is_file():
                hits.append(p)
        root_p = ROOT / name
        if root_p.is_file():
            hits.append(root_p)
        out[name] = hits
    return out


def compare(snap: Path, idx: dict[str, list[Path]]) -> list[tuple[str, str, str]]:
    """返回 [(文件名, 状态, 说明)]。状态取 same / diff / lost / dup。

    同名多处时**先取最浅的那一层**：`README.md` 在根目录、`data/raw/` 和
    `eval/_shot/...` 里都有，但快照里那份只可能是根目录的。深度相同才叫歧义
    ——那种情况是真分不清（比如 `app/x.py` 和 `scripts/x.py`），只能报出来。
    """
    rows = []
    for f in snapshot_files(snap):
        cands = idx.get(f.name) or []
        if not cands:
            rows.append((f.name, "lost", "仓库里已没有这个文件"))
            continue
        cands.sort(key=lambda c: len(c.relative_to(ROOT).parts))
        depth = len(cands[0].relative_to(ROOT).parts)
        top = [c for c in cands if len(c.relative_to(ROOT).parts) == depth]
        if len(top) > 1:
            rows.append((f.name, "dup",
                         "同名同层多处：" + "、".join(
                             str(c.relative_to(ROOT)) for c in top)))
        elif digest(f) != digest(top[0]):
            rows.append((f.name, "diff", str(top[0].relative_to(ROOT))))
        else:
            rows.append((f.name, "same", str(top[0].relative_to(ROOT))))

    # 反向：工作区新增的源码文件也得有人管。
    # 原来只顺着快照查，于是「新加的文件从来没进过快照」这件事一声不响——
    # v115 加的 app/vision.py 和 scripts/test_vision.py 就是这样漏掉的：
    # 拿快照恢复代码会直接少两个文件，而检查器一句都不说。
    have = {f.name for f in snapshot_files(snap)}
    for name, paths in sorted(source_files().items()):
        if not paths:
            rows.append((name, "stale",
                         "清单里有、工作区找不到 —— 该更新清单了"))
            continue
        if name in have:
            continue
        rel = [str(p.relative_to(ROOT)) for p in paths]
        rows.append((name, "dup" if len(paths) > 1 else "new",
                     "、".join(rel)))
    return rows


MARK = {"same": "ok  ", "diff": "DRIFT", "lost": "GONE ",
        "dup": "AMBIG", "new": "NEW  ", "stale": "STALE"}


def report(rows: list[tuple[str, str, str]]) -> int:
    bad = [r for r in rows if r[1] != "same"]
    for name, st, note in rows:
        if st != "same":
            print(f"  [{MARK[st]}] {name:24} → {note}")
    print(f"  {len(rows) - len(bad)} / {len(rows)} 与工作区一致")
    if bad:
        print()
        print("  快照落后于工作区。确认无误后重新拷一遍，例如：")
        for name, st, note in bad:
            if st == "diff":
                print(f"    cp {note} {snap_reldir()}/{name}")
            elif st == "new":
                print(f"    cp {note} {snap_reldir()}/")
        print("  （只拷自己刚改过的文件；lost 的那个要判断是删了还是改名了）")
    return 0 if not bad else 1


_SNAPDIR = [""]


def snap_reldir() -> str:
    return _SNAPDIR[0]


def self_test() -> int:
    """自检：真的能认出「不一样」吗？

    门禁脚本最怕的不是漏测，是明明不一致却退出 0——那种情况回归表全绿，
    人以为守住了，而快照正在慢慢变成另一份代码。
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="snap_self_"))
    a = tmp / "app"
    a.mkdir()
    (a / "x.py").write_text("v1\n", encoding="utf-8")
    snap = tmp / ".snapshot_v0"
    snap.mkdir()
    (snap / "x.py").write_text("v1\n", encoding="utf-8")
    (snap / "gone.py").write_text("bye\n", encoding="utf-8")

    global ROOT
    keep, ROOT = ROOT, tmp
    try:
        idx = repo_index()
        rows = dict((n, s) for n, s, _ in compare(snap, idx))
        fails = []
        if rows.get("x.py") != "same":
            fails.append("一致的文件被报成不一致")
        if rows.get("gone.py") != "lost":
            fails.append("消失的源文件没被认出来")
        (snap / "x.py").write_text("v2\n", encoding="utf-8")
        rows = dict((n, s) for n, s, _ in compare(snap, idx))
        if rows.get("x.py") != "diff":
            fails.append("内容不同没被认出来（这才是最要紧的一条）")
        # 同名两处要报歧义，不能随手挑一个
        b = tmp / "sub"
        b.mkdir()
        (b / "x.py").write_text("v3\n", encoding="utf-8")
        rows = dict((n, s) for n, s, _ in compare(snap, repo_index()))
        if rows.get("x.py") != "dup":
            fails.append("同名多处没报歧义")
    finally:
        ROOT = keep
        for f in sorted(tmp.rglob("*"), reverse=True):
            try:
                f.unlink() if f.is_file() else f.rmdir()
            except Exception:      # noqa: BLE001
                pass
    print(f"  自检 4 项：{'全过' if not fails else '失败'}")
    for f in fails:
        print("   - " + f)
    return 0 if not fails else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    # 每次体检前先自检。这类脚本失灵的样子不是报错，而是**全绿**：
    # 路径写错、指纹算成空串、快照目录没扫到——每一样都会让它安静地放过一切。
    # 30 毫秒的代价，换「它说没问题时真的没问题」。
    if self_test() != 0:
        return 1

    snap = latest_snapshot()
    if snap is None:
        print("没有找到 .snapshot_v* 目录——没有快照就不用查。")
        return 0
    _SNAPDIR[0] = snap.name
    others = sorted(p.name for p in ROOT.glob(".snapshot_v*")
                    if p.is_dir() and p != snap)
    print(f"{snap.name}（最新一份；{len(others)} 份历史快照不参与比较）：")
    return report(compare(snap, repo_index()))


if __name__ == "__main__":
    sys.exit(main())
