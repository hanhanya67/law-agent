# -*- coding: utf-8 -*-
"""
给三个页面截图，用来检查界面有没有塌。改过 CSS 或模板之后跑一次。

为什么不装 Playwright / agent-browser：
  本机只要 500MB 的 Chromium 下载，而 Edge 已经装好了，并且支持
  --headless=new + CDP。用 CDP 直接干两件小事（塞 cookie、截屏），
  比为了截图引入一个浏览器运行时划算得多。

为什么能直接塞 cookie：
  令牌是**存在库里**的，不是签名出来的。任何能打开这个 sqlite 的进程
  调一次 issue_token 就能生成一个服务端认的令牌——这正是「令牌存表」
  换来的便利（代价是每次请求都要查一次库，这个代价值得付）。

跑法（服务由本脚本自己起，端口随机挑，不用先开服务）：
    ./.venv/Scripts/python.exe scripts/shot_pages.py
产物：eval/_shot/*.png
"""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 命令行里给的名字片段，用来只重出关心的几张；不给就全出
ONLY = [a for a in sys.argv[1:] if not a.startswith("-")]

from app.auth import AuthStore              # noqa: E402
from app.config import DB_PATH, TOKEN_NAME  # noqa: E402
from app.costs import bad_percentages as _bad_pct   # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from _svc import start_service, stop_service   # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
OUT = ROOT / "eval" / "_shot"
# 服务由本脚本自己起（见 main()），端口随机挑。写死 8000 等于默认
# 「8000 上那个服务是本目录的」——这条前提从来没被验证过（详见 _svc.py）。

_msg_id = 0


async def send(ws, method: str, params: dict | None = None) -> dict:
    global _msg_id
    _msg_id += 1
    mid = _msg_id
    await ws.send(json.dumps({"id": mid, "method": method,
                              "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


async def set_cookie(ws, token: str) -> None:
    # domain 用 127.0.0.1 而不是 localhost：cookie 不区分端口，但区分 host，
    # 两者在浏览器里是两套 cookie 空间，混用会出现「明明设了却带不上」。
    await send(ws, "Network.setCookie", {
        "name": TOKEN_NAME, "value": token, "domain": "127.0.0.1",
        "path": "/", "httpOnly": True, "sameSite": "Lax",
    })


async def shot(ws, url: str, name: str, wait: float = 2.0,
               width: int = 1440, height: int = 980,
               js: str | None = None, theme: str | None = None) -> bool:
    # 只想重出某几张时：scripts/shot_pages.py slots doc
    # 十几张全出要几分钟，改一处样式却只关心其中两张的时候很浪费。
    # 返回值 = 这张到底截没截。调用方后面的断言必须据此跳过：只给
    # `chat_tasks` 两个关键字时，后台档案那张本来就没拍，弹窗自然不存在，
    # 断言照跑就会报一个假失败（踩过一次）。
    if ONLY and not any(o in name for o in ONLY):
        return False
    await send(ws, "Emulation.setDeviceMetricsOverride", {
        "width": width, "height": height, "deviceScaleFactor": 1,
        "mobile": False})
    await send(ws, "Page.navigate", {"url": url})
    await asyncio.sleep(wait)
    if theme:
        # 主题存在 localStorage 里，只能在同源页面加载之后设，设完 reload
        # 才能拿到 data-theme 生效后的真实渲染。
        await send(ws, "Runtime.evaluate", {"expression":
            "localStorage.setItem('law.theme','%s');location.reload();" % theme})
        await asyncio.sleep(max(wait, 1.8))
    if js:
        await send(ws, "Runtime.evaluate", {"expression": js, "awaitPromise": True})
        await asyncio.sleep(0.9)
    # 等两帧再截。headless=new 下如果页面没产生新帧，captureScreenshot 会
    # 把上一帧还回来：DOM 明明改了，图里还是旧的，最容易让人对着截图查半天
    # 一个已经修好的 bug。
    await send(ws, "Runtime.evaluate", {
        "expression": "new Promise(r=>requestAnimationFrame("
                      "()=>requestAnimationFrame(r)))",
        "awaitPromise": True})
    await asyncio.sleep(0.2)
    r = await send(ws, "Page.captureScreenshot",
                   {"format": "png", "fromSurface": True,
                    "captureBeyondViewport": False})
    p = OUT / name
    p.write_bytes(base64.b64decode(r["data"]))
    print(f"  {name}  {p.stat().st_size // 1024} KB")
    return True


async def _capture(ws, name: str) -> bool:
    """拍**当前页**，不导航。

    用于「在同一个页面上做了一串动作」的取证：换法之后再问一句，
    中间一旦 reload，sessionId 和 currentTask 就都没了，等于换了一条咨询链，
    这一步要验的东西也就不存在了。ONLY 过滤同 shot()。
    """
    if ONLY and not any(o in name for o in ONLY):
        return False
    await send(ws, "Runtime.evaluate", {
        "expression": "new Promise(r=>requestAnimationFrame("
                      "()=>requestAnimationFrame(r)))",
        "awaitPromise": True})
    await asyncio.sleep(0.3)
    r = await send(ws, "Page.captureScreenshot",
                   {"format": "png", "fromSurface": True,
                    "captureBeyondViewport": False})
    p = OUT / name
    p.write_bytes(base64.b64decode(r["data"]))
    print(f"  {name}  {p.stat().st_size // 1024} KB")
    return True


def _payslip_b64() -> str:
    """合成一张工资条，专供「图片证据」那张截图用。

    不是为了造假数据：回放模式下的读图结果本来就是固定样例（见
    `llm._MOCK_VISION`），这里只是让**截图里的图和右边的依据栏对得上**——
    图上写「应发工资合计 12,000.00」，依据栏也写这一句，看图的人不会以为
    两边在讲两件事。真跑在线模式时，那张图是用户自己传的，这段代码不参与。
    """
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    def font(path: str, size: int):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            return ImageFont.load_default()

    im = Image.new("RGB", (620, 400), "#ffffff")
    d = ImageDraw.Draw(im)
    fb = font("C:/Windows/Fonts/msyhbd.ttc", 21)
    f = font("C:/Windows/Fonts/msyh.ttc", 17)
    fl = font("C:/Windows/Fonts/msyh.ttc", 14)
    ink, dim = "#1c1c1c", "#6b6b6b"

    d.text((28, 22), "杭州某科技有限公司", font=fb, fill=ink)
    d.text((28, 50), "2024 年 3 月工资条", font=fl, fill=dim)
    d.line([(28, 78), (592, 78)], fill="#d8d8d8", width=2)

    rows = [("姓名", "张伟"), ("入职时间", "2023年4月"),
            ("应发工资合计", "12,000.00"), ("个人社保", "1,438.60")]
    y = 96
    for k, v in rows:
        d.text((28, y), k, font=f, fill=dim)
        d.text((300, y), v, font=f, fill=ink)
        y += 38

    d.line([(28, y + 4), (592, y + 4)], fill="#d8d8d8", width=1)
    d.text((28, y + 18), "备注：经双方协商一致，自即日起解除劳动关系。",
           font=fl, fill=dim)

    buf = BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# v117 办案包。历史会话回放不会重发 SSE 的 extras 事件——只靠
# loadSession() 截出来的聊天页永远看不到答案下面那五块，所以必须和
# check_ui.py 一样显式取一次再交给渲染层。这不是造一份假数据：
# fetch 打的是真实接口，走的就是 from_case 那套纯规则。
_EXTRAS_Q = "公司没签劳动合同，还拖欠两个月工资，能要回多少钱？"


def _extras_js(focus: str = "all") -> str:
    """把办案包画进 feed 的 JS。

    focus='all' 五块全展开看全貌；focus='decision' 只展开判例参考与
    显式画像，给 v117 的新功能拍特写。两张都从同一个接口取数，
    免得截图和真实链路长出两套数据。
    """
    opener = ("document.querySelectorAll('#feed .extras .ex-sec')"
              ".forEach(s=>{s.open=true;});"
              if focus == "all" else
              "const secs=document.querySelectorAll('#feed .extras .ex-sec');"
              "if(secs.length>=5){secs[3].open=true;secs[4].open=true;}")
    q = json.dumps(_EXTRAS_Q, ensure_ascii=False)
    return ("(async()=>{"
            "const r=await fetch('/api/case/extras',{"
            "method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({question:" + q + ","
            "slots:{monthly_wage:12000,has_contract:false}})});"
            "const d=await r.json();"
            "const ctx=addTurn(" + q + ");"
            "handle('extras',d,ctx,{});" + opener +
            "document.querySelectorAll('#feed .extras .dc-case')"
            ".forEach(s=>{s.open=true;});"
            "const ex=document.querySelector('#feed .extras');"
            "if(ex){ex.scrollIntoView({block:'start'});}"
            "else{window.scrollTo({top:0});}"
            "await new Promise(z=>setTimeout(z,700));"
            "return document.querySelectorAll('#feed .extras .ex-sec').length;"
            "})()")


# 截图最容易骗人的是「脚本全绿、图里却是旧的」：navigate 之后 DOM 没画出来，
# PNG 照样生成。所以每次拍完立刻回读一次真实 DOM，把关键数字打出来并卡住。
# docH 顺带量出整页高度，用来定截多高——拍到一半等于没拍。
_EXTRAS_PROBE = """(()=>{
  const root=document.querySelector('#feed .extras');
  if(!root) return {secs:0};
  const secs=[...root.querySelectorAll('.ex-sec')];
  const btns=[...root.querySelectorAll('button')];
  return {secs:secs.length, opened:secs.filter(s=>s.open).length,
          cases:root.querySelectorAll('.dc-case').length,
          stats:root.querySelectorAll('.dc-stat').length,
          rows:root.querySelectorAll('.dc-tr').length,
          blank:(root.querySelector('.ex-ctext')||{}).textContent||'',
          // v140：不再在 JS 里判 indexOf('%')。v137 起代理费写「按标的
          // 10%–20%」、诉讼费写「1–10 万元部分按 2.5%」，都是合法费率，
          // 一刀切必然误报。文本回传给 Python，走 costs.bad_percentages()。
          text:(root.textContent||''),
          net:root.querySelectorAll('.dc-net').length,
          netText:(root.querySelector('.dc-net')||{}).textContent||'',
          extBox:root.querySelectorAll('.dc-ext').length,
          extBtn:btns.filter(b=>(b.textContent||'').indexOf('联网补充线索')>=0).length,
          exTop:Math.round(root.getBoundingClientRect().top+window.scrollY),
          docH:document.documentElement.scrollHeight};
})()"""


def seed_task_db(db_path: Path) -> tuple[str, int]:
    """在临时库里造「一个用户 + 两个任务 + 若干会话/消息」，返回 (令牌, 用户 id)。

    为什么要把 uid 一起返回：后面「未分类会话」那一步要**当场再种一条**
    不属于任何任务的会话（原因见那一步的注释），种的时候需要 uid。
    只返回令牌的话，那一步就得再按用户名查一次库——查得到，但多一次耦合。

    为什么不往 data/law.db 里塞：这些都是演示数据，进了真实库就得再想办法删，
    而删文件在 safe-delete 下并不省事（这个项目已经被咬过一次）。
    用临时库还有个附带好处——任务面板的截图不再依赖「真实库里恰好有任务」，
    否则换台机器跑出来就是一张空状态图，看着像功能坏了。
    """
    from app.auth import AuthStore as _AS
    from app.memory import MemoryStore as _MS
    from app.schemas import CaseSlots as _CS

    st = _AS(db_path)
    u = st.create_user("shotdemo", "Shot12345", role="user", ip="127.0.0.1")
    uid = u["id"]
    st.save_profile(uid, {
        "real_name": "王小明", "phone": "13800138000",
        "address": "杭州市西湖区文一西路 1 号", "region": "杭州市西湖区",
        "company_name": "杭州某某科技有限公司", "commission": "杭州市",
    }, onboarding="done")

    mem = _MS(db_path)
    # 两件案子故意分属两部法：任务卡要按「这件纠纷属于哪部法」上色，
    # 两张卡颜色一样的话，那张截图证明不了这件事。
    t1 = mem.create_task(uid, "某某公司拖欠工资", profile_id="labor")
    t2 = mem.create_task(uid, "未缴社保与二倍工资差额")
    # 第一个任务给两次咨询、第二个给一次，面板上的「N 次咨询 · M 轮对话」
    # 才有区别。数字全一样的话，看不出这一行到底读的是不是当前任务的数。
    plan = [(t1["task_id"], "shot_t1a", 3), (t1["task_id"], "shot_t1b", 2),
            (t2["task_id"], "shot_t2a", 1)]
    for tid, sid, rounds in plan:
        mem.ensure_session(sid, uid, tid)
        # 每个会话都写一份案情要素。不写的话文书面板会显示
        # 「将使用：0 项案情要素」——数字对得上，但这张图就成了一张
        # 永远碰不到非零分支的图，而且真实用户看不到这种状态。
        # 同一任务的两次咨询故意给不同的要素：合并逻辑（新值胜）才有东西可测。
        mem.save_slots(sid, _CS(
            region="杭州市西湖区", employment_type="全职",
            has_contract=(sid != "shot_t1b"),
            start_date="2022-03-01", monthly_wage=9000))
        for i in range(rounds):
            mem.save_message(sid, "user", f"我第 {i + 1} 次想问一下工资的事")
            mem.save_message(sid, "assistant", "（演示用回答）")
    # 第二件案子归档掉：文书下拉必须还能看到它，并且标出「已结案」。
    # 归档 = 结案，不是删掉；而结案之后正是最需要出申请书的时候。
    mem.update_task(t2["task_id"], uid, status="archived")

    # 一条「不属于任何任务」的老会话（task_id = NULL）。
    # 它在**这一步**的用途只有一个：让任务面板多出一张自动收编的「历史咨询」
    # 卡片（面板断言因此是 2 张，不是 1 张）。它自己撑不到「单会话收据」
    # 那一步——中间任何一次 /api/tasks 都会把它收编走，所以那一步另行现种。
    # 留着它是因为：面板上的「历史咨询」不是一个可以手工造出来的状态，
    # 只有库里真有一条未归属会话才会出现。
    # 这条归到消费内核：任务面板只列办理中的两张卡（t1 + 自动收编的它），
    # 两张都上劳动法的朱红就证明不了「任务卡按各自的法上色」。内容也跟着
    # 换成消费纠纷——徽章写食安、正文问工资，比不标更误导。
    loose_sid = "shot_loose"
    mem.ensure_session(loose_sid, uid, profile_id="consumer")   # 不给 task_id
    mem.save_slots(loose_sid, _CS())
    mem.save_message(loose_sid, "user", "超市买的进口奶粉没有中文标签，能退一赔十吗")
    mem.save_message(loose_sid, "assistant", "（演示用回答）")

    mem.close()
    return st.issue_token(uid, ua="shot"), uid


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    # 留 5 秒缓冲：文件时间戳精度不一，边界上的图会被误判成「旧的」。
    started = time.time() - 5
    store = AuthStore(DB_PATH)
    users = {u["username"]: u for u in store.list_users(size=100)["items"]}
    if "admin" not in users:
        print("库里没有 admin，先跑 scripts/make_admin.py")
        return 1
    tok_admin = store.issue_token(users["admin"]["id"], ua="shot")

    # 普通用户：优先用演示账号，没有就临时签一个的令牌无意义（账号不存在），
    # 所以退化到只截登录页 + 后台。
    demo = next((u for n, u in users.items()
                 if u["role"] == "user" and u["status"] == "active"), None)
    tok_demo = store.issue_token(demo["id"], ua="shot") if demo else None

    # 任务面板那几张单独用一个种子化的临时库（见 seed_task_db 的说明）。
    # 用系统临时目录 + 每轮唯一名字，**不再「先删旧的再建」**：
    # 本机的 safe-delete 会拦删除（SAFE_DELETE_BULK_CONFIRM_REQUIRED），
    # 曾经因为这个 unlink 抛异常，整个截图脚本 0.8 秒就死了，报错还长得
    # 像截图失败。干净的做法是不依赖删除这个前置条件。
    tmpdir = Path(tempfile.mkdtemp(prefix="law_shot_"))
    tmpdb = tmpdir / "shot.db"
    tok_task, shot_uid = seed_task_db(tmpdb)
    svc_task, base_task = start_service(env={"LAW_DB_PATH": str(tmpdb)})
    print(f"  任务面板服务：{base_task}")

    # 与 check_ui.py 同理：自己起服务，不连 8000 上「碰巧存在」的那个。
    svc, base = start_service()
    print(f"  已起服务：{base}")
    # 「缺正式语料」那张图要一个语料刻意降级的服务。用到再起（放在最后），
    # 免得只想重出一张图的时候也白等它。
    svc_seed = None

    proc = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={OUT / 'profile'}", "--disable-gpu",
         "--hide-scrollbars", "--no-first-run",
         "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        uri = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/json", timeout=1) as f:
                    targets = json.load(f)
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    uri = page["webSocketDebuggerUrl"]
                    break
            except Exception:      # noqa: BLE001
                pass
            time.sleep(0.25)
        if not uri:
            print("Edge 调试端口没起来")
            return 1

        import websockets
        async with websockets.connect(uri, max_size=None) as ws:
            await send(ws, "Network.enable")
            await send(ws, "Page.enable")
            await send(ws, "Runtime.enable")

            print("截图：")
            # 案情档案与仲裁申请书。用 admin 的会话（那份里有完整要素），
            # 空字段的档案面板看不出版式，截了也没意义。
            if "admin" in users:
                await set_cookie(ws, tok_admin)
                open_slots = ("(async()=>{"
                              "window.LawNav.history();"
                              "await new Promise(r=>setTimeout(r,1500));"
                              "const it=document.querySelector('#dlist .di');"
                              "if(it) it.click();"
                              "await new Promise(r=>setTimeout(r,1700));"
                              "window.LawNav.slots();"
                              "await new Promise(r=>setTimeout(r,800));})()")
                await shot(ws, base + "/", "chat_slots.png",
                           theme="light", js=open_slots)
                await shot(ws, base + "/", "chat_slots_dark.png",
                           theme="dark", js=open_slots)
                open_doc = open_slots.replace(
                    "window.LawNav.slots();",
                    "window.LawNav.slots();"
                    "await new Promise(r=>setTimeout(r,800));"
                    "document.getElementById('sl-doc').click();")
                await shot(ws, base + "/", "chat_doc.png",
                           theme="light", js=open_doc, wait=2.4)

                # 搜索：关键词取第一个会话标题的前两个字，保证一定命中。
                # 固定写死一个词的话，换了演示数据就截出一张「没有找到」的图。
                await shot(ws, base + "/", "chat_search.png", theme="light",
                           js="(async()=>{"
                              "window.LawNav.history();"
                              "await new Promise(r=>setTimeout(r,1500));"
                              "const t=document.querySelector('#dlist .di .t');"
                              "const kw=(t?t.textContent:'').slice(0,2);"
                              "const n=document.getElementById('d-q');"
                              "n.value=kw;n.dispatchEvent(new Event('input'));"
                              "await new Promise(r=>setTimeout(r,1200));})()")
                # 反馈按钮：要先载入会话，答案渲染完才会挂上工具条
                await shot(ws, base + "/", "chat_fb.png", theme="light",
                           js="(async()=>{const r=await fetch('/api/chat/history');"
                              "const d=await r.json();"
                              "if(d.items&&d.items[0]){await loadSession("
                              "d.items[0].session_id);}"
                              "window.scrollTo({top:0});"
                              "await new Promise(z=>setTimeout(z,900));})()")

                # v117 办案包五块 + 判例参考特写，v118 补上网络状态行与
                # 「联网补充线索」按钮。只重出这三张时用
                #   scripts/shot_pages.py extras
                # 其余各张的版式从 v115 起没变过，不必陪着重跑。
                for nm, th, focus, h in (
                        ("chat_extras.png", "light", "all", 4200),
                        ("chat_extras_dark.png", "dark", "all", 4200),
                        ("chat_extras_decision.png", "light", "decision", 3300)):
                    got = await shot(ws, base + "/", nm, theme=th,
                                     js=_extras_js(focus), height=h, wait=2.2)
                    if not got:
                        continue
                    pr = await send(ws, "Runtime.evaluate", {
                        "expression": _EXTRAS_PROBE, "returnByValue": True})
                    v = pr.get("result", {}).get("value") or {}
                    print(f"  {nm} 验收: {v}")
                    # 全展开要求 5 块都 open；特写只要求判例卡 + 画像 2 块。
                    # 挡的是「details 没展开，图里只剩一排标题」那种假成功。
                    need_open = 5 if focus == "all" else 2
                    # v118：判例卡多了一行网络状态与「点了才联网」的按钮。
                    # 不钉住这两样，脚本会全绿、图里却还是 v117 的旧版。
                    # v140：百分比判据从「一个 % 都不许有」改成
                    # bad_percentages()——代理费 10%–20%、诉讼费 2.5% 是
                    # 法定费率，「不给胜诉率」抓的是语境不是符号。
                    bad = _bad_pct(v.get("text") or "")
                    if (v.get("secs") != 5 or v.get("opened", 0) < need_open
                            or v.get("cases", 0) < 1 or v.get("stats") != 3
                            or v.get("rows") != 6 or bad
                            or "＿＿＿＿" not in (v.get("blank") or "")
                            or v.get("net") != 1
                            or "网络" not in (v.get("netText") or "")
                            or v.get("extBox") != 1 or v.get("extBtn") != 1
                            # 只截视口高度，页高超过 h 就等于底部被切掉了。
                            # v118 内容变长后 3600 已经不够，靠这条逼着调高度，
                            # 而不是等有人对着图发现成本收益卡少了一截。
                            or v.get("docH", 0) > h + 60):
                        print(f"  [FAIL] 办案包没渲染完整，截图不算数（{bad}）")
                        return 1

            if tok_demo:
                await set_cookie(ws, tok_demo)
                # 空状态（浅色）：首屏就是它，必须单独看一眼
                await shot(ws, base + "/", "chat_empty_light.png",
                           theme="light", js="1")
                await shot(ws, base + "/", "chat_empty_dark.png",
                           theme="dark", js="1")
                # v121/v122 四个接入内核各一张首屏。这组图要证明的是
                # 「换内核 = 换一套界面」——v122 起连背景纸色都换了，
                # 所以每个内核都留一张底稿，而不是只拍默认的劳动法。
                for _pid in ("consumer", "civil", "traffic", "family"):
                    taken = await shot(ws, base + "/", f"chat_pf_{_pid}.png",
                                       theme="light",
                                       js="(async()=>{"
                                          "const b=document.querySelector("
                                          "'#pf-rail [data-pid=\"%s\"]');"
                                          "b.click();"
                                          "await new Promise(r=>setTimeout(r,700));"
                                          "})()" % _pid)
                    # shot() 被 ONLY 关键字过滤掉时页面根本没导航，断言照跑
                    # 就会在上一张图的旧状态上报假失败（这个函数开头就写着）。
                    if not taken:
                        continue
                    chk = await send(ws, "Runtime.evaluate", {
                        "expression":
                        "(()=>{const r=document.querySelector("
                        "'#pf-rail [aria-pressed=\"true\"]');"
                        "const dl=document.querySelector("
                        "'.nav-i[data-panel=\"deadline\"]');"
                        "const dc=document.querySelector("
                        "'.nav-i[data-panel=\"doc\"]');"
                        "const sl=document.querySelector("
                        "'.nav-i[data-panel=\"slots\"]');"
                        "const cs=getComputedStyle(document.documentElement);"
                        "return ['" + _pid + "',"
                        "document.documentElement.getAttribute('data-pid')||'',"
                        "r?r.getAttribute('data-pid'):'',"
                        "cs.getPropertyValue('--accent').trim(),"
                        "cs.getPropertyValue('--bg').trim(),"
                        "document.title||'',"
                        "(document.getElementById('meta')||{}).textContent||'',"
                        "document.getElementById('pf-hero')?'has':'none',"
                        "document.getElementById('hello')?'has':'none',"
                        "document.querySelectorAll('#exgrid .ex-card').length,"
                        "(()=>{const vx=parseFloat(cs.getPropertyValue('--vc-x'));"
                        "const vy=parseFloat(cs.getPropertyValue('--vc-y'));"
                        "const vr=parseFloat(cs.getPropertyValue('--vc-r'));"
                        "const W=innerWidth,H=innerHeight;"
                        # v148：判据从「圆心必须落在点过的胶囊上」改成
                        # 「这个圆盖得住整屏」。原来那条写于 v128，当时
                        # circleFrom() 还按被点的元素算圆心；v129 把它改成
                        # 固定从视口中心扩散（写死 W/2、H/2，r=√(W²+H²)+8），
                        # 这条断言从此**永远不可能通过**——于是每次全量
                        # shot_pages 都在第 20 张左右 return 1，后面三十张
                        # 根本不会重出。`chat.png` 停在 11-07 就是这个原因，
                        # 而「历史记录」的断行也因此一直没人看见（v147）。
                        # 判据只认「盖满」这个性质，不认圆心在哪。
                        "const need=Math.sqrt(Math.pow(Math.max(vx,W-vx),2)"
                        "+Math.pow(Math.max(vy,H-vy),2));"
                        "return (vr>=need-1)?'ok':(vx+','+vy+','+vr+','+need);})(),"
                        "dl?getComputedStyle(dl).display:'',"
                        "dc?getComputedStyle(dc).display:'',"
                        "sl?getComputedStyle(sl).display:''"
                        "].join('|');})()",
                        "returnByValue": True})
                    v = (chk.get("result", {}) or {}).get("value") or ""
                    parts = v.split("|")
                    # 每一段都要对：整页主题、索引条选中项、主色、纸色、
                    # 浏览器标签页、统计行必须为空、首屏卡与大字必须已经删掉、
                    # 示例卡四张、过场圆盖得住整屏、劳动专属入口收起。
                    # v148：三个侧栏入口的期望值要对齐**现在的**规矩：
                    #   · dl（时效计算）——v126 起按法域分表，非劳动内核也能进，
                    #     所以是「不是 none」。原判据写于 v126 之前，之后一直红；
                    #   · dc（仲裁申请书）——仍是劳动专属，none；
                    #   · sl（案情档案）——v138 起接入内核也有自己的要素，
                    #     **不再隐藏**，也是「不是 none」。原判据压根没判它
                    #     （收集了 parts[13] 却不看），我第一版补成 none 又红了一次
                    #     ——CSS 278 行那段注释就是规矩本身，改断言先去读它。
                    okv = (len(parts) == 14 and parts[0] == _pid
                           and parts[1] == _pid and parts[2] == _pid
                           and parts[3] not in ("", "#9e2b25")
                           and parts[4] not in ("", "#f6f1e6")
                           and parts[5].endswith("· 多领域法规问答")
                           and "劳动法" not in parts[5]
                           and parts[6] == ""
                           and parts[7] == "none" and parts[8] == "none"
                           and parts[9] == "4" and parts[10] == "ok"
                           and parts[11] not in ("", "none")
                           and parts[12] == "none"
                           and parts[13] not in ("", "none"))
                    if not okv:
                        print(f"  [FAIL] 切到 {_pid} 后界面没跟上（实测 {v}）")
                        return 1
                    # 上一条只证明了「侧栏那两项被 CSS 藏起来了」。但 Alt+3/4
                    # 和 LawNav.deadline() 都不经过侧栏——只藏 DOM 的话，
                    # 快捷键照样能把人送进错的面板。真调一次。
                    # 三个入口的规矩各不相同，必须分开问（v148 校正）：
                    #   · 时效计算（v126）非劳动内核**能进**，表换民法典三年；
                    #   · 仲裁申请书仍是劳动专属 → 挡住，并且用反向控制
                    #     （把 panelAllowed 临时改成恒真）证明挡的是它本身；
                    #   · 案情档案（v138）接入内核也有自己的要素，**能进**。
                    # 原来这三条写的是「时效能进、申请书和档案都挡住」——
                    # v138 之后档案那半条永远不成立，整套截图脚本就卡在这里。
                    # 同一次调用里做正反两问，只问正向会假绿。
                    # 必须等过场：showPanel() 走 _playPageTransition，
                    # `.view.on` 是动画结束才挪过去的。
                    g = await send(ws, "Runtime.evaluate", {
                        "expression":
                        "(async()=>{const wait=ms=>new Promise(r=>setTimeout(r,ms));"
                        "const cur=()=>{const v=document.querySelector('.view.on');"
                        "return v?v.id:'';};"
                        "LawNav.deadline();await wait(500);const opened=cur();"
                        "const s=document.getElementById('dl-dispute');"
                        "const first=s&&s.options.length?s.options[0].value:'-';"
                        "const dom=(document.getElementById('dl-domain')||{}).textContent||'';"
                        "LawNav.panel('chat');await wait(500);"
                        "LawNav.doc();await wait(500);const blocked=cur();"
                        "const saved=panelAllowed;"
                        "panelAllowed=function(){return true;};"
                        "LawNav.doc();await wait(500);const forced=cur();"
                        "panelAllowed=saved;LawNav.panel('chat');await wait(500);"
                        "LawNav.slots();await wait(500);const slotsOk=cur();"
                        "LawNav.panel('chat');await wait(500);"
                        "return opened+'|'+blocked+'|'+forced+'|'+slotsOk+'|'"
                        "+first+'|'"
                        "+(dom.indexOf('三年')>=0?'civil':'labor');})()",
                        "awaitPromise": True,
                        "returnByValue": True})
                    gv = ((g.get("result", {}) or {}).get("value") or "")
                    want = ("p-deadline|p-chat|p-doc|p-slots|"
                            "civil_general|civil")
                    if gv != want:
                        print(f"  [FAIL] {_pid} 内核下的面板边界或时效表不对"
                              f"（实测 {gv}，期望 {want}）")
                        return 1
                # 换法动画的中间帧。只查 CSS 里有那条 keyframes，证明不了
                # 圆真的在长大、真的把新帧盖在旧帧上——这两件只有中间那一帧
                # 能说明。
                #
                # 拍法：先把动画放慢到 6 秒、改成匀速（只为能拍到，不改变
                # 圆心/半径/裁剪这三样线上值），点下去 1.8 秒时截一帧。
                # 判据在下面按「半径扫描」写，不写死像素点——原版写死
                # (500,300)/(900,700) 两个点，前提是圆心在印章上，v129 改了
                # 圆心之后就恒红了（见下面那段长注释）。
                taken = await shot(ws, base + "/", "chat_pf_circle.png",
                                   theme="light", wait=1.9,
                                   js="(()=>{const s=document.createElement('style');"
                                      "s.id='vc-slow';"
                                      "s.textContent='::view-transition-new(root){"
                                      "animation-duration:6000ms!important;"
                                      "animation-timing-function:linear!important}"
                                      " .ex-card{animation:none!important;opacity:1!important}"
                                      " .di,.task-card{animation:none!important;opacity:1!important}';"
                                      "document.head.appendChild(s);"
                                      "document.querySelector('#pf-rail "
                                      "[data-pid=consumer]').click();})()")
                if taken:
                    # shot() 里固定 sleep 0.9 + 两帧 + 0.2，加起来约 1.2s，
                    # 不够 1.8s 那个 30% 的位置，所以这里自己再等一会儿重截。
                    await asyncio.sleep(0.7)
                    r = await send(ws, "Page.captureScreenshot",
                                   {"format": "png", "fromSurface": True,
                                    "captureBeyondViewport": False})
                    (OUT / "chat_pf_circle.png").write_bytes(
                        base64.b64decode(r["data"]))
                    from PIL import Image
                    from io import BytesIO
                    im = Image.open(BytesIO(base64.b64decode(r["data"]))).convert("RGB")
                    W, H = im.size
                    geo = await send(ws, "Runtime.evaluate", {
                        "expression":
                        "(()=>{const cs=getComputedStyle("
                        "document.documentElement);"
                        "return [cs.getPropertyValue('--vc-x'),"
                        "cs.getPropertyValue('--vc-y'),"
                        "cs.getPropertyValue('--vc-r')].join(',');})()",
                        "returnByValue": True})
                    gs = ((geo.get("result", {}) or {}).get("value")
                          or "").split(",")
                    # 读过来的值是 "720px" 这种**带单位**的字符串，直接
                    # float() 会炸，别忘了剥掉。剥不掉就当没读到（下面会红）。
                    def _num(t: str) -> float:
                        import re
                        m = re.match(r"\s*(-?[\d.]+)", t)
                        return float(m.group(1)) if m else -1.0

                    cx, cy, vr = (_num(gs[0]), _num(gs[1]), _num(gs[2])) \
                        if len(gs) >= 3 else (-1.0, -1.0, 0.0)
                    cx, cy = int(cx), int(cy)
                    NEW, OLD = (237, 243, 234), (246, 241, 230)

                    def near(px, ref, tol=4):
                        return all(abs(a - b) <= tol for a, b in zip(px, ref))

                    # 沿水平方向往外扫（左右哪边余量大往哪边），找**第一处旧
                    # 纸色**——那就是圆的边界。两条纸色只差 9/2/4，容差必须收到
                    # 4 以内，否则两色互相冒充（第一版 tol=8 就吃过这个亏：
                    # 「最远新纸色 718px」根本不是纸，是页面上某块同色元件）。
                    # 文字、输入框、卡片、描边环都不是纸色，扫的时候直接跳过。
                    #
                    # 只判三件：边界在半径的 2%~98%（没长起来 / 已经铺满都算红）、
                    # 边界以内确实铺着新纸色、边界以内**没有一处旧纸色**。
                    # 「圆内全是新帧」正是这个过场要证明的东西。
                    step = 2 if (W - cx) >= cx else -2
                    edge, n_new = -1, 0
                    x = cx
                    while 0 <= x < W:
                        px = im.getpixel((x, cy))
                        if near(px, OLD):
                            edge = abs(x - cx)
                            break
                        if near(px, NEW):
                            n_new += 1
                        x += step
                    frac = (edge / vr) if (vr and edge >= 0) else -1
                    print(f"  换法动画中间帧：圆心 ({cx},{cy}) r={vr:.0f} "
                          f"边界 {edge}px（占半径 {frac:.0%}）"
                          f"、界内新纸色 {n_new} 处")
                    if edge < 0 or n_new < 3 or not (0.02 <= frac <= 0.98):
                        print("  [FAIL] 换法动画不是一个从圆心长出来、盖住整屏的圆"
                              "（期望 新纸色在内、旧纸色在外、边界在半径 2%~98%）")
                        return 1
                    await send(ws, "Runtime.evaluate", {
                        "expression": "document.getElementById('vc-slow').remove()"})

                # 暗色下每个内核也必须是自己那套纸色与主色。这一条专门防
                # 「只写了浅色那套」：theme.css 的 html[data-theme="dark"]
                # 与本文件的 [data-pid] 权重相近，于是暗色会被悄悄盖回米黄
                # 朱红——不动手看一眼是发现不了的。
                taken = await shot(ws, base + "/", "chat_pf_consumer_dark.png",
                                   theme="dark",
                                   js="(async()=>{"
                                      "const b=document.querySelector("
                                      "'#pf-rail [data-pid=\"consumer\"]');"
                                      "b.click();"
                                      "await new Promise(r=>setTimeout(r,700));"
                                      "})()")
                acc = await send(ws, "Runtime.evaluate", {
                    "expression":
                    "(()=>{const cs=getComputedStyle("
                    "document.documentElement);"
                    "return cs.getPropertyValue('--accent').trim()+'|'"
                    "+cs.getPropertyValue('--bg').trim()+'|'"
                    "+cs.getPropertyValue('--panel').trim();})()",
                    "returnByValue": True})
                acc = ((acc.get("result", {}) or {}).get("value") or "").strip()
                if taken and (acc.split("|")[0] in ("", "#d8584a")
                              or acc.split("|")[1] in ("", "#16120e")):
                    print(f"  [FAIL] 暗色下消费内核没换纸色/主色（实测 {acc!r}，"
                          f"落在劳动法的暖褐+朱红上）")
                    return 1
                # 载入一条历史会话，否则刷新后 feed 是空的，截不到轮次卡片
                await shot(ws, base + "/", "chat.png", theme="light",
                           js="(async()=>{const r=await fetch('/api/chat/history');const d=await r.json();if(d.items&&d.items[0]){await loadSession(d.items[0].session_id);}window.scrollTo({top:0});await new Promise(z=>setTimeout(z,800));return 'ok';})()")
                await shot(ws, base + "/", "chat_dark.png", theme="dark",
                           js="(async()=>{const r=await fetch('/api/chat/history');const d=await r.json();if(d.items&&d.items[0]){await loadSession(d.items[0].session_id);}window.scrollTo({top:0});await new Promise(z=>setTimeout(z,800));return 'ok';})()")
                # v147：这张图从 v139 起就在，而它里面「历史记录」四个字一直是
                # 断成「历史记」/「录」的——图截了没人看，等于没截。
                # 修的时候别只看这一张：同一行在 390 下断得更明显，两处都不许
                # 折行（check_ui 6e/6f 各钉一条）。
                await shot(ws, base + "/", "chat_hist.png", theme="light",
                           js="window.LawNav.history()")
                # 设置现在是一个弹窗，「改密码」是它里面的二级弹窗。
                # 两张分开截：只看改密码的话，看不出账号/外观/快捷键都收进了设置。
                await shot(ws, base + "/", "chat_settings.png", theme="light",
                           js="window.LawNav.settings()")
                await shot(ws, base + "/", "chat_pwd.png", theme="light",
                           js="(async()=>{window.LawNav.settings();"
                              "await new Promise(r=>setTimeout(r,500));"
                              "document.getElementById('set-pwd').click();"
                              "await new Promise(r=>setTimeout(r,400));})()")
                # v139 三页。三条都要先处理首登弹窗：这个脚本每次用全新账号，
                # 「完善个人档案」一定弹，不关掉的话截到的是弹窗盖在页面上，
                # 看不出底下有什么。关掉后每张都自己重新打开对应页面。
                # 图书馆要等目录与原文都渲染出来（fetch 后一次性 innerHTML，
                # 早了就是空壳）；学习页要真的出一次题，否则截到的是空表单。
                _dismiss = ("var _sk=document.getElementById('onb-skip');"
                            "if(_sk&&_sk.offsetParent!==null){_sk.click();"
                            "await new Promise(r=>setTimeout(r,500));}")
                await shot(ws, base + "/", "chat_library.png", theme="light",
                           js="(async()=>{" + _dismiss +
                              "window.LawNav.library();"
                              "await new Promise(r=>setTimeout(r,900));"
                              "var a=document.querySelector('#lib-toc [data-aid]');"
                              "if(a){a.click();await new Promise(r=>setTimeout(r,600));}"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                # v142：出卷前的设置区（题数 / 题型配比）单独留一张。用户报的
                # 「自定义页数上有乱码」就是这一行——五个控件挤在 218px 里，
                # 输入框内边距被迫写 0、字贴着边框。chat_study 截的是卷子本身，
                # 看不到设置区。
                await shot(ws, base + "/", "chat_study_setup.png", theme="light",
                           js="(async()=>{" + _dismiss +
                              "window.LawNav.study();"
                              "await new Promise(r=>setTimeout(r,800));"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                # v140：学习页改成「一张卷」。点「生成试卷」后轮询到题卡出现；
                # 但卷子是整张画出来的，不像旧版一题一换，所以这里只要题卡
                # 就位就能拍——不必等 HUD 计数，那要用户先答题。
                await shot(ws, base + "/", "chat_study.png", theme="light",
                           js="(async()=>{" + _dismiss +
                              "window.LawNav.study();"
                              "await new Promise(r=>setTimeout(r,700));"
                              "document.getElementById('st-new').click();"
                              # 出题可能要几秒（有 key 时题面由模型写），
                              # 固定 sleep 2s 会截到「正在生成题目…」——
                              # 看着像功能卡住了。这里轮询到题目真的出现为止。
                              "for(var i=0;i<40&&!document.querySelector("
                              "'#st-questions .q-card');i++){"
                              "await new Promise(r=>setTimeout(r,500));}"
                              "await new Promise(r=>setTimeout(r,400));"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                # 个人主页要有内容才看得出「知识树 / 图表 / 日历」是什么样：先在
                # 这一张里自己出卷、答一题（点选项 + 提交本题），再切过去。
                # **不能接着上一张的 DOM**：shot() 每次都会重新导航到 base，
                # 上一张的题目到这一张已经没了，接着取 .q-card 只会取到空——
                # 截出来的主页是「0 题」，看着像进度没记，其实什么都没答。
                await shot(ws, base + "/", "chat_me.png", theme="light",
                           js="(async()=>{"
                              "window.LawNav.study();"
                              "await new Promise(r=>setTimeout(r,600));"
                              "document.getElementById('st-new').click();"
                              "for(var i=0;i<40&&!document.querySelector("
                              "'#st-questions .q-card');i++){"
                              "await new Promise(r=>setTimeout(r,500));}"
                              "var n=0;"
                              # v140 一次给一整张卷，这里连答三题，图表和
                              # 知识树才有数据可画（只答一题，树只有一根枝）。
                              "var cs=document.querySelectorAll("
                              "'#st-questions .q-card');"
                              "for(var k=0;k<cs.length&&k<3;k++){"
                              "var o=cs[k].querySelector('[data-opt]');"
                              "if(!o) continue; o.click();"
                              "await new Promise(r=>setTimeout(r,300));"
                              "var sb=cs[k].querySelector('[data-one-submit]');"
                              "if(sb){sb.click();"
                              "await new Promise(r=>setTimeout(r,1200));n++;}"
                              "}"
                              "window.LawNav.me();"
                              "await new Promise(r=>setTimeout(r,1500));"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));"
                              "return n;})()")
                # v141：主页下半部也要有人看——chat_me 只截到首屏图表，
                # 日历/知识树/徽章全在两屏之外，用户报的「乱码」恰恰在
                # 那里。v142 起这两块各自是一个子页，取景前先点开标题，
                # 否则截到的是「概览」那张图。
                # v142：用户报的两处「乱码」各留一张证据图——进度卡那五张
                # （按钮 123px 被 overflow:hidden 切、读数腰斩）和出卷题数
                # 那一行（218px 里五个控件被 flex:1 1 0 压成 39px）。
                # 这两张只做证据，不参与回归断言；断言在 check_ui 里。
                # v145：徽章子页改成默认摊开，这张就是「点进徽章该看到什么」
                # 的验收图——改之前同一屏只有一行标题 + 一颗按钮。
                # **必须是独立的 shot()，不能接着 chat_me 的页面 _capture**：
                # 上面那张被 ONLY 过滤掉时页面根本没导航，这里就会拍到空页
                # （实测 2 KB 的一张白图）。
                await shot(ws, base + "/", "me_badge.png", theme="light",
                           wait=1.9,
                           js="(async()=>{window.LawNav.me();"
                              "await new Promise(r=>setTimeout(r,2000));"
                              "meTab('badge');"
                              "await new Promise(r=>setTimeout(r,700));"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                await send(ws, "Runtime.evaluate", {"expression":
                    "meTab('progress')"})
                await asyncio.sleep(0.8)
                await send(ws, "Runtime.evaluate", {"expression":
                    "document.getElementById('me-progress')"
                    ".scrollIntoView({behavior:'instant',block:'center'})"})
                await _capture(ws, "chat_me_progress.png")
                await send(ws, "Runtime.evaluate", {"expression":
                    "meTab('tree')"})
                await asyncio.sleep(0.9)
                await send(ws, "Runtime.evaluate", {"expression":
                    "document.getElementById('me-tree')"
                    ".scrollIntoView({behavior:'instant',block:'start'})"})
                await _capture(ws, "chat_me_tree.png")
                await send(ws, "Runtime.evaluate", {"expression":
                    "meTab('cal')"})
                await asyncio.sleep(0.7)
                await send(ws, "Runtime.evaluate", {"expression":
                    "document.getElementById('me-heat')"
                    ".scrollIntoView({behavior:'instant',block:'center'})"})
                await _capture(ws, "chat_me_cal.png")
                # v142 窄屏：≤620px 子页条从「换行」变成「横滑 + 藏滚动条」，
                # 390px 下「徽章 / 打卡」整颗在屏外（条子可视宽 260、内容 434）。
                # 这张专门留证据：点「打卡」之后它必须自己滚进视野、右缘渐隐
                # 摘掉。断言在 check_ui（那里临时压视口，测完恢复 1440）。
                await shot(ws, base + "/", "narrow_me.png",
                           theme="light", width=390, height=844,
                           js="(async()=>{"
                              "try{localStorage.removeItem('law_me_tab')}"
                              "catch(e){}"
                              "var _sk=document.getElementById('onb-skip');"
                              "if(_sk&&_sk.offsetParent!==null){_sk.click();"
                              "await new Promise(r=>setTimeout(r,500));}"
                              "window.LawNav.me();"
                              "await new Promise(r=>setTimeout(r,2200));"
                              "meTab('cal');"
                              "await new Promise(r=>setTimeout(r,700));"
                              "window.scrollTo({top:0});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                # v145 窄屏四页各留一张：概览（四宫格仍 2×2）、打卡
                # （「…打卡 2 天」不再被拆成「2」/「天」两行）、知识树
                # （保底 600 宽 + 卡内横滑，章名从 4.3px 回到 10.3px）、
                # 徽章（默认摊开，390 下是两列六边形）。
                # 每张都走 shot()（自带 390 视口 + 重新导航）：能按名字
                # 单独重出，不受「窄屏那张有没有被 ONLY 过滤掉」牵连。
                # 代价是多导航三次，几秒钟。
                for _tab, _nm in (("overview", "me_overview_390.png"),
                                  ("cal", "me_cal_390.png"),
                                  ("tree", "me_tree_390.png"),
                                  ("badge", "me_badge_390.png")):
                    await shot(ws, base + "/", _nm, theme="light",
                               width=390, height=844, wait=1.9,
                               js="(async()=>{"
                                  "try{localStorage.removeItem('law_me_tab')}"
                                  "catch(e){}"
                                  "var _sk=document.getElementById('onb-skip');"
                                  "if(_sk&&_sk.offsetParent!==null){_sk.click();"
                                  "await new Promise(r=>setTimeout(r,500));}"
                                  "window.LawNav.me();"
                                  "await new Promise(r=>setTimeout(r,2200));"
                                  f"meTab('{_tab}');"
                                  "await new Promise(r=>setTimeout(r,800));"
                                  "window.scrollTo({top:0});"
                                  "await new Promise(r=>setTimeout(r,300));})()")
                # v146 窄屏另外两处（都是这轮实测出来的）：
                #  · 首屏底栏原来把输入框挤成 59px 宽的一根柱子（正文 15px，
                #    一个字一行），现在两行：输入框独占第一行；
                #  · 历史记录的标题被搜索框挤成两行，现在上下两行。
                await shot(ws, base + "/", "page_landing_390.png",
                           theme="light", width=390, height=844, wait=1.9)
                await shot(ws, base + "/", "page_history_390.png",
                           theme="light", width=390, height=844, wait=1.9,
                           js="window.LawNav.history()")
                # 后台是独立页面（/admin），要换管理员令牌、换 URL。
                await set_cookie(ws, tok_admin)
                await shot(ws, base + "/admin", "page_admin_390.png",
                           theme="light", width=390, height=844, wait=1.9)
                await set_cookie(ws, tok_demo)
                # 时效计算：先打开面板再真的算一次，否则截到的是空表单。
                # 这张图要能看见日历本身，所以挑一个**有截止日**的场景：
                # 已离职（起算＝终止之日），起算日取今天往前 90 天。于是本月
                # 既有剩余区间的金色，也有一条「截止日在别的月」的跳转。
                # 用「不确定」+ 讨薪算出来的是「取决于你是否还在职」，图上
                # 没有截止日——那种截图看起来像日历根本没做。
                await shot(ws, base + "/", "chat_deadline.png", theme="light",
                           js="(async()=>{window.LawNav.deadline();"
                              "await new Promise(r=>setTimeout(r,400));"
                              "var s=document.getElementById('dl-dispute');"
                              "s.value='wage';s.dispatchEvent(new Event('change'));"
                              "var t=new Date();t.setDate(t.getDate()-90);"
                              "var iso=_iso(t);"
                              "document.getElementById('dl-onset').value=iso;"
                              "[...document.querySelectorAll('#dl-rel button')]"
                              ".find(b=>b.dataset.v==='yes').click();"
                              "document.getElementById('dl-end').value=iso;"
                              "await new Promise(r=>setTimeout(r,200));"
                              "document.getElementById('dl-go').click();"
                              "await new Promise(r=>setTimeout(r,1200));"
                              "document.getElementById('dl-cal')"
                              ".scrollIntoView({block:'center'});"
                              "await new Promise(r=>setTimeout(r,300));})()")
                await shot(ws, base + "/", "chat_docpanel.png", theme="light",
                           js="window.LawNav.doc()")
                # 图片证据（v115）：读图 → 逐项确认。
                # js 用字符串拼而不是 f-string：要把整张工资条的 base64
                # 塞进去，f-string 里的花括号会和 JS 对象字面量打架。
                _ev_js = (
                    "(async()=>{window.LawNav.panel('chat');"
                    "await new Promise(r=>setTimeout(r,700));"
                    "document.getElementById('ev-open').click();"
                    "await new Promise(r=>setTimeout(r,350));"
                    "const b64='" + _payslip_b64() + "';"
                    "const bin=atob(b64);"
                    "const u8=new Uint8Array(bin.length);"
                    "for(let i=0;i<bin.length;i++)u8[i]=bin.charCodeAt(i);"
                    "readEvidence(new File([u8],'payslip.png',"
                    "{type:'image/png'}));"
                    "await new Promise(r=>setTimeout(r,1900));})()")
                await shot(ws, base + "/", "chat_evidence.png", theme="light",
                           js=_ev_js)
                await shot(ws, base + "/", "chat_help.png", theme="dark",
                           js="document.querySelector('[data-help-btn]').click()")
                # 手机竖屏：按键尺寸和底部输入区最容易在小屏上塌
                await shot(ws, base + "/", "chat_mobile.png", theme="light",
                           width=390, height=844, wait=1.6)
                # 手机竖屏：按键尺寸和底部输入区最容易在小屏上塌
                await shot(ws, base + "/", "chat_mobile.png", theme="light",
                           width=390, height=844, wait=1.6)
                # 窄屏这里有两套「法」胶囊，性质完全不同，必须分开量（v148）：
                #   · 首屏的大胶囊会**换行**（390 下排成 2+2+1），五枚全在容器
                #     里，压根不需要滚；
                #   · 有对话之后顶部那条索引条是 nowrap 的横滑条，5 枚 434px
                #     塞不进 390，选中项必须被 scrollIntoView 送进视野。
                # 原来那条只测第二种，而且是在 `switchProfile('family')` 之后
                # 立刻量——可 v139 起换法一律**开新咨询**，新咨询是空对话，
                # `body.blank` 会把索引条整个收起，量出来 0×0；而「选中项在
                # 容器内」用的 0>=−1 这种退化比较恒真。于是这条断言看着一直在
                # 跑，其实从 v139 起就什么都没测到。
                taken = await shot(ws, base + "/", "chat_mobile_family.png",
                                   theme="light", width=390, height=844,
                                   wait=1.6,
                                   js="(async()=>{switchProfile('family');"
                                      "await new Promise(r=>setTimeout(r,900));})()")
                if taken:
                    vis = await send(ws, "Runtime.evaluate", {
                        "expression":
                        "(async()=>{const out={};"
                        "const box=document.getElementById('land-pills');"
                        "const ps=[...document.querySelectorAll("
                        "'#land-pills [data-pid]')];"
                        "const br=box?box.getBoundingClientRect():null;"
                        "out.land=(ps.length===5&&br&&ps.every(p=>{"
                        "const b=p.getBoundingClientRect();"
                        "return b.width>10&&b.left>=br.left-1"
                        "&&b.right<=br.right+1;}))?'ok':'bad';"
                        "out.over=Math.max(0,"
                        "document.documentElement.scrollWidth-innerWidth);"
                        "const r=await fetch('/api/chat/history');"
                        "const d=await r.json();"
                        "out.has=!!(d.items&&d.items[0]);"
                        "if(out.has){"
                        "await loadSession(d.items[0].session_id);}"
                        "await new Promise(z=>setTimeout(z,1300));"
                        "const rail=document.getElementById('pf-rail');"
                        "const a=rail?rail.getBoundingClientRect():null;"
                        "const it=rail?rail.querySelector("
                        "'[aria-pressed=\"true\"]'):null;"
                        "const b2=it?it.getBoundingClientRect():null;"
                        "out.railW=a?Math.round(a.width):-1;"
                        "out.sw=rail?rail.scrollWidth:-1;"
                        "out.cw=rail?rail.clientWidth:-1;"
                        "out.sel=it?(it.getAttribute('data-pid')||''):'';"
                        "out.selIn=!!(a&&b2&&b2.width>10"
                        "&&b2.left>=a.left-1&&b2.right<=a.right+1);"
                        "return JSON.stringify(out);})()",
                        "awaitPromise": True,
                        "returnByValue": True})
                    vv = ((vis.get("result", {}) or {}).get("value") or "")
                    try:
                        j = json.loads(vv) if vv else {}
                    except ValueError:
                        j = {}
                    rail_ok = ((j.get("railW") or 0) > 100
                               and (j.get("sw") or 0) > (j.get("cw") or 0)
                               and j.get("selIn") is True)
                    if not (j.get("land") == "ok" and j.get("over") == 0):
                        print(f"  [FAIL] 窄屏首屏五枚胶囊没全在容器里（实测 {vv}）")
                        return 1
                    if j.get("has") and not rail_ok:
                        print(f"  [FAIL] 有对话时索引条没把选中项送进视野"
                              f"（实测 {vv}）")
                        return 1
                    if not j.get("has"):
                        # 库里一条会话都没有：索引条此时本来就不该出现
                        # （body.blank 收起），这半条无从量起。说清楚，
                        # 别让「跳过」伪装成「通过」。
                        print("  （库里没有历史会话：索引条那半条跳过，"
                              "首屏五枚已验）")

            # ---- 任务面板与当前任务条（临时库，内容是自己种的两件案子）----
            await set_cookie(ws, tok_task)
            taken = await shot(ws, base_task + "/", "chat_tasks.png",
                               theme="light",
                               js="(async()=>{window.LawNav.panel('tasks');"
                                  "await new Promise(r=>setTimeout(r,1500));})()")
            if taken:
                cnt = await send(ws, "Runtime.evaluate", {"expression":
                    "(()=>{const cards=[...document.querySelectorAll"
                    "('#task-list .task-card')];"
                    "const arch=[...document.querySelectorAll('.task-st')]"
                    ".some(x=>x.textContent.indexOf('已归档')>=0);"
                    "const hist=cards.some(c=>c.textContent"
                    ".indexOf('历史咨询')>=0);"
                    "const laws=cards.map(c=>(c.getAttribute('data-pid')||'-')"
                    "+':'+((c.querySelector('.task-law')||{}).textContent||'')"
                    ").join(',');"
                    "return cards.length+'/'+(arch?'有归档':'无归档')"
                    "+'/'+(hist?'含历史咨询':'无历史咨询')+'/'+laws;})()"})
                v = cnt.get("result", {}).get("value") or ""
                print(f"  任务卡片：{v}")
                # 种子是「1 件办理中 + 1 件已结案 + 1 条不属于任何任务的老会话」。
                # 面板默认不列归档的，但那条老会话会被 `GET /api/tasks` 里的
                # `assign_loose_to_task()` 收编成「历史咨询」任务——所以正确结果
                # 是 **2 张**：自己种的 1 件 + 自动收编的 1 件。
                #
                # 这里原来写的是「应当是 1 张」。种子后来加了那条老会话（文书下拉
                # 的单会话分支要用），断言没跟着改，于是每跑一次都红，还被当成
                # 「真实库里恰好有 2 件任务」。**这是断言过期，不是库脏**——
                # 用临时种子库跑也一样是 2 张，可以复现。
                # 第三段是每张卡的法域：卡片必须挂着自己的 data-pid，
                # 并且徽章文字和它一致。光有颜色不算——颜色对了、徽章还写着
                # 「劳动用工」的食安案子，比不标更误导。
                laws = v.split("/")[-1] if "/" in v else ""
                for pair in [x for x in laws.split(",") if x]:
                    pid, _, badge = pair.partition(":")
                    if pid == "-" or not badge:
                        print(f"  [FAIL] 任务卡没有法域徽章（实测 {v}）")
                        return 1
                if "consumer" not in laws or "labor" not in laws:
                    print(f"  [FAIL] 任务卡没按各自内核上色（实测 {v}，"
                          f"应同时出现 labor 与 consumer）")
                    return 1
                if (not v.startswith("2/") or "无归档" not in v
                        or "含历史咨询" not in v):
                    print("  [FAIL] 任务面板应列 2 件办理中（1 件种子 + 1 件"
                          "自动收编的「历史咨询」），且不显示已归档")
                    return 1
            # 进入列表第一个任务 → 顶部「当前任务」条 + 该任务的会话
            taken = await shot(ws, base_task + "/", "chat_taskbar.png",
                               theme="light", wait=2.2,
                               js="(async()=>{"
                                  "const r=await fetch('/api/tasks');"
                                  "const d=await r.json();"
                                  "await enterTask(d.items[0]);"
                                  "await new Promise(z=>setTimeout(z,1200));})()")
            if taken:
                bar = await send(ws, "Runtime.evaluate", {"expression":
                    "(()=>{const b=document.getElementById('taskbar');"
                    "const nm=b && !b.hidden ? "
                    "document.getElementById('tb-name').textContent : 'hidden';"
                    "const dl=document.querySelector('.nav-i[data-panel=\"deadline\"]');"
                    "return nm+'|'+(b?b.getAttribute('data-pid'):'')+'|'"
                    "+(document.body.getAttribute('data-profile')||'')+'|'"
                    "+(dl?getComputedStyle(dl).display:'-');})()"})
                nm = bar.get("result", {}).get("value") or ""
                print(f"  当前任务条：{nm}")
                if not nm or nm.startswith("hidden"):
                    print("  [FAIL] 进入任务后顶部任务条没出现")
                    return 1
                # 进入的是消费内核的「历史咨询」任务，所以整页要跟着换到
                # 那个法域：任务条挂 consumer，body[data-profile] 也是
                # consumer。v126：时效计算已按法域分表，入口不再收起——
                # 收起反而会让人拿着民法典三年的纠纷去用劳动仲裁一年。
                p_nm, p_pid, p_body, p_dl = (nm.split("|") + ["", "", ""])[:4]
                if "consumer" not in (p_pid + p_body) or p_dl == "none":
                    print(f"  [FAIL] 进入任务没跟着它的法域走（实测 {nm}，"
                          f"期望 data-pid/data-profile 为 consumer、"
                          f"时效计算入口可见）")
                    return 1

            # 文书下拉：归档的案子也要在列表里并标「已结案」。
            # 归档 = 结案，不是删掉；结案之后才是真要出申请书的时候。
            taken = await shot(ws, base_task + "/", "chat_doctasks.png",
                               theme="light",
                               js="(async()=>{window.LawNav.panel('doc');"
                                  "await new Promise(r=>setTimeout(r,1500));})()")
            if taken:
                opts = await send(ws, "Runtime.evaluate", {"expression":
                    "(()=>{const s=document.getElementById('doc-task');"
                    "return [...s.options].map(o=>o.textContent).join(' | ');})()"})
                txt = opts.get("result", {}).get("value") or ""
                print(f"  文书下拉：{txt}")
                if "已结案" not in txt:
                    print("  [FAIL] 归档任务没出现在文书下拉里")
                    return 1

            # 生成后的申请书弹窗。这里要验的是「来源收据」那一行——生成前
            # 预告「将使用 N 次咨询」，生成后必须能对账，否则漏了一整段咨询
            # 也看不出来。挑第 2 个任务：它有两场咨询，收据上的数字才有区分度。
            # 生成前的申请书弹窗。这里要验的是「承诺 = 账单」：
            # 面板上那行「将使用：N 项要素 · M 次咨询 · K 轮对话」是用户点下去
            # **之前**看到的唯一依据，生成后的收据是账单。两者必须同一组数。
            # 而且要按用户的真实走法走：进文书面板 → 在下拉里选中任务（触发
            # 预告请求）→ 点生成。直接调 makeDoc 会跳过预告这一整层。
            # 挑第 2 个任务：它有两场咨询，数字才有区分度（全是 1 的话，
            # 抄错也看不出来）。
            taken = await shot(ws, base_task + "/", "chat_docmodal.png",
                               theme="light", wait=2.4,
                               js="(async()=>{"
                                  "const r=await fetch('/api/tasks');"
                                  "const d=await r.json();"
                                  "const t=d.items[1]||d.items[0];"
                                  "window.__docT={s:t.n_sessions||0,"
                                  "r:t.n_turns||0,tid:t.task_id};"
                                  "window.LawNav.panel('doc');"
                                  "await new Promise(z=>setTimeout(z,1600));"
                                  "const sel=document.getElementById('doc-task');"
                                  "sel.value=t.task_id;"
                                  "sel.dispatchEvent(new Event('change'));"
                                  "await new Promise(z=>setTimeout(z,1000));"
                                  "window.__plan="
                                  "(document.getElementById('doc-sum')||{})"
                                  ".textContent||'';"
                                  "document.getElementById('doc-go').click();"
                                  "await new Promise(z=>setTimeout(z,1600));})()")
            if taken:
                rec = await send(ws, "Runtime.evaluate", {"returnByValue": True,
                    "expression":
                    "(()=>{const s=document.getElementById('d-src');"
                    "return {txt:(s&&!s.hidden)?s.textContent:'hidden',"
                    "plan:window.__plan||'',"
                    "t:window.__docT||{}};})()"})
                got = rec.get("result", {}).get("value") or {}
                txt = got.get("txt")
                plan = got.get("plan") or ""
                t = got.get("t") or {}
                print(f"  生成前预告：{plan}")
                print(f"  来源收据：{txt}")
                if not plan:
                    print("  [FAIL] 文书面板没有显示「将使用」预告")
                    return 1
                # 「0 项要素」不算错（种子真没写），但它是那种「数字对、图假」
                # 的状态：断言全绿，图上却是一个真实用户看不到的样子。
                # 种子必须像真案子。
                if "0 项案情要素" in plan:
                    print("  [FAIL] 预告显示 0 项要素——种子数据不像真案子")
                    return 1
                if not txt or txt == "hidden" or "次咨询" not in txt:
                    print("  [FAIL] 申请书弹窗没显示来源收据")
                    return 1
                # 收据上的数字必须等于**那个任务自己的**数字。
                # 原来只断言「含『次咨询』」，写死一个 2 也能过；而这条收据的
                # 全部意义就是让人核对范围——数字对不上，它就是一句漂亮话。
                # 这里比的是三条不同代码路径：下拉那行读 /api/tasks 的聚合，
                # 预告读 /api/tasks/{id}，收据读生成时服务端现算的 source。
                want = (f"{t.get('s')} 次咨询", f"{t.get('r')} 轮对话")
                print(f"  任务自称：{want[0]} · {want[1]}")
                for w in want:
                    if w not in txt:
                        print(f"  [FAIL] 收据里的数字和任务对不上，缺「{w}」")
                        return 1
                    if w not in plan:
                        print(f"  [FAIL] 预告里的数字和任务对不上，缺「{w}」")
                        return 1
                # 关掉弹窗，别让遮罩影响后面的后台截图
                await send(ws, "Runtime.evaluate", {"expression":
                    "document.getElementById('d-close').click()"})
                await asyncio.sleep(0.4)

            # 老会话（不属于任何任务）也要能出申请书，而且收据上的**范围**
            # 必须说对。原来那句「整个任务范围内按时间摘录」是写死的，单会话
            # 模式也照贴——最窄的范围被说成最宽的，收据就从「能核对」
            # 变成了「假话」。这张图是那个分支唯一的证据。
            #
            # 这里必须**当场再种一条**未归属会话。上面几步（任务面板、
            # 当前任务条、任务文书）都调过 /api/tasks，而那个接口第一件事就是
            # `assign_loose_to_task()`——把所有 task_id 为空的会话收编进
            # 「历史咨询」，并且固定返回 `unassigned: []`。于是种子里原先那条
            # shot_loose 早就被收走了，按 `d.unassigned[0]` 取会话的写法永远
            # 取不到，每次都报「种子库里没有未归属任务的会话」——看着像种子
            # 写错，其实是**步骤顺序**问题：要验未分类，得在种完之后、
            # 第一次 /api/tasks 之前用掉它。
            #
            # 所以改成按已知 id 直接调用，不问接口「谁还没归属」（那个字段
            # 必然为空）。`loadSession()` 对 task_id 为空的会话不会再请求任务
            # 接口，两步之间它仍是未归属的——收据说「它不属于任何任务」
            # 才是实话，而不是为了配色编出来的一句。
            from app.memory import MemoryStore as _MS      # noqa: PLC0415

            loose2 = "shot_loose2"
            _ms = _MS(tmpdb)
            _ms.ensure_session(loose2, shot_uid)           # 不给 task_id
            _ms.save_message(loose2, "user", "公司从去年 10 月开始就没发工资了")
            _ms.save_message(loose2, "assistant", "（演示用回答）")
            _ms.close()

            taken = await shot(ws, base_task + "/", "chat_docsession.png",
                               theme="light", wait=2.2,
                               js="(async()=>{"
                                  "const sid='" + loose2 + "';"
                                  "window.__loose=sid;"
                                  "await loadSession(sid);"
                                  "await new Promise(z=>setTimeout(z,700));"
                                  "await makeDoc(null,sid,null);"
                                  "await new Promise(z=>setTimeout(z,1200));})()")
            if taken:
                r = await send(ws, "Runtime.evaluate", {"returnByValue": True,
                    "expression":
                    "(()=>{const s=document.getElementById('d-src');"
                    "return {sid:window.__loose||'',"
                    "text:(s&&!s.hidden)?s.textContent.trim():'hidden'};})()"})
                v = r.get("result", {}).get("value") or {}
                print(f"  未分类会话收据：{v.get('text')}")
                if not v.get("sid"):
                    print("  [FAIL] 未归属会话没打开（loadSession 失败）")
                    return 1
                if (v.get("text") or "") == "hidden":
                    print("  [FAIL] 生成后没出现「本份取自」收据")
                    return 1
                if "整个任务范围" in (v.get("text") or ""):
                    print("  [FAIL] 单会话模式的收据写成了「整个任务范围」")
                    return 1
                if "只有这一次咨询" not in (v.get("text") or ""):
                    print("  [FAIL] 收据没说清只用了这一次咨询")
                    return 1
                await send(ws, "Runtime.evaluate", {"expression":
                    "document.getElementById('d-close').click()"})
                await asyncio.sleep(0.3)

            await set_cookie(ws, tok_admin)
            # 后台会记住上次看的面板（localStorage）。截图必须显式指定视图，
            # 否则截到哪一块取决于脚本上一次点了什么。
            await shot(ws, base + "/admin", "admin_overview.png", wait=2.6,
                       theme="light",
                       js="document.querySelector('[data-view=overview]').click()")
            await shot(ws, base + "/admin", "admin_overview_dark.png", wait=2.4,
                       theme="dark",
                       js="document.querySelector('[data-view=overview]').click()")
            await shot(ws, base + "/admin", "admin_users.png", wait=2.2,
                       theme="light",
                       js="document.querySelector('[data-view=users]').click()")
            await shot(ws, base + "/admin", "admin_sessions.png", wait=2.2,
                       theme="dark",
                       js="document.querySelector('[data-view=sessions]').click()")
            # 档案只读弹窗。这一步最容易出「看起来像没填」的假象：
            # 详情接口包了一层 {"user": {...}}，路径写错时整屏都是「（未填）」，
            # 截图很漂亮但是错的。所以下面显式断言网格真的渲染出来了——
            # 只看有没有网格不看已填数量：admin 自己的档案可能本来就是空的，
            # 拿数量当断言会把「数据空」误判成「代码坏」。
            taken = await shot(ws, base + "/admin", "admin_profile.png",
                               wait=2.2, theme="light",
                               js="(async()=>{"
                                  "document.querySelector('[data-view=users]').click();"
                                  "await new Promise(r=>setTimeout(r,1600));"
                                  "const b=[...document.querySelectorAll('button')]"
                                  ".find(x=>x.textContent.trim()==='档案');"
                                  "if(b){b.click();}"
                                  "await new Promise(r=>setTimeout(r,1200));})()")
            if taken:
                ok = await send(ws, "Runtime.evaluate", {"expression":
                    "(()=>{const g=document.querySelector('.pf-grid');"
                    "const m=document.querySelector('.pf-meta');"
                    "if(!g||!m) return 'no-grid';"
                    "const filled=[...g.querySelectorAll('.pf-v')]"
                    ".filter(v=>!v.classList.contains('miss')).length;"
                    "return filled+'/'+g.querySelectorAll('.pf-v').length;})()"})
                v = ok.get("result", {}).get("value")
                print(f"  档案弹窗已填/总字段：{v}")
                if not isinstance(v, str) or v == "no-grid":
                    print("  [FAIL] 后台档案弹窗没渲染出来（多半是取值路径错了）")
                    return 1

            # v123：空对话里直接问一句，顶部应当自己长出一根任务条——
            # 「任务自动生成」这件事在界面上只有这一个可见证据。
            #
            # 三个约束，缺一个这张图就不可信：
            #   · 必须在任务面板那几张**之后**——它会真的建一个任务，放前面
            #     会让「面板应列 2 件」的断言多出一张卡；
            #   · 不能用演示库——会在交付库里留下一个测试任务；
            #   · 不能带真 key——一次调用几十秒，截图脚本也不该花钱。
            # 所以另外起一个空库 + 回放模式的服务，用完就关。
            svc_auto, base_auto = start_service(
                env={"LAW_DB_PATH": str(tmpdir / "auto.db"),
                     "DEEPSEEK_API_KEY": ""})
            try:
                # 这是**另一个库**，手上那几张令牌在这儿都不存在——不换 cookie
                # 的话页面直接被踢去登录页，截图里连任务条都找不到，
                # 看起来像「任务没生成」。第一次跑就是这么假红了一次。
                _a_store = AuthStore(tmpdir / "auto.db")
                _adm = next((u for u in _a_store.list_users(size=10)["items"]
                             if u["role"] == "admin"), None)
                if not _adm:
                    print("  [FAIL] 自动任务用的临时库里没有管理员，起不来")
                    return 1
                await set_cookie(ws, _a_store.issue_token(_adm["id"], ua="shot"))
                taken = await shot(ws, base_auto + "/", "chat_autotask.png",
                                   wait=2.4, theme="light",
                                   js="(async()=>{"
                                      # 新库的 admin 是首次登录，会先弹「完善个人档案」。
                                      # 不关掉的话它正好盖在任务条上——图里看不到证据，
                                      # 断言却还是绿的（DOM 在遮罩底下照样读得到）。
                                      "const skip=[...document.querySelectorAll('button')]"
                                      ".find(b=>b.textContent.indexOf('先跳过')>=0);"
                                      "if(skip){skip.click();"
                                      "await new Promise(r=>setTimeout(r,500));}"
                                      "document.getElementById('q').value="
                                      "'公司拖欠工资三个月了怎么办';"
                                      "await send();"
                                      "await new Promise(r=>setTimeout(r,900));"
                                      "window.scrollTo({top:0});"
                                      "})()")
                if taken:
                    r = await send(ws, "Runtime.evaluate", {
                        "returnByValue": True, "expression":
                        "(()=>{const b=document.getElementById('taskbar');"
                        "const lb=document.getElementById('tb-law');"
                        "const nm=document.getElementById('tb-name');"
                        "return (b&&!b.hidden?'shown':'hidden')+'|'"
                        "+((nm&&nm.textContent)||'')+'|'"
                        "+((lb&&lb.textContent)||'')+'|'"
                        "+((b&&b.getAttribute('data-pid'))||'')+'|'"
                        "+((typeof currentTask==='string'&&currentTask)||'')"
                        "+'|'+((typeof sessionId==='string'&&sessionId)||'');})()"})
                    v = r.get("result", {}).get("value") or ""
                    print(f"  自动任务条：{v}")
                    parts = v.split("|")
                    if len(parts) != 6 or parts[0] != "shown":
                        print("  [FAIL] 空对话里问一句后，顶部任务条没出现"
                              "（任务没自动生成）")
                        return 1
                    if not parts[1].startswith("公司拖欠工资三个月了"):
                        print(f"  [FAIL] 自动任务没拿提问当标题（实测 {parts[1]!r}）")
                        return 1
                    # 徽章文字是「印章字 + 内核名」，劳动法是「劳」+「劳动法」，
                    # 所以这里断「含劳动」而不是相等——别把印章字算成重复。
                    if "劳动" not in parts[2] or parts[3] != "labor":
                        print(f"  [FAIL] 自动任务没标法域（实测 {parts[2]!r} /"
                              f" {parts[3]!r}）")
                        return 1
                    if not parts[4] or not parts[5]:
                        print("  [FAIL] 前端没接住服务端回的任务（currentTask /"
                              " sessionId 为空），下一句追问会再建一个任务")
                        return 1
                    # 库里也得真的只有这一个任务，而且会话挂在它下面。
                    # 只看界面的话，「前端自己编了一个 task_id」也能骗过去。
                    r2 = await send(ws, "Runtime.evaluate", {
                        "returnByValue": True, "awaitPromise": True, "expression":
                        "fetch('/api/tasks').then(r=>r.json()).then(d=>"
                        "(d.items||[]).length+'|'+"
                        "(d.items&&d.items[0]?d.items[0].task_id:'')+'|'+"
                        "(d.items&&d.items[0]?(d.items[0].n_sessions||0):0))"})
                    v2 = ((r2.get("result", {}) or {}).get("value") or "")
                    print(f"  库里的任务：{v2}")
                    p2 = v2.split("|")
                    if len(p2) != 3 or p2[0] != "1" or p2[1] != parts[4] \
                            or p2[2] != "1":
                        print(f"  [FAIL] 库里应当恰好 1 个任务、挂 1 个会话、"
                              f"和界面同一个 task_id（实测 {v2}）")
                        return 1

                # ----------------------------------------------------------
                # 用户报的那个场景的第二步：**换法之后再问一句**。
                # v123 之前坏就坏在这里——`startBlank()` 只清了 sessionId，
                # `currentTask` 还在，新法的会话就落进了旧法的任务。
                # 只验第一句的话，这个洞一个断言都碰不到。
                # 不重新加载页面：重新加载等于换了一条咨询链，就测不出「换法时
                # 旧任务还在手上」这个真实状态——而那正是出 bug 的那个状态。
                # 当前页仍是上一步留下的劳动法会话（主题已是 light），接着做。
                # **必须放在 `if taken` 里面**：ONLY 过滤时上一步根本没导航到
                # auto 库，而 cookie 已经换成 auto 库的令牌——此时随便发一个
                # fetch 就会被踢去登录页，紧接着的 evaluate 报
                # 「Inspected target navigated or closed」。看着像功能坏了，
                # 其实是这段在空跑。以前只在整跑时踩不到，所以一直没暴露。
                if taken:
                    await send(ws, "Runtime.evaluate", {"awaitPromise": True,
                        "expression":
                        "(async()=>{switchProfile('consumer');"
                        "await new Promise(r=>setTimeout(r,900));"
                        "document.getElementById('q').value='买到过期食品能赔多少';"
                        "await send();"
                        "await new Promise(r=>setTimeout(r,900));"
                        "window.scrollTo({top:0});return 'ok';})()"})
                if await _capture(ws, "chat_autotask_two.png"):
                    r = await send(ws, "Runtime.evaluate", {
                        "returnByValue": True, "expression":
                        "(()=>{const b=document.getElementById('taskbar');"
                        "return (b&&!b.hidden?'shown':'hidden')+'|'"
                        "+document.getElementById('tb-name').textContent+'|'"
                        "+(b?b.getAttribute('data-pid'):'')+'|'"
                        "+document.body.getAttribute('data-profile')+'|'"
                        "+(currentTask||'');})()"})
                    v = (r.get("result", {}) or {}).get("value") or ""
                    print(f"  换法后第二问：{v}")
                    q = v.split("|")
                    if len(q) != 5 or q[0] != "shown":
                        print("  [FAIL] 换法后第二问，顶部任务条没跟上新任务")
                        return 1
                    if q[1] != "买到过期食品能赔多少" or q[2] != "consumer":
                        print(f"  [FAIL] 第二问没落成消费内核的新任务（实测 "
                              f"{q[1]!r} / {q[2]!r}）——多半又写进旧任务了")
                        return 1
                    if q[3] != "consumer":
                        print(f"  [FAIL] 换法后整页没切到新法（body[data-profile]="
                              f"{q[3]!r}）")
                        return 1
                    # 两个任务、各挂一个会话、分属两个法。
                    # 「旧任务会话数还是 1」是关键：只断「有 2 个任务」的话，
                    # 把新会话塞进旧任务、另建一个空任务也能凑出 2 个。
                    r = await send(ws, "Runtime.evaluate", {
                        "returnByValue": True, "awaitPromise": True, "expression":
                        "fetch('/api/tasks').then(r=>r.json()).then(d=>{"
                        "const it=d.items||[];"
                        "return it.length+'|'"
                        "+it.map(t=>t.profile_id+':'+t.n_sessions)"
                        ".sort().join(',')+'|'"
                        "+it.filter(t=>t.task_id===currentTask).length;})"})
                    v = (r.get("result", {}) or {}).get("value") or ""
                    print(f"  两次咨询的任务：{v}")
                    q2 = v.split("|")
                    if len(q2) != 3 or q2[0] != "2" \
                            or q2[1] != "consumer:1,labor:1":
                        print(f"  [FAIL] 两次咨询应当是 2 个任务、各挂 1 个会话、"
                              f"分属两个法（实测 {v}）")
                        return 1
                    if q2[2] != "1":
                        print(f"  [FAIL] 当前任务和界面上的 task_id 对不上（实测 {v}）")
                        return 1
                    # 任务页：两张卡，一张红一张绿，徽章各自对。
                    taken3 = await shot(ws, base_auto + "/",
                                        "chat_autotask_cards.png",
                                        wait=2.2, theme="light",
                                        js="(async()=>{window.LawNav.panel('tasks');"
                                           "await new Promise(r=>setTimeout(r,1600));"
                                           "})()")
                    if taken3:
                        r = await send(ws, "Runtime.evaluate", {
                            "returnByValue": True, "expression":
                            "(()=>{const cs=[...document.querySelectorAll"
                            "('#task-list .task-card')];"
                            "return cs.length+'|'+cs.map(c=>"
                            "(c.getAttribute('data-pid')||'-')).sort().join(',');})()"})
                        v = (r.get("result", {}) or {}).get("value") or ""
                        print(f"  换法后的任务页：{v}")
                        q3 = v.split("|")
                        if len(q3) != 2 or q3[0] != "2" \
                                or q3[1] != "consumer,labor":
                            print(f"  [FAIL] 任务页应当是两张卡、分属两个法"
                                  f"（实测 {v}）")
                            return 1
            finally:
                stop_service(svc_auto)

            # 退出登录态再截登录页，否则服务端会重定向回首页
            await send(ws, "Network.clearBrowserCookies")
            await shot(ws, base + "/login", "login.png", wait=1.2, theme="light")
            await shot(ws, base + "/login", "login_dark.png", wait=1.2, theme="dark")

            # 最后一张：缺正式语料时的登录页。
            # 这张不是「凑数的一张图」，是给运维看的证据——语料没了程序不报错，
            # 全靠这行字说清楚。文案一变长就可能从卡片里横着挤出去（.sub 是
            # 单行槽位、min-height 只有 18px），所以除了拍图还量一下宽度。
            svc_seed, base_seed = start_service(
                env={"LAW_CORPUS_PATH": str(ROOT / "data" / "seed_statutes.jsonl")})
            taken = await shot(ws, base_seed + "/login", "login_corpus_seed.png",
                               wait=2.2, theme="light")
            if taken:
                # 必须带 returnByValue：这条返回的是**对象**，不带的话 CDP 只回
                # 一个远程引用（{type:'object'}），value 是 None，断言就变成
                # 「None×None」。本文件其它断言返回的是字符串/数字，所以从来
                # 没踩到——这次是第一个返回对象的。
                r = await send(ws, "Runtime.evaluate", {"returnByValue": True,
                    "expression":
                    "(()=>{const e=document.getElementById('stat');"
                    "if(!e) return {missing:true};"
                    "const b=e.getBoundingClientRect();"
                    "return {t:e.textContent.trim(),"
                    "seed:e.classList.contains('seed'),"
                    "over:e.scrollWidth>e.clientWidth+1,"
                    "h:Math.round(b.height),w:Math.round(b.width)};})()"})
                v = r.get("result", {}).get("value") or {}
                print(f"  语料告警：{v.get('t')}（{v.get('w')}×{v.get('h')}）")
                if v.get("missing") or not v.get("seed") \
                        or "示例语料" not in (v.get("t") or ""):
                    print("  [FAIL] 缺语料时登录页没给出告警")
                    return 1
                # 告警不能把运行模式挤掉——两个事实抢一行，容易只顾大的。
                if "LLM" not in (v.get("t") or ""):
                    print("  [FAIL] 告警把运行模式挤掉了（应同时报 LLM 状态）")
                    return 1
                if v.get("over"):
                    print("  [FAIL] 告警文案横向溢出卡片")
                    return 1
            stop_service(svc_seed)
            svc_seed = None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:          # noqa: BLE001
            proc.kill()
        stop_service(svc)
        stop_service(svc_task)
        # 中途 return 1 时它可能还活着（断言失败在 stop 之前）
        if svc_seed:
            stop_service(svc_seed)
        # 截图用的令牌也要收：每次跑都发一个、三天才过期，反复跑就会在库里
        # 留下一串仍然有效的凭据（曾经攒到 28 个）。
        for t in (tok_admin, tok_demo):
            if t:
                try:
                    store.revoke_token(t)
                except Exception:  # noqa: BLE001
                    pass
        # 临时库随用随删。删不掉也不影响什么（在系统临时目录里），
        # 所以这里不做「删不掉就报错」——截图脚本不该因为这个退出码非零。
        for f in (tmpdb, Path(str(tmpdb) + "-wal"), Path(str(tmpdb) + "-shm")):
            try:
                if f.exists():
                    f.unlink()
            except Exception:      # noqa: BLE001
                pass
        try:
            tmpdir.rmdir()          # 目录空了才删得掉；不空就算了
        except Exception:          # noqa: BLE001
            pass

        # 目录里可能还躺着以前版本的脚本产过、名字已不在本次清单里的图。
        # 它们不进交付包（pack_src 排除 _shot），但会让数图的人以为漏了或
        # 多了。不删（那是这台机器上的东西），只把话说清。
        stale = sorted(p.name for p in OUT.glob("*.png")
                       if p.stat().st_mtime < started)
        # 只重出几张时（ONLY 非空），没被点名的 20 多张当然「不是本次生成」，
        # 列出来纯属噪音，所以那种情况不提示。
        if stale and not ONLY:
            print(f"  注：本目录另有 {len(stale)} 张不是本次生成的——"
                  f"{'、'.join(stale)}")
            print("      历史遗留，不进交付包；确认没用可以手动删。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
