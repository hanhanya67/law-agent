# -*- coding: utf-8 -*-
"""启动脚本（bat）的门禁：编码、块内裸括号、端口预检、真能起服务。

为什么单独有这个套件：这个 bat 是用户**双击就用**的入口，而它此前
一条自动检查都没有。后果不是理论问题——v86 往第 5 步加了一段
full corpus gate，里面写了

    echo  2^) rebuild it from data\\raw\\ (offline):

括号在 if 块内**没转义**。cmd 解析到这一行直接崩，退出码 255，
服务一次都没起来过。而它前后所有 Python 套件仍然全绿——因为没人
会去执行一个 .bat。这个 bug 从写进去到被发现，唯一的检查手段是
「我自己双击试了一下」。

所以这里查四层：
  A 编码：纯 ASCII（cmd 按字节偏移解析，中文行会让偏移漂移）、
    必须有 CRLF、不能出现裸 LF
  B 引用：bat 里点名的每个文件都真的在包里
  C 语法：if/for 块内的 ( 和 ) 必须转义（本轮踩的那颗雷）
  D 行为：端口被占时必须拦住且不启动；端口空闲时必须真起来；
    缺正式语料时告警要出现，且只告警不拦启动
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "启动-网页.bat"
CLI = ROOT / "启动-命令行.bat"
SERVER = ROOT / "app" / "server.py"
SEED = ROOT / "data" / "seed_statutes.jsonl"

passed = 0
failed = 0
skipped: list[str] = []


def check(name: str, ok: bool, detail="") -> bool:
    global passed, failed
    mark = "[PASS]" if ok else "[FAIL]"
    print(f"  {mark} {name}" + (f"  → {detail}" if detail != "" else ""))
    if ok:
        passed += 1
    else:
        failed += 1
    return ok


# ---------------------------------------------------------------- A 编码
def check_encoding(path: Path) -> None:
    raw = path.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b >= 128][:3]
    check(f"{path.name} 是纯 ASCII", not non_ascii,
          f"首个非 ASCII 字节 {non_ascii}" if non_ascii else f"{len(raw)} 字节")
    lf, crlf = raw.count(b"\n"), raw.count(b"\r\n")
    check(f"{path.name} 没有裸 LF", lf == crlf, f"LF {lf} / CRLF {crlf}")


# ---------------------------------------------------------------- B 引用
def check_refs() -> None:
    text = BAT.read_text(encoding="ascii")
    # bat 里点名的相对路径（写死几个关键件；比解析 bat 语法稳）
    wanted = [
        "requirements.txt",
        r"data\seed_statutes.jsonl",
        r"data\statutes.jsonl",
        r"scripts\make_seed.py",
        r"scripts\import_law_text.py",
        ".env.example",
        "notice.txt",
    ]
    missing = [w for w in wanted if not (ROOT / w.replace("\\", "/")).exists()]
    # 缺 statutes.jsonl 是允许的（有 gate 兜底），其余绝不能缺
    missing = [m for m in missing if m != r"data\statutes.jsonl"]
    check("bat 点名的文件都在", not missing, "、".join(missing) or f"{len(wanted)} 个引用")
    check("bat 里确实调了 uvicorn", "uvicorn app.server:app" in text)


# ---------------------------------------------------------------- C 语法
def _strip_quoted(s: str) -> str:
    """去掉双引号包起来的部分。

    块内 `%PY% -c "print(...)"` 这种行，括号在引号里，cmd 不需要转义；
    不剥掉就会误报——检查器误报一次，人就开始忽略它了。
    """
    out, in_q = [], False
    for c in s:
        if c == '"':
            in_q = not in_q
        elif not in_q:
            out.append(c)
    return "".join(out)


def scan_parens(path: Path) -> tuple[list[tuple[int, str]], int]:
    """返回 (块内裸括号的行, 结束时块深度)。"""
    lines = path.read_text(encoding="ascii").splitlines()
    depth = 0
    bad: list[tuple[int, str]] = []
    for no, line in enumerate(lines, 1):
        if line.strip().lower().startswith("rem"):
            continue
        # 先剥引号内的内容，再去掉已转义的括号，剩下的才是 cmd 真正解析的括号
        body = _strip_quoted(line)
        stripped = body.replace("^(", "").replace("^)", "")
        opens = stripped.count("(")
        closes = stripped.count(")")
        if depth > 0 and (opens or closes):
            # 块内允许三种：行尾的 ( （开子块）、整行 ) 或 ) else (（关块）
            rest = stripped
            if rest.rstrip().endswith("("):
                rest = rest.rstrip()[:-1]
            if rest.strip() in (")", ")"):
                rest = ""
            rest = rest.replace(") else (", "")
            if rest.count("(") or rest.count(")"):
                bad.append((no, line.strip()[:70]))
        depth += opens - closes
        if depth < 0:
            bad.append((no, line.strip()[:70]))
            depth = 0
    return bad, depth


def check_parens(path: Path) -> None:
    """if/for 块内的裸括号。

    cmd 在块里遇到未转义的 ( 或 ) 会认错块边界，症状是
    「xx was unexpected at this time」或直接退出码 255，
    而且**不管那个块执不执行**——解析阶段就死了。
    """
    bad, depth = scan_parens(path)
    check(f"{path.name} 块内括号都已转义", not bad,
          f"{len(bad)} 处：{bad[:2]}" if bad else "扫描完成")
    check(f"{path.name} 括号配平", depth == 0, f"结束时深度 {depth}")


def self_test() -> None:
    """先证明这个检查器会红，再让它去体检。

    它失灵的样子不是报错，是**永远绿**——那比没有检查更坏，
    因为人会以为 bat 被守住了。
    """
    d = Path(tempfile.mkdtemp(prefix="bat_selftest_"))
    try:
        bad_bat = d / "bad.bat"
        bad_bat.write_text(
            '@echo off\n'
            'if not exist "x" (\n'
            '    echo 2^) rebuild (offline):\n'
            ')\n', encoding="ascii")
        good_bat = d / "good.bat"
        good_bat.write_text(
            '@echo off\n'
            'if not exist "x" (\n'
            '    echo 2^) rebuild ^(offline^):\n'
            '    %PY% -c "print(\'a\', (1+2))"\n'
            ')\n', encoding="ascii")
        bad, _ = scan_parens(bad_bat)
        good, _ = scan_parens(good_bat)
        check("自检：裸括号能被抓到", len(bad) == 1, f"抓到 {len(bad)} 处")
        check("自检：转义括号+引号内括号不误报", len(good) == 0, f"误报 {len(good)} 处")
    finally:
        # 第一版只 unlink 两个 .bat，目录留着——于是每跑一次 run_all，
        # %TEMP% 里就多一个空目录，实测攒了 16 个。测试留下的痕迹不分大小，
        # 反正没人会回头数第二遍。
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- D 行为
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _env_left_behind(had_env: bool) -> None:
    """bat 第 6 步会 `copy .env.example .env`，跑完得把痕迹收掉。

    这一条是在全新解压目录里发现的：跑完 run_all，目录里多出一个 `.env`，
    而它本来是「没有 .env」的干净状态。测试不该改变被测环境——第二次跑时
    环境已经和第一次不同了，而且万一 .env.example 哪天带了值，测试就会
    在别人机器上凭空造出一个「有配置」的目录。

    真有 .env 的机器（开发机）不动它：had_env 为真时直接返回。
    """
    if had_env:
        return
    leftover = ROOT / ".env"
    if leftover.exists():
        # 同样是清理动作：删不掉就留着。在这里抛异常，报出来的是
        # 「启动脚本坏了」，而真实原因只是「有个 .env 没能删掉」——
        # 而且它是在 finally 里抛，整个套件后面几十项一项都不再跑。
        _unlink_quiet(leftover)


def _unlink_quiet(p: Path) -> None:
    """删临时 .bat 失败**不算**这项失败。

    真机上见过两类原因：cmd 还攥着文件句柄（Windows 上很常见），
    以及受限环境把删除动作拦掉。清理不干净的唯一后果是留下一个
    `_bat_occupied.bat`，而 `check_pack_excludes()` 本来就在盯着这件事
    ——所以这里再抛异常，只会把一条环境噪声变成「启动脚本坏了」，
    而且是**在 finally 里抛**：整个套件连后面几十项都不跑了，
    报出来的耗时也只剩两三秒，看起来像脚本自己崩了。
    """
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def run_case_occupied() -> None:
    """占住一个端口，把副本的 PORT 指过去：必须拦，且不能启动服务。"""
    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    had_env = (ROOT / ".env").exists()

    raw = BAT.read_bytes()
    body = raw.replace(b'set "PORT=8000"', b'set "PORT=' + str(port).encode() + b'"')
    copy = ROOT / "_bat_occupied.bat"
    copy.write_bytes(body)
    try:
        r = subprocess.run(["cmd", "/c", str(copy)], cwd=str(ROOT),
                           input=b"\r\n", capture_output=True, timeout=120)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        check("端口被占时退出码非 0", r.returncode != 0, r.returncode)
        check("说清是谁占的（打印 PID）", "already in use by PID" in out)
        check("没有继续启动服务", "Starting the service" not in out)
        check("没有去开浏览器", "start \"\" http" not in out)
    finally:
        _unlink_quiet(copy)
        _env_left_behind(had_env)
        held.close()


def run_case_free() -> None:
    """空闲端口：必须真把服务起起来。副本里摘掉 start，免得弹浏览器。"""
    port = free_port()
    had_env = (ROOT / ".env").exists()
    raw = BAT.read_bytes()
    body = raw.replace(b'set "PORT=8000"', b'set "PORT=' + str(port).encode() + b'"')
    body = body.replace(b'start "" http://127.0.0.1:%PORT%\r\n',
                        b'rem [test] browser suppressed\r\n')
    copy = ROOT / "_bat_free.bat"
    copy.write_bytes(body)
    proc = None
    try:
        proc = subprocess.Popen(["cmd", "/c", str(copy)], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        up = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/meta", timeout=1) as f:
                    up = bool(f.read(40))
                break
            except Exception:               # noqa: BLE001
                pass
        check("端口空闲时服务真起来了", up, f"端口 {port}")
    finally:
        if proc:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        time.sleep(0.8)
        _unlink_quiet(copy)
        _env_left_behind(had_env)


def run_case_cli() -> None:
    """命令行入口：能起、能报出语料条数、/quit 干净退出。

    只测「起得来」。它读不读模型取决于有没有 key，而这里只发 /quit，
    不会产生任何调用——所以这项在有 key 的机器上也照样是零成本的。
    """
    r = subprocess.run(["cmd", "/c", str(CLI)], cwd=str(ROOT),
                       input=b"/quit\r\n\r\n", capture_output=True, timeout=150)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    check("命令行入口能起并干净退出", r.returncode == 0, r.returncode)
    check("命令行入口报出语料条数", "条条文" in out,
          next((ln.strip() for ln in out.splitlines() if "条条文" in ln), "")[:46])


def _seed_rows() -> int:
    return sum(1 for ln in SEED.read_text(encoding="utf-8").splitlines() if ln.strip())


def _raw_rows() -> int | None:
    """data/raw 按现有切分逻辑能切出多少条。

    直接复用 import_law_text.split_articles（纯函数），不去跑它的 main——
    main 会把结果写进 data/statutes.jsonl，检查器一跑就改了被检查的数据。
    """
    import importlib.util
    man = ROOT / "data" / "raw" / "manifest.json"
    if not man.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "_ilt_probe", ROOT / "scripts" / "import_law_text.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    n = 0
    for e in json.loads(man.read_text(encoding="utf-8")):
        f = ROOT / "data" / "raw" / e["file"]
        if not f.exists():
            continue
        arts, _ = mod.split_articles(mod.read_text(f))
        n += len(arts)
    return n


def _statutes_rows() -> int | None:
    """正式语料的条数（包里带的那份）。不在就返回 None。"""
    p = ROOT / "data" / "statutes.jsonl"
    if not p.exists():
        return None
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def check_numbers() -> None:
    """第 5 步告警里那两个写死的数字。

    `17` 和 `2060` 是收货方**第一眼**看到的数字——比 `交付说明.txt` 还靠前，
    用户是先看黑窗口、再翻文档的。可它们只是 echo 里的字面量：语料一变，
    这段话就开始骗人，而且没有任何东西会红。

    v96 给 `交付说明.txt` 补过这类对账，漏了 bat。同一组事实散在三处
    （语料文件 / 交付说明 / bat），前两处有机制盯着，第三处没有——
    漂移只是时间问题。

    第三条查的是那句「6 条工伤不在 raw 里」：6 必须等于
    `statutes(2066) − raw重建(2060)`，**不是** `seed(17) − raw`。
    （第一版就写成后者，算出 −2043，红得像是 bat 错了——检查器自己算错时，
    它红的样子和产品真坏了一模一样。所以指标定义要先写清楚再写断言。）
    """
    text = BAT.read_text(encoding="ascii")
    seed = _seed_rows()
    raw = _raw_rows()
    full = _statutes_rows()

    m1 = re.search(r"Only the (\d+)-article SEED corpus", text)
    check("bat：示例语料条数与文件一致",
          bool(m1) and int(m1.group(1)) == seed,
          f"bat 写 {m1.group(1) if m1 else '?'} / 实际 {seed}")

    m2 = re.search(r"\^\((\d+) articles\^\)", text)
    check("bat：重建条数与 data/raw 一致",
          bool(m2) and raw is not None and int(m2.group(1)) == raw,
          f"bat 写 {m2.group(1) if m2 else '?'} / 实际 {raw}")

    m3 = re.search(r"The (\d+) workplace-injury", text)
    diff = None if (raw is None or full is None) else full - raw
    check("bat：重建会丢的条数与实际差额一致",
          bool(m3) and diff is not None and int(m3.group(1)) == diff,
          f"bat 写 {m3.group(1) if m3 else '?'} / 实际 {diff}"
          + (f"（statutes {full} − raw {raw}）" if diff is not None else "（缺语料文件，算不出）"))


def check_pack_excludes() -> None:
    """测试临时产物不能被顺手打进交付包。

    check_bat 会在项目根写 `_bat_occupied.bat` / `_bat_free.bat`，finally 里删。
    可 finally 只管得住「正常返回」和「抛异常」两种情况——进程被强杀时什么
    都不跑，副本就留在根目录。而打包是**扫目录**的：排除表漏一条，它就会
    跟着包走，收货方解压后会看到一个陌生的 `_bat_occupied.bat`，双击还真能
    起服务（只是端口随机）。不至于出事，但没人解释得清它是什么。

    这里查的不是「文件现在在不在」，是**规则**：万一哪天有人整理排除表
    把它删了，这条会红。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pack_probe", ROOT / "scripts" / "pack_src.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    probes = ["_bat_occupied.bat", "_bat_free.bat"]
    leaked = [p for p in probes if mod.excluded(p) is None]
    check("测试临时产物被排除出交付包", not leaked,
          "、".join(leaked) or f"{len(probes)} 个探针")


def run_case_missing_corpus() -> None:
    """缺正式语料时的告警分支，真跑一次。

    为什么非补不可：`run_case_free` 在**项目根**跑，而那里 `statutes.jsonl`
    一直在，所以第 5 步的 `if not exist` 块**永远不执行**。于是此前只证明了
    「块不执行时解析不崩」，没证明「真的走进这个分支时它说什么、说完还能
    不能起服务」。v93 那颗雷（块内裸括号 → 解析期退出码 255）恰好就长在
    这个块里——**修好了，但没验过修好的那条路。**

    副本里做三处字节替换：
      · `set "PY="` → 强制成当前解释器。不强制的话 bat 会 `where python`
        命中系统里另一个解释器，那个可能没有 fastapi，于是它就去
        `pip install -r requirements.txt`——测试变成联网测试，还可能往
        别人机器上装包。
      · PORT → 随机端口；去掉 `start "" http`（不弹浏览器）。
    语料只带示例版（17 条），故意不带 `statutes.jsonl`。
    """
    prog = Path(sys.executable)
    probe = subprocess.run([str(prog), "-c", "import pydantic, fastapi, uvicorn"],
                           capture_output=True)
    if probe.returncode != 0:
        # 强制 PY 之后第 3 步必然走 pip install 分支。那一步是 bat 的设计，
        # 但不该由测试触发。说不清就跳过，别硬跑。
        skipped.append("缺语料告警（当前解释器没有 fastapi/uvicorn）")
        print("  [SKIP] 缺语料告警：当前解释器缺依赖，一跑就会联网 pip install")
        return

    d = Path(tempfile.mkdtemp(prefix="bat_nocorpus_"))
    port = free_port()
    log = d / "_out.log"
    proc = None
    try:
        (d / "data").mkdir()
        (d / "app").mkdir()
        shutil.copy2(SEED, d / "data" / "seed_statutes.jsonl")
        for p in (ROOT / "app").glob("*.py"):
            shutil.copy2(p, d / "app" / p.name)
        for name in ("requirements.txt", ".env.example", "notice.txt"):
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, d / name)

        body = BAT.read_bytes()
        body = body.replace(b'set "PY="',
                            b'set "PY=' + str(prog).encode("ascii") + b'"', 1)
        body = body.replace(b'set "PORT=8000"',
                            b'set "PORT=' + str(port).encode() + b'"')
        body = body.replace(b'start "" http://127.0.0.1:%PORT%\r\n',
                            b'rem [test] browser suppressed\r\n')
        copy = d / "launch.bat"
        copy.write_bytes(body)

        with open(log, "wb") as fh:
            proc = subprocess.Popen(["cmd", "/c", str(copy)], cwd=str(d),
                                    stdout=fh, stderr=subprocess.STDOUT)
            meta = {}
            for _ in range(60):
                time.sleep(0.5)
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/meta", timeout=1) as f:
                        meta = json.loads(f.read().decode("utf-8"))
                    break
                except Exception:               # noqa: BLE001
                    pass
        if proc:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
            proc.wait(timeout=15)
            proc = None
        time.sleep(0.4)
        out = log.read_text(encoding="utf-8", errors="replace")

        seed = _seed_rows()
        check("缺语料时才打告警", "[WARN] data\\statutes.jsonl NOT found" in out)
        check("告警里的示例条数是真的",
              f"Only the {seed}-article SEED corpus is available" in out,
              f"找 'Only the {seed}-article'")
        # v93 那颗雷的直接回归：转义括号在块内必须渲染成一个括号，而不是
        # 把块提前闭合。这两条过不去，说明第 5 步又变成了解析期错误。
        check("块内 ^( ^) 渲染成正常括号",
              "rebuild it from data\\raw\\ (offline)" in out)
        check("块内编号 ^) 没把块截断",
              "1) put statutes.jsonl back" in out and "2) rebuild it" in out)
        # 告警只是告警：带着两种语料里最小的那个，服务也必须起来。
        check("缺语料时服务照旧起来", bool(meta), f"端口 {port}")
        check("页面的条数与告警口径一致",
              meta.get("articles") == seed and meta.get("corpus_seed") is True,
              f"articles={meta.get('articles')} corpus_seed={meta.get('corpus_seed')}")
        check("notice.txt 真的被 type 出来了", "回放模式" in out)
        check("全程没有 traceback", "Traceback" not in out)
    finally:
        if proc:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
            proc.wait(timeout=15)
        time.sleep(0.4)
        shutil.rmtree(d, ignore_errors=True)


def self_test_real_block() -> None:
    """拿真文件的**副本**做回归：把那处 `^(offline^)` 改回裸括号，必须变红。

    为什么用副本：第一版我是直接改真文件、跑完在 finally 里还原——结果进程被
    SIGTERM 杀掉，finally 根本不执行（SIGTERM 默认行为是立即终止，不走异常
    处理），真文件就停在被改坏的状态。能救回来只因为恰好有一份同版本的快照。

    **做变异测试的脚本不该动被测文件本身**，否则它自己就成了新的故障源，
    而且出事的时机恰好是「它正在证明自己能抓故障」的时候。
    """
    d = Path(tempfile.mkdtemp(prefix="bat_mut_"))
    try:
        raw = BAT.read_bytes()
        mut = raw.replace(b"^(offline^)", b"(offline)")
        f = d / "mut.bat"
        f.write_bytes(mut)
        bad, _ = scan_parens(f)
        check("自检：真文件副本改裸括号后必红", len(bad) >= 1, f"抓到 {len(bad)} 处")
        check("自检：变异只动了那一处括号",
              mut != raw and len(raw) - len(mut) == 2, f"{len(raw)} → {len(mut)} 字节")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_port_consistency() -> None:
    """启动脚本的 `set "PORT=N"` 与 server.py 的 uvicorn.run 默认端口，必须一致。

    端口现在有两处来源：.bat 用 `--port %PORT%`（PORT 在 bat 里 `set` 一次），
    server.py 的 `__main__` 用 `uvicorn.run(port=int(os.environ.get("PORT", 8000)))`。
    两者默认都是 8000，但谁也没盯着谁——有人改了 bat 的 PORT、忘了 server.py 的
    默认，或直接 `python app/server.py` 起服务时就会对不上。这是 v96 那类
    「同一事实两处写死、只改一处」的同一个毛病，只是这次两边都是代码。
    """
    bat_text = BAT.read_text(encoding="ascii")
    srv_text = SERVER.read_text(encoding="utf-8")
    m_bat = re.search(r'set\s+"PORT=(\d+)"', bat_text)
    m_srv = re.search(r'os\.environ\.get\("PORT",\s*(\d+)\)', srv_text)
    if m_srv is None:
        # 兼容改动前的写法 port=8000
        m_srv = re.search(r'uvicorn\.run\([^)]*port=(\d+)', srv_text)
    if m_bat and m_srv:
        ok = m_bat.group(1) == m_srv.group(1)
        detail = f"bat 写 {m_bat.group(1)} / server.py 默认 {m_srv.group(1)}"
    else:
        ok = False
        detail = "两边至少有一个没解析到端口"
    check("启动脚本与 server.py 的默认端口一致", ok, detail)


def main() -> int:
    print("启动脚本门禁（双击入口，此前零覆盖）")
    for p in (BAT, CLI):
        if not p.exists():
            print(f"缺文件：{p}")
            return 1
    check_encoding(BAT)
    check_encoding(CLI)
    self_test()
    self_test_real_block()
    check_refs()
    check_parens(BAT)
    check_parens(CLI)
    check_numbers()
    check_pack_excludes()
    check_port_consistency()
    run_case_occupied()
    run_case_free()
    run_case_cli()
    run_case_missing_corpus()

    print()
    if failed:
        print(f"{passed} 项通过，{failed} 项失败"
              + (f"，{len(skipped)} 项跳过（{'、'.join(skipped)}）" if skipped else "")
              + "。")
        return 1
    if skipped:
        # 跳过也说出来。混在「全绿」里的跳过项等于没跑，只是没人知道。
        print(f"全部通过（{passed} 项，跳过 {len(skipped)} 项：{'、'.join(skipped)}）。")
    else:
        print(f"全部通过（{passed} 项）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
