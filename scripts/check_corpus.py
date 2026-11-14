# -*- coding: utf-8 -*-
"""回归：正式语料缺失时，界面必须自己说出来。

为什么值得单独一条门禁：
  这个故障**不报错**。data/statutes.jsonl 不见了，程序照常启动、页面照常打开、
  接口照常回 200，只是语料从 2066 条退回 seed_statutes.jsonl 里的 17 条示例。
  用户看到「条文 17 条」，第一反应是「少」，实际是「几乎没有」——大部分提问
  都会答「本库未收录」。用户会以为产品不行，而不是知道语料没导入。
  这种「静默降级」没法靠异常日志发现，只能靠一条断言把它钉住。

两个界面都要验，因为它们是两拨人看到的第一屏：
  登录页 #stat  ——  只在语料是示例时亮警告（v127 起正常态不再报规模）
  对话页 #meta  ——  已登录的人靠这行判断答案为什么总是「本库未收录」

怎么造出「缺语料」而不动真实文件：
  设 LAW_CORPUS_PATH 指向示例语料即可（见 app/config.py）。早先的做法是把
  statutes.jsonl 挪走、验完再挪回来——中途被 Ctrl-C 或进程被杀，1.4MB 的正式
  语料就留在某个 .hidden 名字下，下次启动静默跑 17 条。为了测一个「静默降级」
  去人为制造另一次静默降级，不值得。

两个方向都验（这是自带的对照组）：
  A. 指到示例语料 → corpus_seed 真、条文 17 条、两处都带 .seed 类且说「示例语料」
  B. 正常启动     → corpus_seed 假、条文 2066 条、两处**都不带** .seed 类
  只有 A 会通过说明不了什么（一个恒真的告警也能过），B 才是防「天天喊狼来了」。

跑法：
    ./.venv/Scripts/python.exe scripts/check_corpus.py
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _client import Client                       # noqa: E402
from _svc import start_service, stop_service     # noqa: E402
from app.auth import AuthStore                   # noqa: E402
from app.config import DB_PATH, TOKEN_NAME       # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9336
PROFILE = ROOT / "eval" / "_shot" / "profile_corpus"

fails: list[str] = []
_msg_id = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))
    if not ok:
        fails.append(name)


async def send(ws, method: str, params: dict | None = None) -> dict:
    global _msg_id
    _msg_id += 1
    mid = _msg_id
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


async def js(ws, expr: str):
    r = await send(ws, "Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "awaitPromise": True})
    if r.get("exceptionDetails"):
        raise RuntimeError(str(r["exceptionDetails"])[:300])
    return r.get("result", {}).get("value")


async def read_el(ws, url: str, sel: str) -> dict:
    """打开页面，读某个元素**真正渲染出来**的东西。

    只验接口是不够的：corpus_seed 字段早就返回了，界面却一直没地方用它。
    「接口给了、页面没用」正是这个套件要抓的那类问题。
    文案和 class 一起读——只读文案的话，忘了加 .seed 也能过，
    而那样告警和普通信息就长得一模一样，等于白写。
    """
    await send(ws, "Page.navigate", {"url": url})
    await asyncio.sleep(2.3)
    return await js(ws, "(()=>{const e=document.querySelector(" + json.dumps(sel) + ");"
                        "if(!e)return {missing:true};"
                        "return {text:e.textContent.trim(),"
                        "seed:e.classList.contains('seed'),"
                        "color:getComputedStyle(e).color};})()")


async def probe(ws, base: str, token: str) -> tuple[dict, dict]:
    """先以**未登录**身份看登录页，再带 cookie 看对话页。

    顺序不能反：cookie 是按 127.0.0.1 存的，跟端口无关。先登录的话，
    第二次去看 /login 会被重定向进站内，`#stat` 根本不存在，
    表现成「元素缺失」——看着像页面坏了，其实是探针自己没清场。
    """
    await send(ws, "Network.deleteCookies", {"name": TOKEN_NAME,
                                             "domain": "127.0.0.1"})
    login = await read_el(ws, base + "/login", "#stat")
    await send(ws, "Network.setCookie", {
        "name": TOKEN_NAME, "value": token, "domain": "127.0.0.1",
        "path": "/", "httpOnly": True, "sameSite": "Lax"})
    app = await read_el(ws, base + "/", "#meta")
    return login, app


def launch_edge():
    return subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_ws() -> str:
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json",
                                        timeout=1) as f:
                targets = json.load(f)
            page = next((t for t in targets if t["type"] == "page"), None)
            if page:
                return page["webSocketDebuggerUrl"]
        except Exception:          # noqa: BLE001
            pass
        time.sleep(0.25)
    raise RuntimeError("Edge 调试端口没起来")


def temp_login_token() -> tuple[str, int | None]:
    """借一个普通账号签个令牌；库里没有就临时建一个，跑完删。

    不借用管理员：管理员的首页多一个后台入口，读到的 #meta 还是同一个，
    但这套东西不该依赖管理员账号存在。
    """
    store = AuthStore(DB_PATH)
    users = store.list_users(size=200)["items"]
    demo = next((u for u in users
                 if u["role"] == "user" and u["status"] == "active"), None)
    created = None
    if demo is None:
        demo = store.create_user("corpus-check-demo", "corpus-check-pw",
                                 role="user", ip="127.0.0.1")
        created = demo["id"]
        print("  库里没有普通账号，临时建了一个（跑完删）")
    return store.issue_token(demo["id"], ua="corpus-check"), created


def drop_temp_user(uid: int) -> None:
    store = AuthStore(DB_PATH)
    try:
        store._conn.execute("DELETE FROM auth_tokens WHERE user_id=?", (uid,))
        store._conn.execute("DELETE FROM users WHERE id=?", (uid,))
        store._conn.commit()
    except Exception as e:          # noqa: BLE001
        print(f"  [WARN] 临时账号没清干净：{e}")


def check_raw_readme_numbers() -> None:
    """data/raw/README.md 的标题数字必须和算出来的对上。

    这份文件打进包里、跟着源码走，而且是「别随手覆盖语料」的告警——它说的数字
    一旦漂了，接手的人会照着错的认知去操作。和 v96 / v99 / v100 同一个筛子：
    同一组语料事实散在好几处（语料文件 / 交付说明 / .bat / pack_src 的 VERIFIED /
    这份 README），前四处都有机制盯，这份是最后一处没盯的。

    只看标题那四个最容易被改语料带偏的数：部数、raw 重建条数、statutes 条数、
    两者之差。表格里逐法的条数（98 / 107 / …）不在这里盯——那是更细的层面，
    且 2060 那个总数已经把表格求和间接钉住了。
    """
    raw_readme = ROOT / "data" / "raw" / "README.md"
    if not raw_readme.exists():
        return
    text = raw_readme.read_text(encoding="utf-8")

    full = ROOT / "data" / "statutes.jsonl"
    statutes = sum(1 for l in full.read_text(encoding="utf-8").splitlines() if l.strip()) \
        if full.exists() else None
    import import_law_text as ilt
    mf = ROOT / "data" / "raw" / "manifest.json"
    raw_n = 0
    n_laws = 0
    if mf.exists():
        for e in json.loads(mf.read_text(encoding="utf-8")):
            f = ROOT / "data" / "raw" / e["file"]
            if f.exists():
                arts, _ = ilt.split_articles(ilt.read_text(f))
                raw_n += len(arts)
                n_laws += 1
    diff = None if (statutes is None or raw_n is None) else statutes - raw_n

    m_laws = re.search(r"(\d+) 部全部就位", text)
    m_raw = re.search(r"全部就位（(\d+) 条）", text)
    m_stat = re.search(r"有 \*\*(\d+) 条\*\*", text)
    m_diff = re.search(r"(\d+) 条是《工伤保险条例》", text)

    check("raw README：部数与 manifest 一致",
          bool(m_laws) and n_laws is not None and m_laws.group(1) == str(n_laws),
          f"README 写 {m_laws.group(1) if m_laws else '?'} / 实际 {n_laws}")
    check("raw README：重建条数与 raw 一致",
          bool(m_raw) and raw_n is not None and m_raw.group(1) == str(raw_n),
          f"README 写 {m_raw.group(1) if m_raw else '?'} / 实际 {raw_n}")
    check("raw README：statutes 条数与文件一致",
          bool(m_stat) and statutes is not None and m_stat.group(1) == str(statutes),
          f"README 写 {m_stat.group(1) if m_stat else '?'} / 实际 {statutes}")
    check("raw README：差的那几条与两数之差一致",
          bool(m_diff) and diff is not None and m_diff.group(1) == str(diff),
          f"README 写 {m_diff.group(1) if m_diff else '?'} / 实际 {diff}")



async def main() -> int:
    seed = ROOT / "data" / "seed_statutes.jsonl"
    if not seed.exists():
        print(f"缺少示例语料：{seed}")
        return 1

    # ---- 0. 静态：从 data/raw/ 重建会不会丢法 ----
    # import_law_text.py 只会切 raw/ 里有的原文，而 statutes.jsonl 里有条文
    # 从来没有原文（工伤保险条例那 6 条是单独补录的）。这类「只在 JSONL 里」
    # 的法最容易在重建时无声消失——所以把它显式列出来，多出新的就红。
    known_extra = {"工伤保险条例"}          # 已知：raw 无原文，单独补录
    full = ROOT / "data" / "statutes.jsonl"
    if full.exists():
        cur = {json.loads(l)["law_short"] for l in
               full.read_text(encoding="utf-8").splitlines() if l.strip()}
        mf = ROOT / "data" / "raw" / "manifest.json"
        raw_laws = set()
        if mf.exists():
            for e in json.loads(mf.read_text(encoding="utf-8")):
                if (ROOT / "data" / "raw" / e["file"]).exists():
                    raw_laws.add(e["short"])
        extra = sorted(cur - raw_laws)
        check("JSONL 里多出来的法只有已知那几个", set(extra) <= known_extra,
              f"多出 {extra}（raw 里没原文，重建会丢）" if extra
              else f"{len(cur)} 部的法都在 raw 或已知例外里")

    # ---- 0.5 静态：data/raw/README.md 的标题数字与语料对得上 ----
    check_raw_readme_numbers()

    token, created = temp_login_token()
    svc = None
    proc = None
    try:
        proc = launch_edge()
        uri = await wait_ws()
        import websockets
        async with websockets.connect(uri, max_size=None) as ws:
            await send(ws, "Network.enable")
            await send(ws, "Page.enable")
            await send(ws, "Runtime.enable")
            # 复用的 profile 目录会缓存 ui.js/login.html 的旧版本，
            # 表现是「新加的分支不存在」，人容易去怀疑代码而不是缓存。
            await send(ws, "Network.setCacheDisabled", {"cacheDisabled": True})
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})

            # ---- A：指到示例语料，两处都要明说 ----
            print("A. 只有示例语料时：")
            svc, base = start_service(env={"LAW_CORPUS_PATH": str(seed)})
            st, m = Client(base).json("/api/meta")
            check("corpus_seed 为真", m.get("corpus_seed") is True,
                  m.get("corpus_seed"))
            # 不写死 17。要钉的事实是「接口报的条数 == 示例语料文件的行数」，
            # 语料本身会增补——写死的话每加一条条文就得来改这里，而改完这条
            # 断言也还是只验了一个数字。README 末尾「一句提醒」里的那个条数
            # 没有断言看得见，改语料时记得顺手核一眼。
            #
            # 文件整个不存在的情况，main() 开头已经拦住了（打印路径后 return 1，
            # 不抛栈）。这层 try 只兜「文件在、但读不出来」——权限、编码坏了。
            # 那种情况下要变成**一条清楚的失败**，不能让整个套件裸 traceback
            # 退出：检查脚本自己先崩，现场看着像被测的服务坏了，人会去查服务
            # 而不是查语料。（编码损坏这条路径没有实测过，别当它已被验证。）
            try:
                n_seed = sum(1 for ln in seed.read_text(encoding="utf-8").splitlines()
                             if ln.strip())
            except (OSError, UnicodeDecodeError) as e:
                n_seed = None
                print(f"  读不了示例语料 {seed}：{type(e).__name__}: {e}")
            label = f"{n_seed} 条" if n_seed is not None else "（示例语料读不出来）"
            check(f"条文数回退到示例语料的 {label}",
                  n_seed is not None and m.get("articles") == n_seed,
                  m.get("articles"))

            login, app = await probe(ws, base, token)
            check("登录页 #stat 存在", not login.get("missing"), login)
            check("登录页带 .seed 类", login.get("seed") is True, login.get("seed"))
            check("登录页文案说「示例语料」",
                  "示例语料" in (login.get("text") or ""), login.get("text"))
            # 告警不能把「运行模式」挤掉：这两件事容易同时发生（同一台机器、
            # 同一批没配好的东西）。上一版是整行替换，缺语料时模式信息消失了。
            check("登录页仍然报运行模式",
                  "LLM" in (login.get("text") or ""), login.get("text"))
            print(f"        登录页：{login.get('text')}")
            check("对话页 #meta 存在", not app.get("missing"), app)
            check("对话页带 .seed 类", app.get("seed") is True, app.get("seed"))
            check("对话页文案说「示例语料」",
                  "示例语料" in (app.get("text") or ""), app.get("text"))
            print(f"        对话页：{app.get('text')}")

            # ---- B：正式语料在位，不许喊狼来了 ----
            print("B. 正式语料在位时（对照）：")
            stop_service(svc)
            svc = None
            svc, base2 = start_service()
            st, m2 = Client(base2).json("/api/meta")
            check("corpus_seed 为假", m2.get("corpus_seed") is False,
                  m2.get("corpus_seed"))
            check("条文数是正式语料的量", (m2.get("articles") or 0) > 2000,
                  m2.get("articles"))

            login2, app2 = await probe(ws, base2, token)
            check("登录页没被误标", login2.get("seed") is False, login2.get("seed"))
            check("登录页文案是正常收录",
                  "示例语料" not in (login2.get("text") or ""), login2.get("text"))
            check("对话页没被误标", app2.get("seed") is False, app2.get("seed"))
            check("对话页文案是正常收录",
                  "示例语料" not in (app2.get("text") or ""), app2.get("text"))
            print(f"        登录页：{login2.get('text')}")

            # 只加 class 不改颜色的话，告警和普通灰字看不出区别。
            # 这里顺带确认两处真的换了颜色（暗色主题下 warn 是 #e0b25e）。
            check("登录页告警色与正文不同",
                  login.get("color") != login2.get("color"),
                  f"{login.get('color')} vs {login2.get('color')}")
    finally:
        if svc:
            stop_service(svc)
        if proc:
            proc.terminate()
        if created is not None:
            drop_temp_user(created)

    print()
    if fails:
        print(f"未通过 {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
