# -*- coding: utf-8 -*-
"""
界面交互回归：用真实浏览器（Edge 无头 + CDP）验证「看得见的功能」真的能用。

为什么需要它：
  上一版的教训是——接口写好、单测全绿，但界面上根本没人调用它。
  单测测不到「按钮点下去有没有反应」，截图也只能证明「某一刻长得对」。
  这个脚本补的就是中间那段：点一下，然后读回真实状态。

测什么（全部在浏览器里真执行）：
  1. 主题开关两态互切（v127 起是开关，不再是三态循环的图标按钮），
     开关位置与 <html data-theme> 一致，改动落到 localStorage
  2. 字号按钮三档循环，改动落到 <html data-fs>
  3. 刷新后主题/字号还在（localStorage 生效）
  4. 主按钮实测高度 ≥ 44px（触摸下限）
  5. 鼠标移到按钮上，transform 真的变了（悬停反馈不是写在注释里的）
  6. 空状态示例卡片点一下，问题被填进输入框
  7. 快捷键面板能打开、Esc 能关
  8. 「新对话」在已有内容时清空并给出提示
  ...
  14. 左侧七个入口点得到；时效计算、仲裁申请书两个新面板真能算/真能列
  15. Esc 只关最上面那一层（层叠顺序），关完焦点回到打开它的按钮
  16. 帮助层优先：它开着时 Esc 不顺手关下层抽屉
  17. Alt+1..7 直达七个入口（走 CDP 真按键，不是合成事件）

跑法（需要先启动服务）：
    ./.venv/Scripts/python.exe scripts/check_ui.py
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth import AuthStore              # noqa: E402
from app.config import DB_PATH, TOKEN_NAME  # noqa: E402
from app.costs import bad_percentages as _bad_pct   # noqa: E402
from app.memory import MemoryStore          # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from _svc import start_service, stop_service   # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9334            # Edge 的 CDP 调试端口
# 服务由本脚本自己起、端口运行时随机挑（见 main() 里的注释），不再写死 8000——
# 写死 8000 等于默认「8000 上那个服务是本目录的」，而这件事从来没被验证过。
PROFILE = ROOT / "eval" / "_shot" / "profile_ui"

# 1×1 的 PNG。测图片证据时用它现造一个 File 对象——比让 CDP 去点系统文件
# 选择框可靠得多（那个框是原生窗口，无头模式里根本点不到）。
PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

_msg_id = 0
fails: list[str] = []
# 自检过程中真发提问会新建会话，跑完必须删干净——它挂在演示账号名下，
# 留着就是一条「用户从没问过的问题」。
created: list[str] = []
# v143：除了主自检账号，还会临时建「零数据」「首登」两个账号。它们都在
# 中途自己删过一次，但中途抛异常就删不到——finally 里再兜一遍。
_temp_users: list[int] = []


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


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))
    if not ok:
        fails.append(name)


async def press_tab(ws, shift: bool = False) -> None:
    """真的按一下 Tab。

    不能用 JS 派发 KeyboardEvent——合成事件不触发浏览器默认动作，
    Tab 不会移动焦点，测出来永远是「没出界」，等于什么都没验。
    必须走 CDP 的输入通道，浏览器才会执行默认行为。
    """
    p = {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9,
         "nativeVirtualKeyCode": 9, "modifiers": 8 if shift else 0}
    await send(ws, "Input.dispatchKeyEvent", {"type": "rawKeyDown", **p})
    await send(ws, "Input.dispatchKeyEvent", {"type": "keyUp", **p})


async def press_key(ws, key: str, code: str, vk: int,
                    modifiers: int = 0) -> None:
    """按一个非 Tab 的键（Alt、Esc 之类）。CDP modifiers 是位掩码，
    Alt=1、Ctrl=2、Meta=4、Shift=8。"""
    p = {"key": key, "code": code, "windowsVirtualKeyCode": vk,
         "nativeVirtualKeyCode": vk, "modifiers": modifiers}
    await send(ws, "Input.dispatchKeyEvent", {"type": "rawKeyDown", **p})
    await send(ws, "Input.dispatchKeyEvent", {"type": "keyUp", **p})


async def click_at(ws, sel: str) -> bool:
    """用真实鼠标点。

    同样是「合成 vs 真实」的差别：element.click() 不移动焦点，
    于是 activeElement 停在原来的地方，测「关闭后焦点还给谁」会得到假通过。
    """
    r = await js(ws, f"(()=>{{const e=document.querySelector({json.dumps(sel)});"
                     "if(!e)return null;const b=e.getBoundingClientRect();"
                     "return {x:Math.round(b.x+b.width/2),"
                     "y:Math.round(b.y+b.height/2)};})()")
    if not r:
        return False
    for t in ("mousePressed", "mouseReleased"):
        await send(ws, "Input.dispatchMouseEvent",
                   {"type": t, "x": r["x"], "y": r["y"],
                    "button": "left", "clickCount": 1})
    return True


async def main() -> int:
    store = AuthStore(DB_PATH)
    users = {u["username"]: u for u in store.list_users(size=100)["items"]}
    # 用自己的账号，并且**先把首登档案弹窗的状态置成 done**。
    #
    # 原来这里是「借库里现成的普通账号，没有才建」。两种情况下都会出事：
    #  · 交付包解压后是全新库，只能新建 —— 新用户 onboarding=pending，
    #    于是页面一进来就被档案弹窗和遮罩盖住。后面的断言全是「点某个坐标」，
    #    点到的其实是遮罩：改密码弹窗打不开、焦点掉到 body……十几条一起红，
    #    看着像功能坏了，实际是测试脚本忘了关弹窗。实测解压包 19 / 20。
    #  · 借来的账号也可能 pending，于是同一条套件在别人机器上时绿时红。
    #
    # 档案弹窗本身有专门的套件验（test_profile_task.py 51 项），这里不该
    # 被它干扰——两边各自把状态摆成自己需要的形状，才谈得上稳定。
    demo = users.get("ui-check-demo")
    if demo is None:
        demo = store.create_user("ui-check-demo", "ui-check-pw-123",
                                 role="user", ip="127.0.0.1")
        print("  新建自检账号 ui-check-demo（跑完删）")
    # 上次跑到一半被杀会留下这个账号，复用它并强制把状态摆正。
    store.save_profile(demo["id"], {"real_name": "界面自检"}, onboarding="done")
    _created_user = demo["id"]
    token = store.issue_token(demo["id"], ua="ui-check")

    # 造一条固定的自检会话。历史、搜索、反馈、档案面板、申请书这五段都要
    # 「先有一个会话」才测得到；靠库里碰巧存着数据，断言就会时绿时红——
    # 而红的时候你分不清是功能坏了还是数据没了。跑完在 finally 里删掉。
    mem = MemoryStore(DB_PATH)
    seed_sid = "ui-check-seed"
    seed_q = "公司拖欠我三个月工资，也没签劳动合同"
    mem.ensure_session(seed_sid, demo["id"])
    mem.save_message(seed_sid, "user", seed_q)
    mem.save_message(seed_sid, "assistant",
                     "先把证据固定下来：工资流水、考勤记录、工作群聊天记录。")

    # 自己起一个服务，端口随机挑。
    #
    # 为什么不连 8000 上「已经在那儿」的那个：那个服务属于哪个目录是不确定的。
    # 开发机上它常年是 D:\law-agent 起的，解压出来的副本跑这个脚本时，token 是
    # 从**副本的库**里签的，却被拿去打**原目录的服务**——验不过，重定向到登录页，
    # 于是 `[data-fs-btn]` 取不到，报一个 TypeError 说元素为空。看起来像页面坏了，
    # 实际是**测错了实例**。而且它只在「8000 上已经有服务」时才现形，属于那种
    # 换台机器就不复现的问题。
    #
    # 自己起还顺带解决另一件事：断言的是本目录的代码与本目录的库。
    #
    # 把模型 key 清空，强制回放。两个理由，第二个更硬：
    #  1) 这是**界面**回归，验的是事件有没有接上，不是模型答得好不好。
    #     每次跑都要花一次真实调用，时间久了没人愿意跑。
    #  2) 「流式结束后思考过程自动收起」那条要等 done 事件，而真调模型时
    #     done 什么时候到由模型决定——实测 29.3s，而等待上限写的是 30s。
    #     有一次就因为这 0.7 秒红了，看着像折叠逻辑坏了，其实模型那天慢了点。
    #     回放是本地生成的轨迹，秒级返回，这条断言才真的在测 UI。
    # 真实链路仍然有人验：`run_all.py --with-model`（那条路会明说「要花钱」）。
    svc, base = start_service(env={"DEEPSEEK_API_KEY": ""})
    print(f"  已起服务：{base}（回放模式，不调模型）")

    proc = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        uri = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json",
                                            timeout=1) as f:
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
            # 必须关缓存：Edge 的 profile 目录是复用的，ui.js 改完第一次跑
            # 还能拿到旧文件——表现是「新加的函数不存在」这种莫名其妙的
            # TypeError，人容易去怀疑代码而不是缓存。
            await send(ws, "Network.setCacheDisabled", {"cacheDisabled": True})
            await send(ws, "Page.enable")
            await send(ws, "Runtime.enable")
            await send(ws, "Network.setCookie", {
                "name": TOKEN_NAME, "value": token, "domain": "127.0.0.1",
                "path": "/", "httpOnly": True, "sameSite": "Lax"})
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
            await send(ws, "Page.navigate", {"url": base + "/"})
            await asyncio.sleep(2.2)

            print("界面交互：")

            # 1. 主题开关（v127 从图标按钮改成 switch）：两态互切。
            #    v129 起连「第一次按系统偏好落一次」也删了，只有日读/夜读两档。
            #    下面把系统的 prefers-color-scheme 强行模拟成 dark 再清 storage
            #    重进——如果还有任何一条跟随系统的路径，页面就会变深色，这条
            #    立刻红。没这条断言的话，「回落到系统」是读代码才看得出的退化。
            await send(ws, "Emulation.setEmulatedMedia", {
                "features": [{"name": "prefers-color-scheme", "value": "dark"}]})
            await js(ws, "localStorage.removeItem('law.theme');"
                         "localStorage.removeItem('law.fs');location.reload();")
            await asyncio.sleep(1.8)
            first_stored = await js(ws, "localStorage.getItem('law.theme')")
            first_theme = await js(ws, "document.documentElement.dataset.theme")
            check("清空存储后仍是日读（不跟随系统配色）",
                  first_stored == "light" and first_theme == "light",
                  f"stored={first_stored!r} theme={first_theme!r} "
                  f"（已把系统模拟成 dark）")
            await send(ws, "Emulation.setEmulatedMedia", {"features": []})
            seq = []
            for _ in range(3):
                await js(ws, "document.querySelector('.theme-sw input').click()")
                # data-theme 是在过场回调里同步写的，但 380ms 的扩散圆还在跑，
                # 等够 0.6s 免得第三下和第一下叠在一起互相 cancel。
                await asyncio.sleep(0.6)
                seq.append(await js(ws, "document.documentElement.dataset.theme"))
            check("主题开关两态互切", seq[0] != seq[1] and seq[0] == seq[2], seq)
            check("开关位置与 data-theme 同步",
                  await js(ws, "document.querySelector('.theme-sw input').checked"
                               " === (document.documentElement.dataset.theme"
                               " === 'dark')"), seq)
            check("data-theme 与 localStorage 一致",
                  await js(ws, "localStorage.getItem('law.theme')") in
                  ("light", "dark"),
                  await js(ws, "localStorage.getItem('law.theme')"))

            # color-scheme 是**唯一**不看 data-theme 的东西：它决定滚动条、
            # 下拉箭头、自动填充底色这些原生部件怎么画。不显式声明就是
            # normal，Windows 开着深色模式时日读页面会配一条深色滚动条——
            # 这正是「还在跟随系统」的观感来源。断言它跟主题同向。
            check("原生部件配色跟着主题（不跟随系统）",
                  await js(ws, "getComputedStyle(document.documentElement)"
                               ".colorScheme") ==
                  await js(ws, "document.documentElement.dataset.theme"),
                  await js(ws, "getComputedStyle(document.documentElement)"
                               ".colorScheme"))

            # 设置弹窗里那行「配色」要把当前档位写出来。只写「切换」时，用户会
            # 怀疑背后还藏着一档（旧版本是三态，第三档就叫「跟随系统」）。
            now_label = await js(ws, "document.querySelector('[data-theme-now]')"
                                     ".textContent")
            now_theme = await js(ws, "document.documentElement.dataset.theme")
            check("设置里写明当前档位（只有日读/夜读两档）",
                  now_label == ("夜读" if now_theme == "dark" else "日读"),
                  f"{now_label} / data-theme={now_theme}")

            # 切主题途中不许出现 ::view-transition-group(root) 的动画。
            # 那是浏览器默认那条「旧帧盒子 → 新帧盒子」插值，两帧尺寸只要差
            # 一条滚动条，整页就会被横向挤一下再弹回（过场**过程中**的震，
            # 和 v129 删掉的「结束后上浮 6px」是两回事）。
            # 它只有 250ms，过完再查一定是干净的——必须在点之前布采样器，
            # 否则这条断言恒绿。这正是「写了个抓不住的断言」的经典形态。
            await js(ws, r"""
              window.__grp = false;
              window.__old = [];
              window.__t0 = performance.now();
              (function tick(){
                if (document.getAnimations) {
                  document.getAnimations().forEach(function(a){
                    var p = a.effect && a.effect.pseudoElement;
                    if (p === '::view-transition-group(root)') window.__grp = true;
                    /* v131：换主题时 old(root) 不该有任何动画。
                       它一有动画就说明「旧帧淡到 88% + 过一遍 brightness()」
                       又被接上了（--vc-old-op 从没人设过，兜底值就是 .88）——
                       圆还没盖到的地方整页先暗一截，观感是闪一下 / 震一下。
                       记名字而不是记布尔，红了能直接看出是谁在动。 */
                    if (p === '::view-transition-old(root)' &&
                        a.animationName && a.animationName !== 'none') {
                      if (window.__old.indexOf(a.animationName) < 0)
                        window.__old.push(a.animationName);
                    }
                  });
                }
                if (performance.now() - window.__t0 < 900) requestAnimationFrame(tick);
              })();
            """)
            await js(ws, "document.querySelector('.theme-sw input').click()")
            await asyncio.sleep(1.0)
            check("切主题没有整页盒子插值（不震）",
                  await js(ws, "window.__grp") is False)
            old_anims = await js(ws, "window.__old")
            check("换主题时旧帧一动不动（不淡出/不压暗/不过滤镜）",
                  old_anims == [], f"old(root) 上的动画={old_anims}")
            check("--vc-out-name 已置为 none",
                  await js(ws, "getComputedStyle(document.documentElement)"
                               ".getPropertyValue('--vc-out-name').trim()") == "none")

            # 2. 字号
            fs = []
            for _ in range(3):
                await js(ws, "document.querySelector('[data-fs-btn]').click()")
                await asyncio.sleep(0.35)
                fs.append(await js(ws, "document.documentElement.dataset.fs"))
            check("字号按钮三档切换", set(fs) == {"s", "m", "l"}, fs)

            # 3. 刷新后保持
            before_theme = await js(ws, "document.documentElement.dataset.theme")
            await js(ws, "location.reload()")
            await asyncio.sleep(1.8)
            check("刷新后主题保持",
                  await js(ws, "document.documentElement.dataset.theme") == before_theme,
                  before_theme)

            # 4. 按钮高度（触摸下限 44px）
            h = await js(ws, "Math.round(document.getElementById('send')"
                             ".getBoundingClientRect().height)")
            check("发送按钮高度 ≥ 44px", h >= 44, f"{h}px")
            # 开关真正的命中区是 label（里面的 input 是 1px 的隐身元素），
            # 量 input 只会得到 1px，永远红。
            h2 = await js(ws, "Math.round(document.querySelector('.theme-sw')"
                              ".getBoundingClientRect().height)")
            check("主题开关命中区高度 ≥ 36px", h2 >= 36, f"{h2}px")

            # 5. 悬停反馈：把鼠标真的移上去，再读 transform
            await js(ws, "window.scrollTo(0,0)")
            rect = await js(ws, "(()=>{const r=document.getElementById('send')"
                                ".getBoundingClientRect();"
                                "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
            base_t = await js(ws, "getComputedStyle(document.getElementById('send')).transform")
            await send(ws, "Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": rect["x"], "y": rect["y"]})
            await asyncio.sleep(0.45)
            hover_t = await js(ws, "getComputedStyle(document.getElementById('send')).transform")
            check("按钮悬停有位移/缩放", hover_t != base_t, f"{base_t} → {hover_t}")
            await send(ws, "Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": 4, "y": 4})

            # 6. 示例卡片填入输入框
            await js(ws, "document.querySelector('.ex-card').click()")
            await asyncio.sleep(0.3)
            val = await js(ws, "document.getElementById('q').value")
            check("示例卡片填入输入框", bool(val and len(val) > 6), val[:24] + "…")

            # 6b. v144：首屏点开示例浮层，不许把法典胶囊和免责声明切一半。
            #     面板 430×200 从输入框上方展开，而首屏垂直居中——两者必然相交。
            #     修之前实测：五颗胶囊里前三颗被压掉 66% / 67% / 29%，
            #     「仅供参考，不构成法律意见。」13 个字被盖掉 5 个，截图里只剩
            #     「不构成法律意见」悬在面板右边。用户管这个叫「乱码」。
            #     判据必须**逐字量**（Range 取每个字的矩形），不能只看元素外框：
            #     外框相交不等于字被切，被盖掉几个字才是判据。
            land_top0 = await js(
                ws, "Math.round(document.getElementById('landing')"
                    ".getBoundingClientRect().top)")
            await js(ws, "toggleExPop(true)")
            await asyncio.sleep(0.6)          # 等 280ms 让位动画走完
            room = await js(ws, """(() => {
              const pop = document.getElementById('ex-pop');
              const pr = pop.getBoundingClientRect();
              const inter = (a, c) => Math.max(0,
                  Math.min(a.right, c.right) - Math.max(a.left, c.left)) *
                Math.max(0, Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top));
              const chips = [...document.querySelectorAll('#land-pills > *')]
                .map(e => Math.round(inter(pr, e.getBoundingClientRect())));
              const dis = document.querySelector('.land-disclaim');
              let cov = 0, all = 0;
              const node = [...dis.childNodes].find(n => n.nodeType === 3);
              if (node) {
                const rg = document.createRange();
                for (let i = 0; i < node.length; i++) {
                  rg.setStart(node, i); rg.setEnd(node, i + 1);
                  const b = rg.getBoundingClientRect();
                  if (b.width < 0.5) continue;
                  all++;
                  if (inter(pr, b) > 0) cov++;
                }
              }
              const land = document.getElementById('landing')
                .getBoundingClientRect();
              return {chips, cov, all, popTop: Math.round(pr.top),
                      landTop: Math.round(land.top),
                      landBottom: Math.round(land.bottom),
                      shift: document.getElementById('landing').style
                        .getPropertyValue('--ex-shift')};
            })()""")
            check("首屏示例浮层不压法典胶囊（五颗一颗都不压）",
                  room and len(room.get("chips") or []) == 5
                  and max(room["chips"]) == 0, room and room.get("chips"))
            check("首屏示例浮层不切断免责声明（逐字量，一个字都不盖）",
                  room and room.get("all", 0) >= 12 and room.get("cov") == 0,
                  room and f"盖住 {room.get('cov')}/{room.get('all')} 字")
            # 窄屏：面板右缘出屏会把 document.scrollWidth 撑大，整页能左右晃。
            # 390 下修之前是 86 + 335 = 421，比屏幕宽 31px。
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1,
                "mobile": True})
            await asyncio.sleep(0.7)
            nar = await js(ws, """(() => {
              const pop = document.getElementById('ex-pop').getBoundingClientRect();
              return {right: Math.round(pop.right),
                      cw: document.documentElement.clientWidth,
                      sw: document.documentElement.scrollWidth};
            })()""")
            check("窄屏（390）示例浮层不出右缘（整页不能被撑出横向滚动）",
                  nar and nar.get("right", 9999) <= nar.get("cw", 0)
                  and nar.get("sw") == nar.get("cw"), nar)
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
            await asyncio.sleep(0.5)
            await js(ws, "toggleExPop(false)")
            await asyncio.sleep(0.5)
            back = await js(ws, """(() => {
              const land = document.getElementById('landing');
              return {top: Math.round(land.getBoundingClientRect().top),
                      shift: land.style.getPropertyValue('--ex-shift'),
                      cls: document.body.className,
                      open: !document.getElementById('ex-pop').hidden};
            })()""")
            check("关掉示例浮层后首屏回到原位（不留位移、不留展开标记）",
                  back and back.get("top") == land_top0
                  and not back.get("shift")
                  and "ex-open" not in (back.get("cls") or "")
                  and back.get("open") is False,
                  f"top {land_top0} → {back.get('top')} / {back}")
            await js(ws, "document.getElementById('q').value = ''")

            # 6c. v146：390 下这条底栏原来被挤成一根柱子——提问参考 79 +
            #     图片 44 + 提问 65 + 三个 10px 间隙 = 218，可用宽只有 276，
            #     剩 58 给输入框，再减掉 body.blank 给的左右各 24 内边距，
            #     正文只剩 15px：一个字一行，「说说你的情况」竖着排下来，
            #     截图上就是乱码。修法是把底栏改成两行（输入框独占第一行）。
            #     判据量「正文可用宽」，不是量输入框外框——外框 58 也能是
            #     「有宽度」，但里面写不了字。
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1,
                "mobile": True})
            await asyncio.sleep(0.7)
            q390 = await js(ws, """(() => {
              const q = document.getElementById('q');
              const cs = getComputedStyle(q);
              const r = q.getBoundingClientRect();
              const s = document.getElementById('send').getBoundingClientRect();
              return {w: Math.round(r.width), h: Math.round(r.height),
                      inner: Math.round(r.width - parseFloat(cs.paddingLeft)
                                        - parseFloat(cs.paddingRight)),
                      ownRow: Math.round(r.bottom) <= Math.round(s.top) + 2,
                      over: Math.round(document.documentElement.scrollWidth
                                       - document.documentElement.clientWidth)};
            })()""")
            check("窄屏（390）首屏输入框能写字（正文宽 ≥180px，不再是一根柱子）",
                  q390 and (q390.get("inner") or 0) >= 180
                  and (q390.get("ownRow") is True)
                  and q390.get("over") == 0, q390)

            # 6d. v146：占位提示被输入框裁掉是「看不见的」——placeholder 不参与
            #     布局，DOM 里没有任何痕迹，只能量文本宽。canvas 用同一套字体
            #     量一遍，再和「输入框可用宽」比。390 下实测裁过三处：
            #     图书馆的「输入条号…（引用原文/摘录）」、单位全称、仲裁委。
            #     只扫 <input>：<textarea> 的占位语本来就换行，不算被裁。
            #     在「个人档案」页扫——那里有八个单行输入框，样本才够；
            #     首屏只有一个 textarea，扫出来 n=1，那种断言等于没测。
            await js(ws, "window.LawNav.profile()")
            await asyncio.sleep(1.0)
            ph390 = await js(ws, """(() => {
              const c = document.createElement('canvas').getContext('2d');
              const bad = [];
              let n = 0;
              document.querySelectorAll('input').forEach(el => {
                if (!el.placeholder || el.offsetParent === null) return;
                const cs = getComputedStyle(el);
                if (el.type === 'hidden' || cs.visibility === 'hidden') return;
                n++;
                c.font = cs.font || (cs.fontSize + ' ' + cs.fontFamily);
                const need = c.measureText(el.placeholder).width;
                const avail = el.clientWidth - parseFloat(cs.paddingLeft)
                              - parseFloat(cs.paddingRight);
                if (need > avail + 1) bad.push(el.id || el.className);
              });
              return {n, bad};
            })()""")
            check("窄屏（390）占位提示都不被输入框裁掉（canvas 量文本宽）",
                  ph390 and (ph390.get("n") or 0) >= 6
                  and not ph390.get("bad"), ph390)

            # 6e. v146：历史记录的「标题 + 右侧搜索框」一行排开，390 下标题被
            #     压到 min-content（两个字宽）折成两行。窄屏改成上下两行。
            await js(ws, "window.LawNav.history()")
            await asyncio.sleep(0.9)
            hq = await js(ws, """(() => {
              const h = document.querySelector('#p-history .pad-h');
              const s = document.getElementById('d-q');
              if (!h || !s) return null;
              const hr = h.getBoundingClientRect();
              const sr = s.getBoundingClientRect();
              const lh = parseFloat(getComputedStyle(h).lineHeight) || 24;
              // 比对的是 .pad-head（标题与搜索框那行的可用宽），不是 .pad——
              // .pad 含左右内边距，用它当基准会永远差 20 来像素，假红。
              const head = document.querySelector('#p-history .pad-head');
              return {hH: Math.round(hr.height), lh: Math.round(lh),
                      oneLine: hr.height <= lh * 1.6,
                      below: sr.top >= hr.bottom - 1,
                      sw: Math.round(sr.width),
                      cw: head ? Math.round(head.clientWidth) : 0};
            })()""")
            check("窄屏（390）历史记录标题不折行、搜索框落到下一行且铺满",
                  hq and hq.get("oneLine") is True and hq.get("below") is True
                  and (hq.get("sw") or 0) >= (hq.get("cw") or 0) - 4, hq)
            await js(ws, "window.LawNav.panel('chat')")
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
            await asyncio.sleep(0.7)

            # 6f. v147：同一处**桌面也断**。v146 只在 390 下看这一行，就把
            #     「历史记录折成两行」记成了窄屏问题；1440 上它断成
            #     「历史记」/「录」——两层原因缺一不可：
            #       · theme.css 给 `input[type="search"]` 定了 width:100%，
            #         flex 行里它要求整行 716px；
            #       · 中文的 min-content 是**一个字**，标题能被压到 18px 宽
            #         再逐字换行，看起来不像「挤」，像排版坏了。
            #     判据量的是「标题一行 + 它没被压成几个像素宽 + 搜索框还在
            #     可用尺寸 + 整页不横滚」。只量高度会被「压到 55px 宽、一行
            #     只放得下两个字」这种中间态骗过去。
            await js(ws, "window.LawNav.history()")
            await asyncio.sleep(0.9)
            hd = await js(ws, """(() => {
              const h = document.querySelector('#p-history .pad-h');
              const s = document.getElementById('d-q');
              if (!h || !s) return null;
              const hr = h.getBoundingClientRect();
              const sr = s.getBoundingClientRect();
              const lh = parseFloat(getComputedStyle(h).lineHeight) || 24;
              return {w: Math.round(hr.width), hH: Math.round(hr.height),
                      lh: Math.round(lh),
                      oneLine: hr.height <= lh * 1.6,
                      sw: Math.round(sr.width),
                      over: Math.max(0, Math.round(
                        document.documentElement.scrollWidth - innerWidth))};
            })()""")
            check("桌面（1440）历史记录标题也不折行（搜索框不许把标题压成 min-content）",
                  hd and hd.get("oneLine") is True
                  and (hd.get("w") or 0) >= 70
                  and (hd.get("sw") or 0) >= 200
                  and hd.get("over") == 0, hd)
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.4)


            # 7. 快捷键面板
            await js(ws, "document.querySelector('[data-help-btn]').click()")
            await asyncio.sleep(0.4)
            opened = await js(ws, "document.querySelector('.law-help')"
                                  ".classList.contains('on')")
            await js(ws, "document.dispatchEvent(new KeyboardEvent('keydown',"
                         "{key:'Escape'}))")
            await asyncio.sleep(0.3)
            closed = await js(ws, "!document.querySelector('.law-help')"
                                  ".classList.contains('on')")
            check("快捷键面板可开可关", opened and closed, f"{opened}/{closed}")

            # 8.5 模态框必须真的居中。
            # 曾经的 bug：`.dlg` 用 `animation:scale-in`，而 scale-in 的终帧只有
            # `transform:scale(1)`，会把元素自身的 translate(-50%,-50%) 覆盖掉，
            # 于是弹窗左上角落在视口正中心、右下角被切出屏幕。
            # 「差不多居中」用肉眼很难判断，所以量几何。
            await click_at(ws, "#acct")
            await asyncio.sleep(0.5)
            # v28：改密码收进了设置，顶栏那个按钮现在打开的是设置。
            # 真正的改密码弹窗要从设置里点「修改」进来。
            await click_at(ws, "#set-pwd")
            await asyncio.sleep(0.5)
            geo = await js(ws, """(()=>{const d=document.getElementById('pwd');
              const r=d.getBoundingClientRect();
              return {dx:Math.round(r.left+r.width/2-innerWidth/2),
                      dy:Math.round(r.top+r.height/2-innerHeight/2),
                      bottom:Math.round(r.bottom), vh:innerHeight,
                      shown:!d.hidden};})()""")
            check("改密码弹窗打开", geo.get("shown") is True)
            check("弹窗水平居中（误差 ≤ 2px）", abs(geo.get("dx", 99)) <= 2, geo.get("dx"))
            check("弹窗垂直居中（误差 ≤ 2px）", abs(geo.get("dy", 99)) <= 2, geo.get("dy"))
            check("弹窗没有越出视口底部",
                  geo.get("bottom", 9999) <= geo.get("vh", 0),
                  f"{geo.get('bottom')} / {geo.get('vh')}")

            # 8.6 焦点陷阱。鼠标用户永远发现不了：对话框开着，Tab 三下焦点
            #     已经在顶栏上，再按 Enter 点的是背后的按钮——以为在填密码，
            #     实际触发的是别的东西。
            check("打开弹窗时焦点落在「当前密码」框（不被默认落点覆盖）",
                  await js(ws, "document.activeElement.id") == "p-old",
                  await js(ws, "document.activeElement.id"))
            fwd = []
            for _ in range(12):
                await press_tab(ws)
                fwd.append(await js(ws, "document.getElementById('pwd')"
                                        ".contains(document.activeElement)"))
            check("连按 12 次 Tab，焦点始终留在弹窗内",
                  all(fwd), f"{fwd.count(True)}/12")
            bwd = []
            for _ in range(5):
                await press_tab(ws, shift=True)
                bwd.append(await js(ws, "document.getElementById('pwd')"
                                        ".contains(document.activeElement)"))
            check("Shift+Tab 反向绕回来也不出界",
                  all(bwd), f"{bwd.count(True)}/5")

            await click_at(ws, "#p-cancel")
            await asyncio.sleep(0.5)
            # 上一级是设置，不是页面。取消改密码要回到设置——
            # 焦点陷阱记的「打开者」是设置里那个「修改」按钮，
            # 它此刻在 display:none 的容器里，focus() 不生效，焦点会掉到 body。
            check("取消改密码后回到设置（不把人扔回主界面）",
                  await js(ws, "!document.getElementById('setdlg').hidden") is True)
            check("焦点留在设置弹窗内",
                  await js(ws, "document.getElementById('setdlg')"
                               ".contains(document.activeElement)") is True,
                  await js(ws, "document.activeElement.id || "
                               "document.activeElement.tagName"))
            check("改密码的陷阱已摘除",
                  await js(ws, "(()=>{const a=document.activeElement;"
                               "return a===document.body||!document."
                               "getElementById('pwd').contains(a);})()"))

            await click_at(ws, "#set-close")
            await asyncio.sleep(0.4)
            check("关掉设置后焦点回到顶栏「设置」按钮（不掉到 body）",
                  await js(ws, "document.activeElement.id") == "acct",
                  await js(ws, "document.activeElement.id || document."
                               "activeElement.tagName"))
            await asyncio.sleep(0.2)

            # 8. 新对话：先塞一条假内容再点
            await js(ws, "window.LawNav.history()")
            await asyncio.sleep(1.4)
            n = await js(ws, "document.querySelectorAll('#dlist .di').length")
            if n:
                await js(ws, "document.querySelector('#dlist .di .di-main').click()")
                await asyncio.sleep(1.6)
                had = await js(ws, "document.getElementById('feed').children.length")
                await js(ws, "document.getElementById('newchat').click()")
                await asyncio.sleep(0.6)
                now = await js(ws, "document.getElementById('feed').children.length")
                empty_on = await js(ws, "!document.getElementById('empty').hidden")
                check("新对话清空并回到空状态",
                      had > 0 and now == 0 and empty_on, f"{had} → {now}")
            else:
                check("新对话清空并回到空状态", False, "库里没有历史会话，跳过")

            # 9. CSRF：前端 fetch 包装层是否真的把令牌抄进了请求头。
            #    期望 400（空问题被业务层挡住）；如果是 403，说明包装层没生效，
            #    真实使用下所有写请求都会挂。
            st = await js(ws, "(async()=>{const r=await fetch('/api/chat',"
                              "{method:'POST',headers:{'Content-Type':"
                              "'application/json'},body:JSON.stringify({question:''})});"
                              "return r.status;})()")
            check("前端写请求自动带 CSRF 头（非 403）", st == 400, f"HTTP {st}")
            check("浏览器持有 csrf cookie",
                  bool(await js(ws, "document.cookie.indexOf('law_csrf=')>=0")))

            # 10. 案情档案：面板能不能开、改字段有没有反馈、保存后能不能读回。
            #     这一段的重点是「用户改得动」，不是「面板好看」——记忆
            #     不可编辑，抽错一次就永远错，这是它存在的唯一理由。
            await js(ws, "window.LawNav.history()")
            await asyncio.sleep(1.4)
            has_hist = await js(ws,
                "document.querySelectorAll('#dlist .di').length > 0")
            if has_hist:
                await js(ws, "document.querySelector('#dlist .di .di-main').click()")
                await asyncio.sleep(1.6)
                await js(ws, "window.LawNav.slots()")
                await asyncio.sleep(0.8)
                # v104：案情档案、历史记录、个人档案都从抽屉改成了独立页面，
                # 所以判据不再是「抽屉 hidden 属性」，而是「哪个 view 带 on」。
                opened = await js(ws,
                    "document.getElementById('p-slots').classList.contains('on')")
                rows = await js(ws,
                    "document.querySelectorAll('#sl-fields .sl-row').length")
                only_one = await js(ws,
                    "document.querySelectorAll('.view.on').length")
                hist_off = await js(ws,
                    "!document.getElementById('p-history').classList.contains('on')")
                check("档案页面能打开", opened is True)
                check("八个案情要素都渲染出来", rows == 8, rows)
                check("切页时只有一个 view 处于 on（不并存）", only_one == 1, only_one)
                check("打开档案时历史页面已切走", hist_off is True)
                # v104 新增：每条历史记录都要有删除入口，缺一条用户就删不掉
                # 那条记录（只能整库清空），而界面上不会有任何提示。
                n_hdel = await js(ws,
                    "document.querySelectorAll('#dlist .di .di-del').length")
                check("历史记录每条都有删除按钮", n_hdel >= 1, n_hdel)

                # 自动填充防护。截图里抓到过真事：面板最后一个输入框
                # 被浏览器填了登录用户名 admin，而库里那个字段是空的——
                # 用户看到一条自己没说过的事实，点保存就写进案情。
                attrs = await js(ws, "[...document.querySelectorAll("
                                     "'#sl-fields [data-slot]')].map(n=>"
                                     "[n.getAttribute('autocomplete'),n.name])")
                check("槽位输入框禁用自动填充",
                      all((a or [None])[0] == "off" for a in attrs or []),
                      (attrs or [])[:2])
                check("槽位输入框有不含凭据语义的 name",
                      all(str((a or ['', ''])[1]).startswith("slot_")
                          for a in attrs or []),
                      [a[1] for a in (attrs or [])][:2])

                # 直接改 value 不派发事件 = 浏览器注入的状态，必须回滚。
                # 断言的是「退回接口给的原值」，不是「原值一定是空」——
                # 原来写死 `== ""`，而历史里最新那条会话的社保字段可能本来就有值
                # （DB 里真有一条「未缴」），于是同一份脚本时而通过时而失败。
                # 那是在依赖一个没说出口的前提：当前会话的这个字段是空的。
                v0 = await js(ws, "document.querySelector("
                                  "'[data-slot=social_insurance]').value")
                await js(ws, "(()=>{const n=document.querySelector("
                             "'[data-slot=social_insurance]');n.__probe=1;"
                             "n.value='admin';reconcileAutofill();})()")
                v_inj = await js(ws, "document.querySelector("
                                     "'[data-slot=social_insurance]').value")
                check("注入进来的值被回滚成接口给的原值",
                      v_inj == (v0 or "") and v_inj != "admin",
                      f"注入前={v0!r} 注入后={v_inj!r}")
                # 只改 value 不够：被自动填充过的框，Chromium 会另画一层文本，
                # 值清了屏幕上还在。所以要换节点。
                replaced = await js(ws, "!document.querySelector("
                                        "'[data-slot=social_insurance]').__probe")
                check("回滚时连节点一起换掉（不然自动填充的残影留在屏幕上）",
                      replaced is True)

                # 用户亲手敲的要留下，否则这个防护就在吃用户的输入
                await js(ws, "(()=>{const n=document.querySelector("
                             "'[data-slot=social_insurance]');n.value='一直没交';"
                             "n.dispatchEvent(new Event('input'));"
                             "reconcileAutofill();})()")
                v_typed = await js(ws, "document.querySelector("
                                       "'[data-slot=social_insurance]').value")
                check("用户亲手填的值不会被回滚", v_typed == "一直没交", v_typed)
                await js(ws, "(()=>{const n=document.querySelector("
                             "'[data-slot=social_insurance]');n.value='';"
                             "n.dispatchEvent(new Event('input'));})()")

                orig = await js(ws, "document.querySelector("
                                    "'[data-slot=monthly_wage]').value")
                await js(ws, "(()=>{const n=document.querySelector("
                             "'[data-slot=monthly_wage]');n.value='12345';"
                             "n.dispatchEvent(new Event('input'));})()")
                await asyncio.sleep(0.3)
                changed = await js(ws, "(()=>{const r=document.querySelector("
                                       "'.sl-row.changed [data-slot]');"
                                       "return r ? r.dataset.slot : '';})()")
                check("改过的字段被标出来（保存前就能看见）",
                      changed == "monthly_wage", changed)

                await js(ws, "document.getElementById('sl-save').click()")
                await asyncio.sleep(1.4)
                saved = await js(ws, "document.querySelector("
                                     "'[data-slot=monthly_wage]').value")
                check("保存后值真的落到库里", saved == "12345", saved)

                # 复原，别把测试数据留在演示账号的会话里
                await js(ws, "(()=>{const n=document.querySelector("
                             f"'[data-slot=monthly_wage]');n.value={json.dumps(orig)};"
                             "n.dispatchEvent(new Event('input'));})()")
                await js(ws, "document.getElementById('sl-save').click()")
                await asyncio.sleep(1.0)
                back = await js(ws, "document.querySelector("
                                    "'[data-slot=monthly_wage]').value")
                check("测试数据已复原", back == orig, f"{orig!r} → {back!r}")

                # 10.4 两层模态叠加：案情面板上开申请书弹窗。
                #      两个陷阱都装着监听，谁先注册谁先跑；没有层序判断的话，
                #      下面那层发现焦点不在自己里面，就把光标抢回被盖住的面板。
                await click_at(ws, "#sl-doc")
                await asyncio.sleep(2.0)
                doc_open = await js(ws, "!document.getElementById('ddlg').hidden")
                check("申请书弹窗能打开", doc_open is True)
                if doc_open:
                    inner = []
                    for _ in range(8):
                        await press_tab(ws)
                        inner.append(await js(ws, "document.getElementById('ddlg')"
                                                 ".contains(document.activeElement)"))
                    check("面板上叠弹窗时，Tab 只在上层弹窗里转",
                          all(inner), f"{inner.count(True)}/8")
                    await click_at(ws, "#d-close")
                    await asyncio.sleep(0.5)
                    check("关掉弹窗后焦点回到「生成申请书」按钮",
                          await js(ws, "document.activeElement.id") == "sl-doc",
                          await js(ws, "document.activeElement.id || document."
                                       "activeElement.tagName"))
                    check("下层面板的陷阱还在（关了一层不会全丢）",
                          await js(ws, "document.getElementById('p-slots')"
                                       ".classList.contains('on')"))

                # v104：档案是页面，没有「关闭」按钮。关掉上层弹窗之后
                # 页面必须原地不动——原来这里点 sl-close 验「能关」，
                # 那个按钮已经随抽屉一起删掉了。
                check("关掉弹窗后档案页面仍在原处",
                      await js(ws, "document.getElementById('p-slots')"
                                   ".classList.contains('on')"))
            else:
                await js(ws, "window.LawNav.slots()")
                await asyncio.sleep(0.6)
                tip = await js(ws, "document.getElementById('sl-fields').textContent")
                check("没有会话时档案面板给提示",
                      "还没有开始对话" in (tip or ""), (tip or "")[:40])

            # 10.6 v104 结构调整的护栏。
            #      「抽屉删了、脚本还在绑它的按钮」这类断裂不会报错，
            #      只会让后面的代码全都不执行，所以这里正面断言旧结构不存在。
            check("旧抽屉元素已彻底移除",
                  await js(ws, "!document.getElementById('drawer') && "
                               "!document.getElementById('slots')") is True)
            check("个人档案在左侧导航里有独立入口",
                  await js(ws, "!!document.querySelector("
                               "'.nav-i[data-panel=profile]')") is True)
            check("历史记录是独立页面而不是抽屉",
                  await js(ws, "!!document.querySelector('.view#p-history')") is True)

            # 10.6b v134：劳动争议专属文案在非劳动法内核下真的看不见。
            #      check_pages.py 只钉了「HTML 里写了 data-labor-only」，这里用
            #      真实浏览器验 CSS 判据生效。可见文本自己按 display 逐层构造：
            #      textContent 会把藏起来的 span 也算进去，innerText 在 view
            #      未激活时又退化成 textContent —— 两个都会给出假绿。
            v134 = await js(ws, """(()=>{
              const b=document.body, old=b.getAttribute('data-profile');
              const fields=[...document.querySelectorAll('#p-profile [data-labor-only]')];
              const onb=[...document.querySelectorAll('#onb [data-labor-only]')];
              const smell=['仲裁委','单位全称','统一社会信用代码','法定代表人',
                           '单位注册地址','申请书','拖欠工资'];
              const hidden=e=>getComputedStyle(e).display==='none';
              const vis=root=>{
                let out='';
                const walk=n=>{
                  if(n.nodeType===3){out+=n.nodeValue;return;}
                  if(n.nodeType!==1||hidden(n))return;
                  for(const c of n.childNodes)walk(c);
                };
                for(const c of root.children)walk(c);
                return out;
              };
              b.setAttribute('data-profile','family');
              const famHidden=fields.every(hidden);
              const onbHidden=onb.every(hidden);
              const famText=vis(document.getElementById('p-profile'))+'|'+
                            vis(document.getElementById('onb'));
              const leak=smell.filter(w=>famText.includes(w));
              const famLen=famText.length;
              // 就地验红：临时摘掉一个标记，判据必须立刻抓到串味词。
              // 不重跑整套，也不改文件——「没找到」和「探针瞎了」在这里分开。
              const red=document.getElementById('pp-commission').closest('.fld');
              red.removeAttribute('data-labor-only');
              const redText=vis(document.getElementById('p-profile'));
              const redHit=smell.filter(w=>redText.includes(w));
              red.setAttribute('data-labor-only','');
              if(old===null)b.removeAttribute('data-profile');
              else b.setAttribute('data-profile',old);
              const labShown=fields.filter(e=>!hidden(e)).length;
              return {n:fields.length,onbN:onb.length,famHidden,onbHidden,
                      leak,famLen,redHit,labShown,
                      dyn:window.onbOpen.toString().includes(
                          "ONB_LAST = (currentProfile === 'labor')"),
                      prompt:document.getElementById('task-new').onclick.toString()};
            })()""")
            check("婚家内核下个人档案的劳动争议字段真的被 CSS 收起",
                  bool(v134) and v134["famHidden"] and v134["n"] >= 6,
                  v134 and v134["n"])
            check("婚家内核下首登引导的劳动争议块也收起",
                  bool(v134) and v134["onbHidden"], v134 and v134["onbN"])
            # 护栏：可见文本探针必须真的读到内容。否则 leak=[] 是空串假绿
            # ——「没找到串味词」和「压根没读到字」长得一模一样。
            check("可见文本探针真读到了内容（排除空串假绿）",
                  bool(v134) and v134["famLen"] > 80,
                  v134 and v134["famLen"])
            check("婚家内核下档案/引导可见文本无劳动争议字样",
                  bool(v134) and not v134["leak"], v134 and v134["leak"])
            check("就地验红：摘掉一个标记，判据立刻抓到串味词",
                  bool(v134) and len(v134["redHit"]) >= 1,
                  v134 and v134["redHit"])
            check("切回劳动法字段全部回来（不是一刀切删掉）",
                  bool(v134) and v134["labShown"] == v134["n"],
                  v134 and v134["labShown"])
            check("首登引导按内核缩步（非劳动法只留一步）",
                  bool(v134) and v134["dyn"])
            check("新建任务命名提示按内核生成、不再举例拖欠工资",
                  bool(v134) and "某某公司拖欠工资" not in v134["prompt"]
                  and "profileName" in v134["prompt"])

            # 10.7 任务页：每张卡片都要有删除入口（v104 需求）。
            #      只断「按钮在」，**不点** —— 点了会把任务下的会话一起带走，
            #      后面还有一串用例依赖那条历史会话。
            await js(ws, "window.LawNav.panel('tasks')")
            await asyncio.sleep(1.4)
            n_card = await js(ws,
                "document.querySelectorAll('#task-list .task-card').length")
            n_tdel = await js(ws,
                "document.querySelectorAll('#task-list .task-card .danger').length")
            check("任务页每张卡片都有删除按钮",
                  n_card >= 1 and n_tdel == n_card, f"{n_tdel}/{n_card}")
            check("任务页不再出现「未分类」段落",
                  await js(ws, "document.getElementById('task-list')"
                               ".textContent.indexOf('未分类') < 0") is True)

            # 10.7b v137：任务卡第二行要显示「这个案子要办什么」（goal）。
            #      先在库里写一句，再让面板重拉一次——只断「卡片上有这行字」，
            #      不依赖模型（回放也该算得出来）。
            goal_txt = "追回拖欠工资并主张未签合同二倍工资"
            tl = mem.list_tasks(demo["id"]) or []
            first_tid = tl[0]["task_id"] if tl else None
            if first_tid:
                mem.set_task_memory(first_tid, goal=goal_txt)
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.4)
            await js(ws, "window.LawNav.panel('tasks')")
            await asyncio.sleep(1.2)
            got_goal = await js(ws,
                "Array.from(document.querySelectorAll('#task-list .task-goal'))"
                ".map(n=>n.textContent).join('|')")
            check("任务卡第二行显示本案目标（v137）",
                  bool(got_goal) and goal_txt in got_goal, got_goal)

            # 空目标不许留一个空行：清掉再拉一次，卡片上不该再有 .task-goal。
            if first_tid:
                mem.set_task_memory(first_tid, goal="")
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.4)
            await js(ws, "window.LawNav.panel('tasks')")
            await asyncio.sleep(1.2)
            check("没有目标时不渲染空行（v137）",
                  await js(ws, "document.querySelectorAll('#task-list .task-goal')"
                               ".length") == 0)
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.5)

            # 10.7c v137：档案页「系统记下的（可删）」。只记用户说过的，
            #       所以断言看两样：值上屏了、旁边有删除入口。
            mem.add_profile_facts(demo["id"],
                                  {"city": {"value": "杭州",
                                            "source": "我在杭州，月薪8000"}})
            await js(ws, "window.LawNav.panel('profile')")
            await asyncio.sleep(1.2)
            pf = await js(ws, """(()=>{
              const card = document.getElementById('pf-facts-card');
              if (!card) return {missing: true};
              return {hidden: card.hidden,
                      txt: card.textContent || '',
                      btns: card.querySelectorAll('button').length};
            })()""")
            check("档案页列出系统记下的事实并给删除入口（v137）",
                  bool(pf) and pf.get("hidden") is False
                  and "杭州" in (pf.get("txt") or "")
                  and pf.get("btns", 0) >= 1, pf)
            mem.add_profile_facts(demo["id"], {"city": {"value": ""}})
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.5)

            # 10. toast 真的出现过
            await js(ws, "LawUI.toast('自检')")
            await asyncio.sleep(0.3)
            check("Toast 能弹出",
                  await js(ws, "!!document.querySelector('.law-toast.in')"))

            # 10.5 流式期间发送按钮必须变成「停止」，而且**还能点**。
            #      上一版是 disabled=true，用户想中断只能干等——流断了还要
            #      刷新页面，把已经生成的内容一起丢掉。
            #      这里直接验「状态与动作接没接上」，不真发请求：接口余额为 0
            #      时真发会立刻失败，测出来的红跟这个功能无关。
            await js(ws, "window.__aborted=false;"
                         "window._ctrl={abort:function(){window.__aborted=true;}};"
                         "setSendMode(true);")
            await asyncio.sleep(0.25)
            busy = await js(ws, "(()=>{const b=document.getElementById('send');"
                                 "const s=getComputedStyle(b);"
                                 "return {t:b.textContent.trim(),d:b.disabled,"
                                 "c:b.classList.contains('stop'),"
                                 "bw:s.borderTopWidth,bg:s.backgroundColor};})()")
            check("流式期间按钮写「停止」且可点",
                  busy.get("t") == "停止" and busy.get("d") is False,
                  busy)
            check("「停止」与「提问」长得不一样（描边，不是同一个实心红）",
                  busy.get("c") is True and busy.get("bw") != "0px",
                  f"{busy.get('bw')} / {busy.get('bg')}")
            await click_at(ws, "#send")
            await asyncio.sleep(0.3)
            check("点「停止」真的调了 abort",
                  await js(ws, "window.__aborted") is True)
            await js(ws, "window._ctrl=null;setSendMode(false);")
            await asyncio.sleep(0.2)
            idle = await js(ws, "(()=>{const b=document.getElementById('send');"
                                 "return {t:b.textContent.trim(),d:b.disabled};})()")
            check("停止后按钮复位成「提问」", idle.get("t") == "提问"
                  and idle.get("d") is False, idle)

            # 10.5 回放提示的渲染。
            #      真发一轮问答能顺带验到这条，但那要花掉一次模型调用（在线时
            #      是真金白银），余额为 0 时又是必然的红——两种环境都不适合
            #      放进必跑套件。这里只验「拿到 degraded 事件之后画成什么样」，
            #      分发那一段由服务端日志与一次真实请求人工确认。
            deg = await js(ws, "(()=>{const host=document.createElement('div');"
                               "const ans=document.createElement('div');"
                               "host.appendChild(ans);document.body.appendChild(host);"
                               "handle('degraded',{text:'本轮答案来自离线回放'},"
                               "{answer:ans},{});"
                               "const el=ans.querySelector('.degraded');"
                               "const s=el?getComputedStyle(el):null;"
                               "const r={found:!!el,cls:el?el.className:'',"
                               "txt:el?el.textContent:'',"
                               "bw:s?s.borderLeftWidth:'',"
                               "fs:s?parseFloat(s.fontSize):0};"
                               "host.remove();return r;})()")
            check("degraded 事件渲染成独立提示块",
                  deg.get("found") is True and "degraded" in (deg.get("cls") or ""), deg)
            check("提示块带左侧色条（不是与正文混成一段）",
                  deg.get("bw") not in ("", "0px"), deg.get("bw"))
            check("提示字号小于正文", 0 < deg.get("fs", 0) < 17, deg.get("fs"))
            check("提示文字保留原文", "离线回放" in (deg.get("txt") or ""),
                  (deg.get("txt") or "")[:20])

            # 11. 会话搜索：能搜到、命中词高亮、清空能回到全量。
            #     不测「搜不到的词」——那种用例永远绿，证明不了索引是对的。
            await js(ws, "window.LawNav.history()")
            await asyncio.sleep(1.5)
            title = await js(ws, "document.querySelector('#dlist .di .t')"
                                  " ? document.querySelector('#dlist .di .t')"
                                  ".textContent : ''")
            kw = (title or "")[:2]
            if kw:
                await js(ws, "(()=>{const n=document.getElementById('d-q');"
                             f"n.value={json.dumps(kw)};"
                             "n.dispatchEvent(new Event('input'));})()")
                await asyncio.sleep(1.1)
                hits = await js(ws, "document.querySelectorAll('#dlist .di').length")
                check("搜索能命中已有会话", hits >= 1, f"{kw} → {hits} 条")
                check("命中的词被高亮（结果里能看出为什么匹配）",
                      await js(ws, "!!document.querySelector('#dlist .di mark')"))
                await js(ws, "(()=>{const n=document.getElementById('d-q');"
                             "n.value='';n.dispatchEvent(new Event('input'));})()")
                await asyncio.sleep(1.0)
                check("清空关键词回到全量列表",
                      await js(ws, "document.querySelectorAll('#dlist .di')"
                                   ".length") >= 1)
            else:
                check("搜索能命中已有会话", False, "库里没有历史会话，跳过")

            # 12. 反馈：点一下记下，再点一下撤销。
            #     「点错了改不了」的评价等于噪音，所以撤销是功能不是彩蛋。
            await js(ws, "document.querySelector('#dlist .di .di-main').click()")
            await asyncio.sleep(1.8)
            n_fb = await js(ws, "document.querySelectorAll('.fb-group .fb').length")
            check("答案下有「有用 / 没用」两个按钮", n_fb >= 2, n_fb)
            if n_fb >= 2:
                await js(ws, "document.querySelectorAll('.fb-group .fb')[0].click()")
                await asyncio.sleep(1.2)
                check("点了「有用」会变成已选状态",
                      await js(ws, "document.querySelectorAll('.fb-group .fb')[0]"
                                   ".classList.contains('on')"))
                await js(ws, "document.querySelectorAll('.fb-group .fb')[0].click()")
                await asyncio.sleep(1.2)
                check("再点一次撤销（不留假状态）",
                      not await js(ws, "document.querySelectorAll('.fb-group .fb')[0]"
                                       ".classList.contains('on')"))

            # 13. 状态徽章：点一下必须给出人能看懂的话，而不是没反应
            await js(ws, "document.getElementById('mode').click()")
            await asyncio.sleep(0.5)
            check("状态徽章点了有反馈",
                  await js(ws, "!!document.querySelector('.law-toast.in')"))

            # 14. 左侧一列。这一版把原来散在顶栏和抽屉底部的入口收成平级
            #     入口，还补了两个以前界面上根本点不到的功能（时效计算、
            #     申请书独立面板）。断言要钉住「点得到」而不只是「DOM 里有」。
            #     必须过滤 hidden：「后台管理」对普通账号是 display:none，
            #     但它在 DOM 里仍然是 .nav-i，不过滤就会把 8 当成 7。
            navs = await js(ws, "(()=>[...document.querySelectorAll('.nav-i')]"
                                ".filter(b=>!b.hidden)"
                                ".map(b=>b.textContent.trim()))()")
            # v82 加了「任务」，v104 把「个人档案」从设置里拎进侧栏，
            # v139 又在案卷那组后面补了「法典图书馆 / 自主学习 / 个人主页」：
            # 普通账号看到 11 项（10 个页面 + 设置；「后台管理」只对管理员显示，
            # 所以不能写死 12）。数出来而不是拿常量对常量——写死一个数字加
            # 一个新入口就红，是这套断言唯一的作用。
            check("侧栏十一个入口都在", len(navs) == 11, navs)
            # 三个新入口必须真的在侧栏里、且是按顺序排在一起的：v139 之前
            # 它们连 DOM 都没有，只断言数量的话换成别的入口也能过。
            check("v139 三个新入口在侧栏且顺序正确",
                  ("典法典图书馆" in (navs or []))
                  and ("学自主学习" in (navs or []))
                  and ("我个人主页" in (navs or []))
                  and ((navs or []).index("典法典图书馆")
                       < (navs or []).index("学自主学习")
                       < (navs or []).index("我个人主页")), navs)
            check("「任务」排在最前", (navs or [""])[0].endswith("任务"), navs[:1])
            check("写的是「历史记录」不是「历史」",
                  "史历史记录" in (navs or []), navs)
            check("「管理员后台」收进了设置（顶栏不再挂一个裸链接）",
                  await js(ws, "!document.getElementById('adm')"
                               " && !!document.getElementById('set-adm')"))

            # 14.1 时效计算：以前只有 Agent 内部工具，界面上点不到。
            #      这里测「填 → 算 → 出结论」，尤其是**在职讨薪不给日期**那条：
            #      给一个日期就等于造了一个法律上不存在的期限。
            await js(ws, "window.LawNav.deadline()")
            await asyncio.sleep(0.7)
            check("点「时效计算」切到独立面板",
                  await js(ws, "document.getElementById('p-deadline')"
                               ".classList.contains('on')"
                               " && !document.getElementById('p-chat')"
                               ".classList.contains('on')"))
            ntypes = await js(ws, "document.querySelectorAll('#dl-dispute option')"
                                  ".length")
            check("争议类型不是空下拉框", ntypes >= 7, f"{ntypes} 项")

            # 14.1e 日历常驻（v114）：日历不该等一次计算才出现。
            #      以前它挂在结论卡里，而最常见的一档（讨薪 + 状态「不确定」）
            #      结论是「取决于你是否还在职」，图上没有截止日——用户翻遍
            #      整页看不到一张日历，就会认为这个功能只是张表单。
            n_cal0 = await js(ws, "document.querySelectorAll('#dl-cal .cal-d')"
                                  ".length")
            check("一进页面就有日历（不用先算一次）", n_cal0 >= 28, f"{n_cal0} 格")
            check("没结果时日历给一句「点一天就当天算」",
                  await js(ws, "!!document.querySelector('#dl-cal .cal-hint')"))
            check("没结果时日历上不画截止日",
                  await js(ws, "document.querySelectorAll('#dl-cal .cal-d.dl')"
                               ".length") == 0)
            n_we = await js(ws, "document.querySelectorAll('#dl-cal .cal-d.we')"
                                ".length")
            check("周末在日历上有标记（末日撞周末是最容易漏的一天）",
                  n_we >= 8, f"{n_we} 格")

            # 点格子 = 选起算日 = 直接算。点完还要回去按「计算」的话，
            # 日历就只是个配图，用户仍然在操作表单。
            ok_pick = await js(ws, "(()=>{"
                "var s=document.getElementById('dl-dispute');"
                "s.value='wage';s.dispatchEvent(new Event('change'));"
                "var c=[...document.querySelectorAll('#dl-cal .cal-d')]"
                ".find(x=>x.classList.contains('today'));"
                "if(!c) return false;c.click();return true;})()")
            await asyncio.sleep(1.6)
            v_pick = await js(ws, "document.getElementById('dl-onset').value")
            check("点日历上的今天，起算日跟着变成今天",
                  ok_pick is True and v_pick == (await js(ws, "todayStr()")), v_pick)
            t_pick = (await js(ws, "document.getElementById('dl-out').textContent")) or ""
            check("点完直接出结论（不用再按计算）", len(t_pick) > 20, t_pick[:70])
            check("选中的那一天在日历上有选中标记",
                  await js(ws, "document.querySelectorAll('#dl-cal .cal-d.sel')"
                               ".length") == 1)

            # 14.1h 结论卡插在日历**上方**，所以第一次算完，整张日历会往下跳
            #       一格（约 150px）。日历现在是主要入口，跳一次就意味着
            #       「想点 8/10，结果点到 8/12」。插入内容时要把滚动位置补回去。
            #       先清空回到「没有结论」的状态——已经有结论时再点，高度不变，
            #       这一条会永远为真。
            await click_at(ws, "#dl-reset")
            await asyncio.sleep(0.5)
            await js(ws, "document.getElementById('dl-cal')"
                         ".scrollIntoView({block:'center'})")
            await asyncio.sleep(0.4)
            top0 = await js(ws, "document.getElementById('dl-cal')"
                                ".getBoundingClientRect().top")
            await js(ws, "(()=>{var c=[...document.querySelectorAll"
                         "('#dl-cal .cal-d')].find(x=>x.classList"
                         ".contains('today'));if(c)c.click();})()")
            await asyncio.sleep(1.8)
            top1 = await js(ws, "document.getElementById('dl-cal')"
                                ".getBoundingClientRect().top")
            # 阈值取一格的高度（44）：实测把 275px 的位移补到 27px，剩下那点
            # 补不掉是因为日历本来就靠页面底部、往下滚已经顶到 max（诊断过
            # scrollY==max），不是算法没算。275 那种整卡推下去会被这条逮住。
            check("点完结论出现时，日历不会整体被顶走一格以上",
                  abs((top1 or 0) - (top0 or 0)) <= 44,
                  f"top {top0:.0f} → {top1:.0f}")

            # 14.1f 键盘（v114）：整张月历只占**一个** Tab 位，方向键走格子，
            #       回车才确认。每格都 tabindex=0 的话，键盘用户要按三十八下
            #       才能从日历里出去——那比「点不了」更难受。
            #       走 CDP 真按键，不用合成事件：合成事件不触发默认动作，
            #       焦点不会动，测出来是「一直是同一格」的假通过。
            n_tab0 = await js(ws, "document.querySelectorAll('#dl-cal .cal-d"
                                  "[tabindex=\"0\"]').length")
            check("整张日历只占一个 Tab 位", n_tab0 == 1, f"{n_tab0} 格可 Tab")
            await js(ws, "var c=document.querySelector('#dl-cal .cal-d"
                         "[tabindex=\"0\"]'); if(c) c.focus();")
            iso0 = await js(ws, "document.activeElement.dataset.iso")
            v_before = await js(ws, "document.getElementById('dl-onset').value")
            await press_key(ws, "ArrowRight", "ArrowRight", 39)
            await asyncio.sleep(0.25)
            iso1 = await js(ws, "document.activeElement.dataset.iso")
            # 期望值在 Python 里另算一遍，不在测试里复用页面的 _iso()
            want1 = (_dt.date.fromisoformat(iso0) +
                     _dt.timedelta(days=1)).isoformat() if iso0 else None
            check("方向键右移一天，焦点跟着走", bool(iso1) and iso1 == want1,
                  f"{iso0} → {iso1}")
            # 这条必须和上一条绑在一起看：焦点压根没动时「起算日没变」也成立，
            # 单独跑就是个永远为真的断言。所以先把「焦点确实动了」一起判。
            check("只走格子不改起算日（改了就会每按一下发一次请求）",
                  iso1 != iso0 and
                  (await js(ws, "document.getElementById('dl-onset').value"))
                  == v_before, f"{v_before} 仍在")
            await press_key(ws, "Enter", "Enter", 13)
            await asyncio.sleep(1.5)
            check("回车才把那一天设为起算日",
                  (await js(ws, "document.getElementById('dl-onset').value"))
                  == iso1, iso1)
            m_before = await js(ws, "document.querySelector('#dl-cal .cal-t')"
                                    ".textContent")
            await press_key(ws, "PageDown", "PageDown", 34)
            await asyncio.sleep(0.4)
            m_after = await js(ws, "document.querySelector('#dl-cal .cal-t')"
                                   ".textContent")
            check("PageDown 翻到下个月", m_before != m_after,
                  f"{m_before} → {m_after}")
            check("翻月后焦点仍在日历里（没有掉到 body）",
                  await js(ws, "!!document.activeElement.closest('#dl-cal')"))

            # 14.1g 点到未来：日历可点之后，「点错年份」变成一次点击就能做到的事。
            #       后端对这条有一道明确拒绝（起算日晚于今天），前端不能把它
            #       当正常结果画出来——否则会算出一个还没开始的「期限」。
            for _ in range(12):
                await press_key(ws, "PageDown", "PageDown", 34)
            await asyncio.sleep(0.4)
            iso_far = await js(ws, "(()=>{var c=document.querySelector("
                                   "'#dl-cal .cal-d[tabindex=\"0\"]');"
                                   "if(!c)return null;c.click();"
                                   "return c.dataset.iso;})()")
            await asyncio.sleep(1.8)
            t_far = (await js(ws, "document.getElementById('dl-out')"
                                    ".textContent")) or ""
            check("点到一年后：明说算不出来，不画一个还没开始的期限",
                  bool(iso_far) and "起算日晚于今天" in t_far,
                  f"{iso_far} → {t_far[:40]}")
            check("点到一年后：日历上的旧截止日被撤掉（不留两个答案）",
                  await js(ws, "document.querySelectorAll('#dl-cal .cal-d.dl')"
                               ".length") == 0)

            await js(ws, "(()=>{const s=document.getElementById('dl-dispute');"
                         "s.value='wage';"
                         "s.dispatchEvent(new Event('change'));"
                         "document.getElementById('dl-onset').value='2025-09-01';"
                         "[...document.querySelectorAll('#dl-rel button')]"
                         ".find(b=>b.dataset.v==='no').click();})()")
            await asyncio.sleep(0.4)
            check("选「还在职」后不问终止日期（问了也没有意义）",
                  await js(ws, "document.getElementById('dl-end-wrap').hidden") is True)
            await click_at(ws, "#dl-go")
            await asyncio.sleep(2.0)
            t_on = (await js(ws, "document.getElementById('dl-out').textContent")) or ""
            check("在职讨薪算出来是「不受一年时效限制」，不是一个日期",
                  "不受一年时效限制" in t_on, t_on[:70])

            await js(ws, "[...document.querySelectorAll('#dl-rel button')]"
                         ".find(b=>b.dataset.v==='yes').click();"
                         "document.getElementById('dl-end').value='2024-06-15';")
            await asyncio.sleep(0.4)
            check("选「已离职」才出现终止日期输入",
                  await js(ws, "document.getElementById('dl-end-wrap').hidden") is False)
            await click_at(ws, "#dl-go")
            await asyncio.sleep(2.0)
            t_off = (await js(ws, "document.getElementById('dl-out').textContent")) or ""
            check("离职讨薪从「终止之日」起算一年（2024-06-15 → 2025-06-15）",
                  "2025-06-15" in t_off, t_off[:70])
            check("过期了就说过期，不装成还剩几天",
                  "已过期" in t_off, t_off[:70])

            # 14.1c 时效日历（v108）：结论是数字，日历是位置——「还剩 87 天」
            #       和「这 87 天落在哪几格」是两件事。过期场景默认翻到截止日
            #       那个月，所以红块和过期区间都该在。
            await asyncio.sleep(0.4)
            n_cell = await js(ws, "document.querySelectorAll('.cal-d').length")
            check("时效结果带月历", n_cell >= 28, f"{n_cell} 格")
            check("月历标出截止日",
                  await js(ws, "document.querySelectorAll('.cal-d.dl').length") == 1)
            check("已过期：截止日之后的日子标成过期区间",
                  await js(ws, "document.querySelectorAll('.cal-d.past').length") > 0)

            # 未过期场景：今天要圈出来，今天到截止日之间连成一段。
            # 起止日期用「今天减 30 天」现算，不写死——写死的话这条断言在
            # 某个具体日期之后会自己变红，而红的原因和代码无关。
            await js(ws, "(()=>{var d=new Date();d.setDate(d.getDate()-30);"
                         "var s=_iso(d);"
                         "document.getElementById('dl-onset').value=s;"
                         "document.getElementById('dl-end').value=s;})()")
            await click_at(ws, "#dl-go")
            await asyncio.sleep(1.8)
            check("未过期：今天在月历上被圈出来",
                  await js(ws, "document.querySelectorAll('.cal-d.today').length") == 1)
            check("未过期：今天到截止日之间连成一段",
                  await js(ws, "document.querySelectorAll('.cal-d.range').length") > 0)
            check("截止日不在本月时给跳转入口",
                  await js(ws, "document.querySelectorAll('.cal-jump a').length") >= 1)

            # 14.1d 「先填终止日期，再点回还在职」：那个输入框只是被隐藏，
            #       值还在。不拦住的话后端拿残留值算出一个截止日——用户点了
            #       在职，拿到的却是离职的结论。
            await js(ws, "(()=>{var s=document.getElementById('dl-dispute');"
                         "s.value='wage';s.dispatchEvent(new Event('change'));"
                         "document.querySelectorAll('#dl-rel button').forEach("
                         "function(b){if(b.dataset.v==='yes')b.click();});})()")
            await asyncio.sleep(0.3)
            await js(ws, "document.getElementById('dl-end').value='2025-06-30'")
            await js(ws, "document.querySelectorAll('#dl-rel button').forEach("
                         "function(b){if(b.dataset.v==='no')b.click();});")
            await asyncio.sleep(0.3)
            await click_at(ws, "#dl-go")
            await asyncio.sleep(1.8)
            t_stay = (await js(ws, "document.getElementById('dl-out').textContent")) or ""
            check("在职时残留的终止日期不会把结论改成离职",
                  "不受一年时效限制" in t_stay, t_stay[:70])

            # 14.1b 本库没有的期限，必须明说没有，而不是套一个通用数字。
            #      工伤走的是《工伤保险条例》，语料里没有这部法——编一个
            #      「一年」出来，用户会照着去准备材料。
            await js(ws, "(()=>{const s=document.getElementById('dl-dispute');"
                         "s.value='injury';"
                         "s.dispatchEvent(new Event('change'));"
                         "document.getElementById('dl-go')"
                         ".scrollIntoView({block:'center'});})()")
            await asyncio.sleep(0.4)
            # 先确认按钮真的可点。底部的输入区是 fixed，上一轮结论卡插进来
            # 之后内容变长，按钮可能被它盖住——那时坐标点击会落到输入框上，
            # 看起来像「功能坏了」，实际是「点不到」。用户在真机上也会这样。
            hit = await js(ws, "(()=>{const g=document.getElementById('dl-go');"
                               "const b=g.getBoundingClientRect();"
                               "const e=document.elementFromPoint("
                               "b.x+b.width/2,b.y+b.height/2);"
                               "return (e===g||g.contains(e))?'ok'"
                               ":(e?(e.id||e.className||e.tagName):'null');})()")
            check("工伤：计算按钮没被底部输入区盖住", hit == "ok", str(hit))
            await click_at(ws, "#dl-go")
            await asyncio.sleep(1.6)
            t_inj = (await js(ws, "document.getElementById('dl-out').textContent")) or ""
            check("工伤：明说本库未收录，不编一个日期出来",
                  "未收录" in t_inj, t_inj[:70])

            # 14.1i 图片证据（多模态）：读图 → 逐项确认 → 只写勾中的。
            #       这条链路的要害不是「能不能读出来」，是「读错的时候能不能被
            #       人看见」。所以这里的断言有一半在管「不确定的字段不预设勾选」，
            #       而不只是「读到了几个字段」。
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.8)
            ev_sid = "ui-check-ev"
            mem.ensure_session(ev_sid, demo["id"])
            created.append(ev_sid)
            await js(ws, f"sessionId = '{ev_sid}'")

            check("输入区有传图入口",
                  await js(ws, "!!document.getElementById('ev-open')"))
            await js(ws, "document.getElementById('ev-open').click()")
            await asyncio.sleep(0.5)
            check("点开先是「选一张图」的引导，不是空面板",
                  await js(ws, "!!document.querySelector('#ev-body .ev-drop')"))

            # 现造一张 PNG，走完整链路：FileReader → 接口 → 渲染。
            # 不用 CDP 的 setFileInputFiles：那要先落一个真文件，
            # 而这张图是当场生成的，写盘再读只是多一步 IO 和一处要清理的临时文件。
            await js(ws, f"""(async()=>{{
              const b64='{PNG_1PX}';
              const bin=atob(b64);
              const u8=new Uint8Array(bin.length);
              for(let i=0;i<bin.length;i++) u8[i]=bin.charCodeAt(i);
              readEvidence(new File([u8],'t.png',{{type:'image/png'}}));
              await new Promise(r=>setTimeout(r,1700));
            }})()""")
            n_row = await js(ws, "document.querySelectorAll('#ev-body .ev-row').length")
            n_chk = await js(ws, "[...document.querySelectorAll('#ev-body input')]"
                                 ".filter(c=>c.checked).length")
            check("读出来的字段逐项列出", n_row == 3, f"{n_row} 项")
            check("只有高置信度的默认勾上", n_chk == 2, f"{n_chk} 项")
            check("图不清楚的那一项不预设勾选（要人主动去勾）",
                  await js(ws, "document.querySelectorAll('#ev-body .ev-low')"
                               ".length") == 1)
            check("每项都摊出「图中原文」——用户判断全靠这一行",
                  await js(ws, "(document.querySelector('#ev-body .ev-q')||{})"
                               ".textContent.indexOf('图中原文：') === 0"))
            check("上传的图能看见（确认没拿错一张）",
                  await js(ws, "!!document.querySelector('#ev-body img')"))
            check("没认出来的地方明说，不闷着",
                  await js(ws, "(document.querySelector('#ev-body .ev-note')||{})"
                               ".textContent.indexOf('没敢认') >= 0"))

            await js(ws, "document.getElementById('ev-go').click()")
            await asyncio.sleep(1.8)
            check("导入后面板自己关掉",
                  await js(ws, "document.getElementById('ev-mask').hidden"))
            _sl_raw = await js(ws, f"""(async()=>{{
              const r=await fetch('/api/chat/session/{ev_sid}');
              const d=await r.json();
              return JSON.stringify(d.slots||{{}});
            }})()""")
            _sl = json.loads(_sl_raw) if isinstance(_sl_raw, str) else {}
            check("勾中的两项真的写进了槽位",
                  _sl.get("monthly_wage") == 12000
                  and _sl.get("start_date") == "2023-04", str(_sl))
            # 整段最要紧的一条：没勾的那项**不许**跟着进去。
            # 少一项只是少一个字段，下次追问就补上了；多一项错的值会被
            # 当成既定事实一路算进赔偿金额，而用户永远不知道它错在哪。
            check("没勾的那项没跟着混进去",
                  "leave_reason" not in _sl, str(_sl))

            # 14.2 申请人书面板：以前只能从「案情档案」抽屉底部进去，
            #      而那个按钮要求「先有会话」——没会话的人连入口都摸不到。
            await js(ws, "window.LawNav.doc()")
            await asyncio.sleep(1.2)
            check("点「仲裁申请书」切到独立面板",
                  await js(ws, "document.getElementById('p-doc')"
                               ".classList.contains('on')"))
            # v82 起下拉列的是任务不是会话（一个案子的多次咨询要合并用）。
            # 没有任务时也应至少有一个提示项，而不是空的下拉。
            ntask = await js(ws, "document.querySelectorAll('#doc-task option').length")
            check("申请书面板有任务选择器（没任务也有提示）", ntask >= 1, f"{ntask} 项")

            # 14.3 思考过程默认收起。
            #      流式期间必须展开（否则页面像卡死），done 之后自动折成一行。
            #      这条只能靠真发一次提问验证：折叠是 done 事件驱动的，
            #      手动构造节点测不到「事件有没有接上」。
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.4)
            before = await js(ws, "window.sessionId || ''")
            await js(ws, "(()=>{const n=document.getElementById('q');"
                         "n.value='公司拖欠我两个月工资，也没签合同怎么办';"
                         "n.dispatchEvent(new Event('input'));})()")
            await click_at(ws, "#send")
            live = None
            for _ in range(60):        # 最多等 30s
                await asyncio.sleep(0.5)
                # 取**最后**一轮：feed 里前面还有从历史记录还原的轮次，
                # 拿第一个测到的是历史那行「未保留轨迹」，跟这次发送无关。
                live = await js(ws, "(()=>{const hs=[...document.querySelectorAll("
                                    "'.trace-head')];const h=hs[hs.length-1];"
                                    "if(!h)return null;"
                                    "const b=h.parentElement.querySelector("
                                    "'.trace-body');"
                                    "return {exp:h.getAttribute('aria-expanded'),"
                                    "hidden:b?b.hidden:null,"
                                    "sum:h.textContent.trim()};})()")
                if live and live.get("exp") == "false":
                    break
            after = await js(ws, "window.sessionId || ''")
            if after and after != before:
                created.append(after)
            check("流式结束后思考过程自动收起", bool(live)
                  and live.get("exp") == "false" and live.get("hidden") is True, live)
            check("收起后那行摘要写了步数（能看出到底查没查条文）",
                  bool(live) and "步" in (live.get("sum") or ""), live)
            if live and live.get("exp") == "false":
                await js(ws, "(()=>{const hs=[...document.querySelectorAll("
                             "'.trace-head')];hs[hs.length-1].click();})()")
                await asyncio.sleep(0.4)
                re_exp = await js(ws, "(()=>{const hs=[...document.querySelectorAll("
                                      "'.trace-head')];const h=hs[hs.length-1];"
                                      "const b=h.parentElement.querySelector("
                                      "'.trace-body');return {exp:h.getAttribute("
                                      "'aria-expanded'),hidden:b.hidden};})()")
                check("点一下能展开（默认收起不等于藏起来）",
                      re_exp.get("exp") == "true" and re_exp.get("hidden") is False,
                      re_exp)
                await js(ws, "(()=>{const hs=[...document.querySelectorAll("
                             "'.trace-head')];hs[hs.length-1].click();})()")
                await asyncio.sleep(0.3)
                check("再点一下收回",
                      await js(ws, "(()=>{const hs=[...document.querySelectorAll("
                                   "'.trace-head')];return hs[hs.length-1]"
                                   ".getAttribute('aria-expanded');})()") == "false")

            # 15. Esc 只关最上面那一层。
            #     老实现是「按固定顺序挨个试」：先试设置、再试抽屉、再试弹窗……
            #     层一多就会关错——申请书弹窗开着，Esc 把下面的案情面板关了。
            #     现在改成入栈/出栈，关谁由打开的那一刻决定，这里钉这个顺序。
            #
            #     先空按几下把前面用例可能留着的层清干净：栈空时 closeTopLayer
            #     返回 false，什么都不做，所以这一步本身无副作用。
            for _ in range(4):
                await press_key(ws, "Escape", "Escape", 27)
                await asyncio.sleep(0.25)

            # v104：设置不再占用 Alt 序号（个人档案挤进了侧栏，Alt 序列
            # 只剩 1..7 七个页面）。从侧栏那一项点进去，是为了让「关完焦点
            # 回到打开它的按钮」这条断言仍然有意义。
            await click_at(ws, ".nav-i[data-open=settings]")
            await asyncio.sleep(0.7)
            check("侧栏「设置」能打开设置",
                  await js(ws, "document.getElementById('setdlg').hidden") is False)
            await click_at(ws, "#set-pwd")
            await asyncio.sleep(0.6)
            check("从设置里再叠一层（改密码）",
                  await js(ws, "document.getElementById('pwd').hidden") is False)

            await press_key(ws, "Escape", "Escape", 27)
            await asyncio.sleep(0.5)
            st = await js(ws, "({pwd:document.getElementById('pwd').hidden,"
                              "set:document.getElementById('setdlg').hidden})")
            check("Esc 只关最上面那层，下面的设置还在",
                  st.get("pwd") is True and st.get("set") is False, st)

            await press_key(ws, "Escape", "Escape", 27)
            await asyncio.sleep(0.5)
            st2 = await js(ws, """(()=>{const a=document.activeElement;
              return {set:document.getElementById('setdlg').hidden,
                      act:(a.dataset&&a.dataset.open)||a.id||a.tagName};})()""")
            check("再按一次 Esc 才关设置", st2.get("set") is True, st2)
            check("关完焦点回到打开它的侧栏按钮（不掉到 body）",
                  st2.get("act") == "settings", st2)

            # 16. 帮助层优先。帮助面板自己也监听 Esc（ui.js，气泡阶段）。
            #     主脚本若在 capture 阶段抢跑，一次 Esc 会连下层页面一起切走。
            await press_key(ws, "6", "Digit6", 54, modifiers=1)      # Alt+6
            await asyncio.sleep(0.8)
            check("Alt+6 打开历史记录",
                  await js(ws, "document.getElementById('p-history')"
                               ".classList.contains('on')") is True)
            # 帮助层只能用键盘开：页面本身不挡顶栏，但这颗「?」按钮在窄屏会被收掉。
            # 也正因为这样，它必须自己能从 Esc 里抢到一次——否则下面那层先被关掉，
            # 用户按十下 Esc，眼前这个面板一动不动。
            await js(ws, "document.activeElement && document.activeElement.blur()")
            await press_key(ws, "?", "Slash", 191, modifiers=8)      # Shift+/
            await asyncio.sleep(0.6)
            check("历史页开着时按 ? 也能调出快捷键面板",
                  await js(ws, "LawUI.helpOpen()") is True)
            # 新增的键必须写进面板：快捷键只活在代码里，用户就永远不知道。
            # 这一版加了 Alt+1..7，面板里没有的话等于白加。
            check("快捷键面板里写了 Alt+1..7（不是只写在代码里）",
                  "七个入口直达" in ((await js(
                      ws, "document.querySelector('.law-help-sheet')"
                          ".textContent")) or ""))
            await press_key(ws, "Escape", "Escape", 27)
            await asyncio.sleep(0.5)
            hp = await js(ws, "({help:LawUI.helpOpen(),"
                              "hist:document.getElementById('p-history')"
                              ".classList.contains('on')})")
            check("帮助开着时 Esc 只关帮助，不顺手把历史页切走",
                  hp.get("help") is False and hp.get("hist") is True, hp)

            # 17. Alt+1..7 七个入口全通。光标留在输入框里也要生效——
            #     问完一句光标还在框里，按 Alt+4 应该直接跳去写申请书。
            #     匹配 e.code 而非 e.key：换键盘布局时 Alt+1 可能产出 ¡。
            await js(ws, "document.getElementById('q').focus()")
            got = {}
            for k, code, vk, panel in (("1", "Digit1", 49, "tasks"),
                                       ("2", "Digit2", 50, "chat"),
                                       ("3", "Digit3", 51, "deadline"),
                                       ("4", "Digit4", 52, "doc")):
                await press_key(ws, k, code, vk, modifiers=1)
                await asyncio.sleep(0.7)
                got[panel] = bool(await js(
                    ws, f"document.getElementById('p-{panel}').classList.contains('on')"
                        " && document.querySelectorAll('.view.on').length === 1"))
            check("Alt+1/2/3/4 在输入框里也能切面板，且只亮一个",
                  all(got.values()), got)

            # v104 之后 Alt+5/6/7 分别是个人档案 / 历史记录 / 案情档案，
            # 三者都是独立页面（原来 5 是案情、6 是历史，且都是抽屉）。
            for k, code, vk, panel in (("5", "Digit5", 53, "profile"),
                                       ("6", "Digit6", 54, "history"),
                                       ("7", "Digit7", 55, "slots")):
                await press_key(ws, k, code, vk, modifiers=1)
                await asyncio.sleep(0.8)
                got[panel] = bool(await js(
                    ws, f"document.getElementById('p-{panel}').classList.contains('on')"
                        " && document.querySelectorAll('.view.on').length === 1"))
            check("Alt+5/6/7 切到个人档案 / 历史记录 / 案情档案",
                  all(got.get(p) for p in ("profile", "history", "slots")), got)

            # 17b. 切页过场（v108）：先盖 0.7–1.0s 载入遮罩（9 个场景随机），
            #   揭开时目标页分层淡入。
            #   为什么查：这套是**静默失败**的——遮罩没挂上、或 pg-in 没写进去，
            #   页面只是「硬切」，控制台不报错、页面上也不报错，只能靠对账。
            #   同时要盯住 .view.on 的唯一性：整个 check_ui 靠它判断当前在哪页，
            #   一旦同时亮两个，后面几十条断言全部错位。
            check("页面上有切页载入遮罩",
                  await js(ws, "!!document.getElementById('pgt')") is True)
            check("遮罩里是 8–9 个场景",
                  (await js(ws, "document.querySelectorAll('#pgt .pgt-scene').length")) >= 8)
            # 上面 Alt+7 那一段的过场还没播完（0.7–1.0s），先等它收官，
            # 否则这一按会被 _pgtCancel 当成「连按」直接瞬时换页、压根不盖遮罩。
            for _ in range(120):
                if not await js(ws, "window._pgtBusy"):
                    break
                await asyncio.sleep(0.1)
            t_pgt = time.time()
            await press_key(ws, "1", "Digit1", 49, modifiers=1)
            await asyncio.sleep(0.3)          # 遮罩展示期内（0.7–1.0s 才揭开）
            check("切页时载入遮罩已盖上",
                  await js(ws, "document.getElementById('pgt').classList.contains('on')") is True)
            check("过场进行中仍只亮一个页面",
                  await js(ws, "document.querySelectorAll('.view.on').length") == 1)
            # 等遮罩真的揭开，而不是靠固定 sleep 猜时长——时长一改（或机器一慢）
            # 固定等待就会假红，而假红的门禁最后一定被人关掉。
            for _ in range(120):
                if not await js(ws, "window._pgtBusy"):
                    break
                await asyncio.sleep(0.05)
            # 时长本身也是需求：「一秒左右」。v105 那版是 3–4 秒，好看但挡路；
            # 上限放到 1600ms 是给遮罩（≤1000）+ 分层淡入（≤380）+ 轮询误差留的。
            # 只测「揭开了」不测多久，就等于把这条需求交给了下一个人记。
            el_ms = (time.time() - t_pgt) * 1000
            check("过场总时长在一秒左右", el_ms < 1600, f"{el_ms:.0f}ms")
            check("遮罩揭开后已隐藏",
                  await js(ws, "document.getElementById('pgt').classList.contains('on')") is False)
            # 揭开的同一拍，目标页的版面块被挂上 pg-in；没有它揭开就是硬闪。
            # 看 computed animationName，不看 inline 字符串：inline 是 shorthand，
            # 序列化结果在各浏览器上不一致，拿它做断言容易假红。
            check("揭开后目标页已分层淡入（版面块挂着 pg-in）",
                  await js(ws, "(()=>{const b=document.getElementById('p-tasks');"
                               "const s=b.querySelector('.pad')||b.querySelector('.wrap')||b;"
                               "return [].some.call(s.children,function(e){"
                               "return getComputedStyle(e).animationName.indexOf('pg-in')>=0})})()") is True)
            check("遮罩与分层淡入之后仍只亮一个页面",
                  await js(ws, "document.querySelectorAll('.view.on').length") == 1)

            # 18. 模态框开着时按快捷键，不许把弹窗丢在原地。
            #     v104 起 Alt+7 是「案情档案」页面，不再是设置。所以这里验的是：
            #     改密码 + 设置两层都开着时按导航快捷键，切页面必须把两层一起收掉；
            #     漏收的话用户会看到一个弹窗悬在档案页上，还得再按 Esc 才能走。
            #     （这条以前抓的是「closePwd 把设置弹回来」的幽灵层。）
            await js(ws, "window.LawNav.settings()")
            await asyncio.sleep(0.6)
            await click_at(ws, "#set-pwd")
            await asyncio.sleep(0.6)
            await press_key(ws, "7", "Digit7", 55, modifiers=1)      # 改密码开着时 Alt+7
            await asyncio.sleep(0.6)
            st3 = await js(ws, "({set:document.getElementById('setdlg').hidden,"
                               "pwd:document.getElementById('pwd').hidden,"
                               "slots:document.getElementById('p-slots')"
                               ".classList.contains('on')})")
            check("改密码开着时按导航键，两层弹窗一起收掉并切到目标页",
                  st3.get("pwd") is True and st3.get("set") is True
                  and st3.get("slots") is True, st3)
            await press_key(ws, "Escape", "Escape", 27)
            await asyncio.sleep(0.5)
            st4 = await js(ws, "({set:document.getElementById('setdlg').hidden,"
                               "pwd:document.getElementById('pwd').hidden})")
            check("关掉之后没有幽灵层弹回来",
                  st4.get("set") is True and st4.get("pwd") is True, st4)

            # 18.5 首屏徽章的兜底：把 /api/status 掐掉再看它还说不说「在线」。
            #      原来首屏直接按 meta.mode 上绿色，等 /api/status 回来才改。
            #      状态接口一旦失败（超时、代理拦、登录态刚失效），那颗绿就永远
            #      留着——而它只代表「配置里有 key」。
            meta0 = json.load(urllib.request.urlopen(base + "/api/meta", timeout=5))
            await send(ws, "Network.setBlockedURLs", {"urls": ["*/api/status*"]})
            await js(ws, "location.reload()")
            await asyncio.sleep(2.2)
            badge = await js(ws, "(()=>{const b=document.getElementById('mode');"
                                 "return {t:b.textContent,c:b.className};})()")
            if meta0.get("llm_live") == "error":
                check("状态接口不可用时徽章也不冒充在线",
                      "bad" in (badge.get("c") or "")
                      and badge.get("t") != "LLM 在线", badge)
            else:
                # 服务健康时这条无从验：不写进用例数，免得「跳过」被当成通过
                print(f"  [SKIP] 状态接口不可用时的徽章（当前是 {meta0.get('llm_live')}）")
            await send(ws, "Network.setBlockedURLs", {"urls": []})
            await js(ws, "location.reload()")
            await asyncio.sleep(1.8)

            # 18.5 办案包（v116）：接口 → SSE 事件分发 → 三块渲染 → 勾选复制。
            #      不真发一问，避免为了验 UI 再建一条会话；直接用接口拿真实数据，
            #      再走 handle('extras') 这条前端真路径。真发问题的链路会顺带
            #      在库里留会话，清理成本比这点覆盖不值。
            check("办案包脚本没有被前面的异常截断",
                  await js(ws, "typeof handle === 'function' && "
                               "typeof renderExtras === 'function' && "
                               "typeof reloadExtras === 'function'") is True)

            seed = await js(ws, """(async()=>{
              const r = await fetch('/api/case/extras', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({question:'公司没签劳动合同，还拖欠工资',
                                      slots:{monthly_wage:12000}})});
              const d = await r.json();
              return {status:r.status, keys:Object.keys(d),
                      nodes:((d.timeline||{}).nodes||[]).length,
                      items:((d.claims||{}).items||[]).length,
                      rows:((d.evidence_check||{}).items||[]).length};
            })()""")
            check("办案包接口返回五块",
                  seed.get("status") == 200 and
                  {"timeline", "claims", "evidence_check",
                   "decision", "persona"} <= set(seed.get("keys") or []),
                  seed)
            check("接口给的时间轴/请求/举证都不空",
                  seed.get("nodes") == 8 and seed.get("items", 0) >= 1 and
                  seed.get("rows") == 6, seed)

            # 独立判例接口：只要决策卡，不拉整包。最关键的是它和整包里的
            # decision 必须逐字段相同 —— 两套口径一旦长歪，页面和 MCP
            # 会给出不一样的结局计数，而两边都不会报错。
            dd = await js(ws, """(async()=>{
              const body = JSON.stringify({question:'公司没签劳动合同，还拖欠工资',
                                           slots:{monthly_wage:12000}});
              const opt = {method:'POST', headers:{'Content-Type':'application/json'}, body};
              const a = await fetch('/api/case/decision', opt);
              const d = await a.json();
              const b = await fetch('/api/case/extras', opt);
              const ex = await b.json();
              const c = await fetch('/api/case/decision', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({question:'x', session_id:'not-my-session'})});
              return {status:a.status, keys:Object.keys(d),
                      same: JSON.stringify(d) === JSON.stringify(ex.decision),
                      cases:((d.similar||{}).items||[]).length,
                      rows:((d.cost_benefit||{}).rows||[]).length,
                      noWin: !!(d.policy||{}).no_win_rate,
                      local: ((d.scope||{}).source||{}).label || '',
                      forbidden:c.status};
            })()""")
            check("独立判例接口只返回决策卡且与整包 decision 逐字段一致",
                  dd.get("status") == 200 and
                  set(dd.get("keys") or []) ==
                  {"scope", "similar", "cost_benefit", "policy"} and
                  dd.get("same") is True, dd)
            check("独立判例接口仍有相似案件/成本行/无胜率/未联网标注",
                  dd.get("cases", 0) >= 1 and dd.get("rows") == 6 and
                  dd.get("noWin") is True and
                  "本地判例库" in (dd.get("local") or ""), dd)
            check("独立判例接口同样挡住越权会话",
                  dd.get("forbidden") == 403, dd)

            painted = await js(ws, """(async()=>{
              const r = await fetch('/api/case/extras', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({question:'公司没签劳动合同，还拖欠工资',
                                      slots:{monthly_wage:12000}})});
              const d = await r.json();
              window.__exData = d;
              const ctx = addTurn('办案包自检问题');
              window.__exCtx = ctx;
              handle('extras', d, ctx, {});
              const q = s => document.querySelectorAll('#feed .extras ' + s).length;
              return {boxes:q(''), secs:q('.ex-sec'), nodes:q('.ex-node'),
                      claims:q('.ex-claim'), rows:q('.ex-row'),
                      checked:q('.ex-cb:checked'), todo:q('.ex-tag.todo'),
                      blank:(document.querySelector('#feed .extras .ex-ctext')||{}).textContent||'',
                      reload:q('.ex-foot button')};
            })()""")
            check("SSE extras 事件渲染出办案包",
                  painted.get("boxes") == 1 and painted.get("secs") == 5, painted)
            check("时间轴 8 个阶段、举证 6 行都上了屏",
                  painted.get("nodes") == 8 and painted.get("rows") == 6, painted)
            check("请求事项默认一条都不勾", painted.get("checked") == 0, painted)
            check("请求事项金额在页面上仍然是留空",
                  "＿＿＿＿" in painted.get("blank", ""), painted.get("blank"))
            check("举证体检标出了待补项", painted.get("todo", 0) >= 1, painted)
            check("重算按钮在位", painted.get("reload") == 1, painted)

            copied = await js(ws, """(()=>{
              let got = null;
              const raw = LawUI.copy;
              LawUI.copy = function(t){ got = t; return true; };
              const cbs = document.querySelectorAll('#feed .extras .ex-cb');
              if (cbs.length) cbs[0].checked = true;
              const btn = Array.from(document.querySelectorAll('#feed .extras button'))
                            .find(b => b.textContent.indexOf('复制勾中') >= 0);
              if (btn) btn.click();
              LawUI.copy = raw;
              const all = Array.from(document.querySelectorAll('#feed .extras .ex-ctext'))
                            .map(n => n.textContent);
              const rest = all.slice(1);
              return {hasPicked: !!got && got.indexOf(all[0]) >= 0,
                      leaked: got ? rest.filter(s => got.indexOf(s) >= 0) : [],
                      n:all.length};
            })()""")
            check("只复制勾中的那一条（没勾的不跟着混进去）",
                  copied.get("hasPicked") is True and not copied.get("leaked"),
                  copied)

            again = await js(ws, """(()=>{
              handle('extras', window.__exData, window.__exCtx, {});
              return document.querySelectorAll('#feed .extras').length;
            })()""")
            check("同一轮重算不会叠出第二块办案包", again == 1, again)

            # 18.6 判例参考与画像（v117）：只数事实，不算胜率；立场默认未选。
            dec = await js(ws, """(()=>{
              const root = document.querySelector('#feed .extras');
              if (!root) return {};
              const txt = root.textContent || '';
              const sel = root.querySelector('#ex-stance');
              const plain = root.querySelector('#ex-plain');
              return {
                cases: root.querySelectorAll('.dc-case').length,
                stats: Array.from(root.querySelectorAll('.dc-stat b')).map(n=>n.textContent),
                trs: root.querySelectorAll('.dc-tr').length,
                links: root.querySelectorAll('.dc-link').length,
                quote: (root.querySelector('.dc-quote')||{}).textContent || '',
                txt: txt,
                disclaim: txt.indexOf('仅供参考，不构成法律意见') >= 0,
                local: txt.indexOf('本地判例库，未联网') >= 0,
                stance: !!sel, stanceValue: sel ? sel.value : 'missing',
                plain: !!plain, plainChecked: plain ? plain.checked : null,
                blank: txt.indexOf('＿＿＿＿') >= 0
              };
            })()""")
            check("判例卡列出相似案件并附官方处理结果原文",
                  dec.get("cases", 0) >= 1 and
                  "处理结果（官方原文）" in dec.get("quote", ""), dec)
            check("结局分布三个计数都上了屏", len(dec.get("stats") or []) == 3, dec)
            check("成本收益六行都上了屏", dec.get("trs") == 6, dec)
            check("每件案例都有官方来源链接",
                  dec.get("links", 0) == dec.get("cases", 0) and dec.get("links", 0) >= 1, dec)
            # v137：办案包里现在有法定费率（「按标的 10%–20%」「2.5%」），
            # 判据从「一个 % 都没有」收窄成「没有费率以外的 %」——守的仍是
            # 「不给胜诉率」这条红线，只是不再误伤正确内容。
            check("页面上不给任何百分比胜率",
                  not _bad_pct(dec.get("txt") or ""),
                  _bad_pct(dec.get("txt") or "")[:3])
            check("办案包只留一句免责",
                  dec.get("disclaim") is True, dec)
            check("来源标注如实说明未联网", dec.get("local") is True, dec)
            check("金额行仍是留空", dec.get("blank") is True, dec)
            check("立场控件存在且默认未选择",
                  dec.get("stance") is True and dec.get("stanceValue") == "", dec)
            check("简洁表述默认不自动开启",
                  dec.get("plain") is True and dec.get("plainChecked") is False, dec)

            # 真改一次立场：PUT 保存 → 重算 → applicant 过滤跟着变。
            switched = await js(ws, """(async()=>{
              const sel = document.querySelector('#feed .extras #ex-stance');
              if (!sel) return {};
              const put = await fetch('/api/persona', {
                method:'PUT', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({stance:'employer', confirmed:true})});
              const saved = await put.json();
              const r = await fetch('/api/case/extras', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({question:'竞业限制违约金'})});
              const d = await r.json();
              return {put:put.status, saved:(saved.facts||{}).stance,
                      applicant:((d.decision||{}).scope||{}).applicant,
                      insufficient:(((d.decision||{}).similar||{}).distribution||{}).insufficient};
            })()""")
            check("显式选单位视角后落库并改变判例过滤",
                  switched.get("put") == 200 and switched.get("saved") == "employer"
                  and switched.get("applicant") == "employer", switched)
            check("单位视角样本不足时如实提示",
                  switched.get("insufficient") is True, switched)
            check("非法立场被接口拒绝",
                  await js(ws, """(async()=>{
                    const r = await fetch('/api/persona', {
                      method:'PUT', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({stance:'瞎编的'})});
                    return r.status;
                  })()""") == 400)

            # 18.7 v118：网络自动检测 + 「点了才联网」。
            # 这一组最要紧的是默认路径：一次外部请求都不许发，且页面要如实
            # 写出为什么没联网，而不是留一块空白让人以为功能坏了。
            netv = await js(ws, """(async()=>{
              const meta = await (await fetch('/api/meta')).json();
              const st = await (await fetch('/api/status')).json();
              const nw = await (await fetch('/api/network')).json();
              const root = document.querySelector('#feed .extras');
              const r = await fetch('/api/case/decision', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({question:'没签劳动合同，还拖欠工资',
                                      external:1})});
              const d = await r.json();
              return {
                metaNet: !!meta.network, metaKeys: Object.keys(meta.network||{}),
                statusNet: !!(st.network && st.network.label),
                netApi: !!(nw.network && nw.network.mode),
                extEnabled: nw.external_search_enabled,
                netLine: (root.querySelector('.dc-net')||{}).textContent || '',
                btn: !!root.querySelector('.dc-ext .btn'),
                itemsBefore: root.querySelectorAll('.dc-ext-item').length,
                casesBefore: root.querySelectorAll('.dc-case').length,
                decisionStatus: r.status,
                extStatus: (((d.scope||{}).external)||{}).status,
                keys: Object.keys(d).sort(),
                local: (((d.scope||{}).source)||{}).label
              };
            })()""")
            check("meta / status / network 三处都带自动检测结果",
                  netv.get("metaNet") is True and netv.get("statusNet") is True
                  and netv.get("netApi") is True, netv)
            check("meta 只给结论，不暴露探测目标主机名",
                  "online" in (netv.get("metaKeys") or [])
                  and "host" not in (netv.get("metaKeys") or []), netv.get("metaKeys"))
            check("外部检索默认关闭", netv.get("extEnabled") is False, netv)
            check("判例卡显示自动检测的网络状态",
                  "网络（自动检测）" in (netv.get("netLine") or ""), netv.get("netLine"))
            check("页面上有「联网补充线索」按钮", netv.get("btn") is True, netv)
            check("没点按钮前一条外部线索都没有",
                  netv.get("itemsBefore") == 0, netv)

            clicked = await js(ws, """(async()=>{
              const root = document.querySelector('#feed .extras');
              const b = root.querySelector('.dc-ext .btn');
              if (!b) return {};
              b.click();
              await new Promise(z=>setTimeout(z,900));
              const txt = (root.querySelector('.dc-ext-body')||{}).textContent || '';
              return {items: root.querySelectorAll('.dc-ext-item').length,
                      cases: root.querySelectorAll('.dc-case').length,
                      txt: txt,
                      label: b.textContent};
            })()""")
            check("点了按钮、默认配置下仍不联网且如实说明",
                  clicked.get("items") == 0 and
                  ("默认关闭" in (clicked.get("txt") or "") or
                   "本地已核验" in (clicked.get("txt") or "")), clicked)
            check("联网旁路不改变已核验案例数量",
                  clicked.get("cases") == netv.get("casesBefore"), clicked)
            check("外部线索区不给任何百分比胜率",
                  not _bad_pct(clicked.get("txt") or ""),
                  _bad_pct(clicked.get("txt") or "")[:3])
            check("按钮用完后恢复可点",
                  clicked.get("label") == "联网补充线索", clicked)
            check("独立接口 external=1 默认返回 disabled 且四键不变",
                  netv.get("decisionStatus") == 200
                  and netv.get("extStatus") == "disabled"
                  and netv.get("keys") == ["cost_benefit", "policy", "scope", "similar"],
                  netv)
            check("默认来源仍标注本地判例库、未联网",
                  netv.get("local") == "本地判例库，未联网", netv)

            # 18.5 换法过场收尾（v129）：圆铺满就结束，收尾不许再动整页。
            #      原来 switchProfile 的 settle 给 .view.on 挂 .vc-land
            #      （240ms、整页上浮 6px），用户的原话是「切一下会震一下」。
            #      这里用 MutationObserver 盯类名，而不是过场后读
            #      transform：动画带 both 填充、320ms 后类就被摘掉，等 1 秒
            #      再去读，transform 本来就已经回到 none——连拍也不一定落在
            #      那 240ms 里，那种断言是绿的但抓不到东西。盯类名则只要有人
            #      把这段动画写回来，这里必红。
            for _ in range(15):
                if not await js(ws, "!!window._busy"):
                    break
                await asyncio.sleep(0.2)
            await js(ws, """(() => {
              window.__landSeen = false;
              if (window.__vcObs) window.__vcObs.disconnect();
              window.__vcObs = new MutationObserver(ms => {
                for (const m of ms) {
                  const c = m.target.classList;
                  if (c && c.contains('vc-land')) window.__landSeen = true;
                }
              });
              window.__vcObs.observe(document.body,
                {subtree: true, attributes: true, attributeFilter: ['class']});
              return true;
            })()""")
            # v139：切法入口只在「智能咨询」页显示（其余页 body.no-pf-rail）。
            # 上面按 Alt+7 遗留的状态还停在案情档案页，先回聊天页再点索引条，
            # 否则按钮是 display:none，这条就假红了。
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.4)
            cur = await js(ws, "document.documentElement.dataset.pid")
            target = "consumer" if cur == "labor" else "labor"
            await js(ws, f"""(() => {{
              const b = document.querySelector('#pf-rail [data-pid="{target}"]');
              if (b) b.click();
              return !!b;
            }})()""")
            await asyncio.sleep(1.1)      # 620ms 过场 + 收尾余量
            after = await js(ws, """(() => {
              const r = document.getElementById('vc-ring');
              const v = document.querySelector('.view.on');
              return {
                pid: document.documentElement.dataset.pid,
                land: window.__landSeen,
                opacity: r ? getComputedStyle(r).opacity : 'gone',
                name: r ? (r.style.viewTransitionName || '') : 'gone',
                vt: !!document.startViewTransition,
                transform: v ? getComputedStyle(v).transform : 'gone'
              };
            })()""")
            check("换法真的换了内核（索引条可点）",
                  after.get("pid") == target, f"{cur} → {after.get('pid')}")
            check("换法收尾不再给整页挂位移动画（没有「震一下」）",
                  after.get("land") is False, after.get("land"))
            # settle 现在只剩这一件事。漏了的话 name 会留在环上，之后每次过场
            # （包括切主题）都把这个圆环一起快照进去——多一圈线，且不报错。
            check("换法结束后描边环不再参与过场（name 已摘、opacity 归 0）",
                  after.get("name") == "" and after.get("opacity") == "0", after)

            # v138：非劳动法内核的案情档案要按本内核字段表渲染，不能永远
            # 是劳动法那八项。这里直接验 slotDefs() 的分流逻辑（不依赖是否
            # 有 consumer 会话）：consumer 下返回的是商品/金额/问题那套，
            # 且不含「月工资/离职方式」这些劳动争议字段——串了就是用户
            # 打开档案看到一堆和自己案子无关的框。
            dslots_ok = await js(ws, """(() => {
              const d = document.documentElement;
              const old = d.dataset.pid;
              d.dataset.pid = 'consumer';
              const defs = slotDefs();
              d.dataset.pid = old;
              const keys = defs.map(x => x.k);
              const labels = defs.map(x => x.label);
              return {
                n: defs.length,
                keys: keys,
                has_goods: keys.indexOf('goods') >= 0,
                has_price: keys.indexOf('price') >= 0,
                no_labor: !('monthly_wage' in keys) && !('leave_reason' in keys),
                labels_cn: labels.every(s => /[\\u4e00-\\u9fff]/.test(s))
              };
            })()""")
            check("接入内核字段表按 pid 分流（consumer 五字段、无劳动法字段）",
                  dslots_ok.get("n") == 5 and dslots_ok.get("has_goods")
                  and dslots_ok.get("has_price") and dslots_ok.get("no_labor")
                  and dslots_ok.get("labels_cn"), dslots_ok)

            await js(ws, """(() => {
              const b = document.querySelector('#pf-rail [data-pid="labor"]');
              if (b) b.click();
              return !!b;
            })()""")
            await asyncio.sleep(1.1)
            check("能切回劳动法",
                  await js(ws, "document.documentElement.dataset.pid") == "labor")

            # v139：只有「智能咨询」页能切换法案。用户报的就是这个 bug——
            # 从个人档案 / 历史 / 案情档案页点索引条，整页换成新法域的皮，
            # 页面上却还是旧法域的数据，哪边都对不上。
            # 两道防线都要验：CSS 收起入口，以及 JS 入口自己挡一道（键盘
            # roving tabindex 和程序调用不走点击，display:none 拦不住）。
            await js(ws, "window.LawNav.panel('history')")
            await asyncio.sleep(0.5)      # _applyPanel 在 60ms 后才跑，不能同步读
            guard = await js(ws, """(() => {
              const rail = document.getElementById('pf-rail');
              const hidden = document.body.classList.contains('no-pf-rail')
                             && (!rail || getComputedStyle(rail).display === 'none');
              const before = window.currentProfile;
              switchProfile('consumer');
              return {hidden: hidden, before: before,
                      after: window.currentProfile,
                      pid: document.documentElement.dataset.pid,
                      view: (document.querySelector('.view.on') || {}).id};
            })()""")
            check("只有智能咨询页能切法案：其它页收起切法索引条",
                  guard.get("hidden") is True, guard)
            check("非聊天页绕不过 JS 闸：switchProfile 不改当前法域",
                  guard.get("view") == "p-history"
                  and guard.get("after") == guard.get("before")
                  and guard.get("pid") == guard.get("before"), guard)

            # 就地验红：把 .view.on 临时改回 p-chat、并关掉 view transition
            #（否则 paint 在下一帧才跑，同步读会假绿），同一调用必须真的换法。
            # 证明上面拦住它的是「当前页」这道闸，不是 pid 恰好相同之类巧合。
            ctrl = await js(ws, """(() => {
              const h = document.getElementById('p-history');
              const c = document.getElementById('p-chat');
              const vt = document.startViewTransition;
              document.startViewTransition = undefined;
              h.classList.remove('on'); c.classList.add('on');
              const before = window.currentProfile;
              switchProfile('consumer');
              const after = window.currentProfile;
              switchProfile('labor');
              document.startViewTransition = vt;
              c.classList.remove('on'); h.classList.add('on');
              return {before: before, after: after};
            })()""")
            await js(ws, "window.LawNav.panel('chat')")
            await asyncio.sleep(0.5)
            check("就地验红：闸放回来后同一调用确实能换法",
                  ctrl.get("after") == "consumer"
                  and ctrl.get("before") != ctrl.get("after"), ctrl)

            # 20. v139 三个新页面：能打开、有内容、页内选择器不改全局法域。
            #     顺带验「后来加的页面也没有切法索引条」——用户报的 bug 就是
            #     从非咨询页换法，新页面更不能漏掉这道收口。
            await js(ws, "window.LawNav.panel('library')")
            await asyncio.sleep(1.8)      # 切页遮罩 0.7–1.0s + 目录/原文两次取数
            lib = await js(ws, """(() => {
              const v = document.querySelector('.view.on');
              const toc = document.getElementById('lib-toc');
              const body = document.getElementById('lib-body');
              return {
                view: v ? v.id : '',
                links: toc ? toc.querySelectorAll('a').length : 0,
                arts: body ? body.querySelectorAll('.lib-art').length : 0,
                vpid: document.getElementById('lib-vpid').children.length,
                rail: getComputedStyle(document.getElementById('pf-rail')).display,
                globalPid: document.documentElement.dataset.pid
              };
            })()""")
            check("法典图书馆能打开且目录/原文都渲染出来",
                  lib.get("view") == "p-library" and lib.get("links") > 0
                  and lib.get("arts") > 0, lib)
            check("图书馆页内法典选择器有五枚", lib.get("vpid") == 5, lib)

            # 页内换法：本页纸色跟着换，html[data-pid] 一动不动。
            await js(ws, """(() => {
              const b = document.querySelector('#lib-vpid [data-vpid="consumer"]');
              if (b) b.click();
            })()""")
            await asyncio.sleep(1.4)
            after_v = await js(ws, """(() => ({
              local: document.getElementById('p-library').getAttribute('data-pid'),
              global: document.documentElement.dataset.pid
            }))()""")
            check("页内换法只改本页 data-pid，不改全局法域",
                  after_v.get("local") == "consumer"
                  and after_v.get("global") == lib.get("globalPid"), after_v)

            await js(ws, "window.LawNav.panel('study')")
            await asyncio.sleep(1.4)
            # v142 回归钉：出卷卡的题数行。原 bug 是 5 个控件被 flex 均分
            # 压到 39px，「自定」输入框的左右内边距只能写 0，字贴着边框、
            # 卡缘一切就断。判据是真实矩形：每个控件 ≥50px、都在卡片内、
            # 整行不横向溢出。
            nq = await js(ws, """(() => {
              const row = document.getElementById('st-n');
              const card = document.getElementById('st-setup');
              const cr = card.getBoundingClientRect();
              const kids = [...row.children].map(el => {
                const r = el.getBoundingClientRect();
                return {w: Math.round(r.width),
                        over: Math.round(r.right - cr.right)};
              });
              return {n: kids.length,
                      minW: kids.length ? Math.min(...kids.map(k => k.w)) : 0,
                      maxOver: kids.length ? Math.max(...kids.map(k => k.over)) : 99,
                      sw: row.scrollWidth, cw: row.clientWidth};
            })()""")
            check("出卷题数每个控件都装得下一行字（不被压成 39px）",
                  nq.get("n") == 5 and nq.get("minW") >= 50
                  and nq.get("maxOver") <= 0
                  and nq.get("sw", 1) <= nq.get("cw", 0), nq)
            await js(ws, "document.getElementById('st-new').click()")
            quiz_n = 0
            for _ in range(20):
                quiz_n = await js(ws, "document.querySelectorAll('#st-questions .q-card').length")
                if quiz_n:
                    break
                await asyncio.sleep(0.4)
            st = await js(ws, """(() => {
              const cards = document.querySelectorAll('#st-questions .q-card');
              const hud = document.querySelector('#st-hud .st-hud');
              return {
                n: cards.length,
                opts: cards.length ? cards[0].querySelectorAll('.q-opt').length : 0,
                laws: document.getElementById('st-law').options.length,
                hud: !!hud,
                nav: hud ? hud.querySelectorAll('[data-goto]').length : 0,
                submit: hud ? hud.querySelectorAll('[data-submit-paper]').length : 0,
                blend: cards.length ? cards[0].getAttribute('data-qt') : '',
                rail: getComputedStyle(document.getElementById('pf-rail')).display
              };
            })()""")
            check("自主学习能出题且每题有选项",
                  st.get("n") >= 1 and st.get("opts") >= 2, st)
            check("学习页范围下拉已按本内核填好", st.get("laws") >= 2, st)
            # v140：卷子必须有吸顶进度条与交卷按钮，题号导航数量与题目一致。
            check("学习页生成的是「一张卷」（进度条 + 题号导航 + 交卷）",
                  st.get("hud") and st.get("nav") == st.get("n")
                  and st.get("submit") == 1, st)

            # 答一题 → 进度条动 → 交卷 → 出评估报告（分数与逐题回顾）。
            await js(ws, """(() => {
              const c = document.querySelector('#st-questions .q-card');
              if (!c) return;
              c.setAttribute('data-qt', 'blank');
              const card = document.querySelector('#st-questions .q-card');
              card.setAttribute('data-qt', 'choice');
              const b = card.querySelector('[data-opt]');
              if (b) b.click();
            })()""")
            await asyncio.sleep(0.4)
            done0 = await js(ws, "document.querySelector('#st-hud [data-done]')"
                                 " ? document.querySelector('#st-hud [data-done]')"
                                 ".textContent : ''")
            check("选中选项后进度条计数 +1", done0 == "1", done0)

            await js(ws, "document.querySelector('#st-hud [data-submit-paper]').click()")
            score = None
            for _ in range(30):
                score = await js(ws, "(() => { const e = document"
                                     ".querySelector('#st-questions .rp-score');"
                                     " return e ? e.textContent : ''; })()")
                if score:
                    break
                await asyncio.sleep(0.4)
            rp = await js(ws, """(() => ({
              score: (document.querySelector('#st-questions .rp-score') || {}).textContent || '',
              next: (document.querySelector('#st-questions .rp-next') || {}).textContent || '',
              boxes: document.querySelectorAll('#st-questions .rp-box').length,
              svg: document.querySelectorAll('#st-questions .rp-box svg').length
            }))()""")
            check("交卷后出评估报告（带分数与下一步）",
                  bool(rp.get("score")) and len(rp.get("next") or "") >= 8, rp)
            check("报告里题型/章节用图表示（不是纯文字）",
                  rp.get("svg") >= 1, rp)

            # v141 二回合回归钉：条形图标签切 9 个汉字 ≈ 99px，而条形
            # 底座从 x=78 起画、**后画盖先画**——「第十章 供用电…」被
            # 拦腰盖住，用户截图里的又一处「乱码」。labelW 已放宽到 104，
            # 这里在真实报告 DOM 上量：每段标签的右缘不得压进条形起点。
            rcover = await js(ws, """(() => {
              const box = [...document.querySelectorAll(
                '#st-questions .rp-box')].find(b =>
                  (b.querySelector('h4') || {}).textContent === '章节正确率');
              if (!box) return {rows: 0, hits: []};
              const s = box.querySelector('svg');
              if (!s) return {rows: 0, hits: []};
              const rects = [...s.querySelectorAll('rect')];
              if (!rects.length) return {rows: 0, hits: []};
              const barL = rects[0].getBoundingClientRect().left;
              /* 只查**标签**（起点在条形左边）的文字：右侧的「0/1」读数
                 本来就在条形右边，right > barL 恒成立，不能算被盖。 */
              const hits = [...s.querySelectorAll('text')]
                .filter(t => {
                  const b = t.getBoundingClientRect();
                  return b.left < barL && b.right > barL + 2;
                })
                .map(t => t.textContent || '');
              return {rows: s.querySelectorAll('text').length,
                      hits: hits.slice(0, 5)};
            })()""")
            if rcover and rcover.get("rows"):
                check("报告条形图标签不被条形盖住（labelW 装得下 10 个汉字）",
                      not rcover.get("hits"), rcover)

            # v142 主页分成六个子页，切页状态记在本机。这里先清掉上次
            # 残留，才能验「默认停在概览」。
            await js(ws, "localStorage.removeItem('law_me_tab')")
            await js(ws, "window.LawNav.panel('me')")
            await asyncio.sleep(2.0)
            me = await js(ws, """(() => ({
              cards: document.querySelectorAll('#me-progress .me-card').length,
              rail: getComputedStyle(document.getElementById('pf-rail')).display,
              summary: (document.getElementById('me-summary').textContent || '').length,
              svg: document.querySelectorAll('#p-me .ch-wrap svg').length,
              tree: document.querySelectorAll('#me-tree .tr-node').length,
              treeLinks: document.querySelectorAll('#me-tree .tr-link').length,
              cal: document.querySelectorAll('#me-heat .cal-c').length,
              badges: document.querySelectorAll('#me-badges .bd-i').length,
              badgeKinds: document.querySelectorAll('#me-badges .bd-kind').length,
              wrong: document.querySelectorAll('#me-wrong .wb-i').length
            }))()""")
            check("个人主页给全五个法典进度",
                  me.get("cards") == 5 and me.get("summary") > 0, me)
            check("主页有图表（折线/环形/雷达/柱状至少 3 张）",
                  me.get("svg") >= 3, me.get("svg"))
            check("知识树是真树形（有节点也有连线）",
                  me.get("tree") >= 3 and me.get("treeLinks") >= 2,
                  {"nodes": me.get("tree"), "links": me.get("treeLinks")})
            check("打卡是日历网格（一格一天）", me.get("cal") >= 28, me.get("cal"))
            check("徽章墙列出全部徽章（未获得也显示）",
                  me.get("badges") >= 18, me.get("badges"))
            check("徽章按组分栏（六组都在）",
                  me.get("badgeKinds") >= 6, me.get("badgeKinds"))

            # v142：主页拆成六个子页。先验导航本身——六个标题齐全、
            # 默认只露「概览」、点一下就切过去且别的同时收起、指示条跟得上。
            tabs = await js(ws, """(() => {
              const bs = [...document.querySelectorAll('#me-tabs [data-tab]')];
              const panes = [...document.querySelectorAll('#p-me .me-pane')];
              return {
                tabs: bs.length, panes: panes.length,
                labels: bs.map(b => (b.textContent || '').trim().slice(0, 2)),
                on: bs.filter(b => b.classList.contains('on'))
                      .map(b => b.dataset.tab),
                shown: panes.filter(p => !p.hidden).map(p => p.dataset.pane)
              };
            })()""")
            check("个人主页有六个子页标题（不是一屏七段往下堆）",
                  tabs.get("tabs") == 6 and tabs.get("panes") == 6, tabs)
            check("进主页默认只显示「概览」一个子页",
                  tabs.get("on") == ["overview"]
                  and tabs.get("shown") == ["overview"], tabs)
            await js(ws, "meTab('wrong')")
            await asyncio.sleep(0.45)
            sw1 = await js(ws, """(() => {
              const on = [...document.querySelectorAll('#me-tabs [data-tab].on')]
                .map(b => b.dataset.tab);
              const shown = [...document.querySelectorAll('#p-me .me-pane')]
                .filter(p => !p.hidden).map(p => p.dataset.pane);
              const ink = document.getElementById('me-tab-ink');
              return {on, shown,
                      inkW: Math.round(ink.getBoundingClientRect().width)};
            })()""")
            check("点子页真的切过去（同一时刻只有一个子页可见）",
                  sw1.get("on") == ["wrong"] and sw1.get("shown") == ["wrong"],
                  sw1)
            check("子页指示条跟着选中项走（不是零宽）",
                  (sw1.get("inkW") or 0) >= 20, sw1.get("inkW"))

            # v142 窄屏：≤620px 时这排从「换行」改成「横滑」，滚动条被藏了。
            # 两件事只能自己保证：① 选中项要自动滚进视野——否则从本机恢复
            # 到「打卡」时它整颗在屏外，用户以为压根没这两个子页（390px 实测
            # 「打卡」左边距 472、「徽章」378，全在屏外）；② 右缘要有渐隐
            # 暗示，滚到头得摘掉。桌面下 scrollWidth==clientWidth、滚动量恒 0，
            # 这两条根本不动作——所以必须真把视口压到 390 才测得到。
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1,
                "mobile": True})
            await asyncio.sleep(0.6)
            narrow = []
            for _t in ("cal", "overview"):
                await js(ws, f"meTab('{_t}')")
                await asyncio.sleep(0.5)
                narrow.append(await js(ws, """(() => {
                  const bar = document.getElementById('me-tabs');
                  const on = bar.querySelector('[data-tab].on');
                  const br = bar.getBoundingClientRect();
                  const orr = on.getBoundingClientRect();
                  const pane = document.getElementById('p-me');
                  return {tab: on.dataset.tab,
                          over: Math.round(bar.scrollWidth - bar.clientWidth),
                          sl: Math.round(bar.scrollLeft),
                          inside: orr.left >= br.left - 1
                                  && orr.right <= br.right + 1,
                          more: bar.classList.contains('more'),
                          paneOver: Math.round(pane.scrollWidth
                                               - pane.clientWidth)};
                })()"""))
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
            await asyncio.sleep(0.6)
            await js(ws, "meTab('overview')")
            await asyncio.sleep(0.45)
            check("窄屏（390）子页条能横滑，且选中的那颗在屏幕内（不是整颗跑出去）",
                  len(narrow) == 2
                  and all(x.get("over", 0) > 0 and x.get("inside") is True
                          and x.get("paneOver") == 0 for x in narrow), narrow)
            check("窄屏右缘渐隐跟着滚动走（滚到头摘掉、退回开头挂上）",
                  narrow[0].get("more") is False
                  and narrow[1].get("more") is True, narrow)

            # v142 回归钉：进度卡里的按钮与读数不能被卡片裁掉。原 bug 就是
            # 视口 1440 时五列各 124px、按钮要 151px，被 overflow:hidden
            # 切掉 27px。这里按真实矩形量，不数节点。
            await js(ws, "meTab('progress')")
            await asyncio.sleep(0.55)
            cardq = await js(ws, """(() => {
              const box = document.getElementById('me-progress');
              const out = {cards: 0, overR: -999, minW: 999, sw: 0,
                           labels: []};
              box.querySelectorAll('.me-card').forEach(c => {
                const cr = c.getBoundingClientRect();
                out.cards++;
                out.minW = Math.min(out.minW, Math.round(cr.width));
                out.sw = Math.max(out.sw, c.scrollWidth - c.clientWidth);
                const b = c.querySelector('button');
                if (b) {
                  out.overR = Math.max(out.overR,
                    Math.round(b.getBoundingClientRect().right - cr.right));
                }
                const ls = [...c.querySelectorAll('.me-kv span')]
                  .map(s => (s.textContent || '').trim()).join('/');
                if (ls) out.labels.push(ls);
              });
              return out;
            })()""")
            check("进度卡里的按钮整颗在卡内（不再被 overflow:hidden 切）",
                  cardq.get("cards") == 5 and cardq.get("overR", 99) <= -2,
                  cardq)
            check("进度卡够宽（≥170px，没被五列压成 124px）",
                  (cardq.get("minW") or 0) >= 170, cardq.get("minW"))
            check("进度卡读数是「章节/已练/正确率」三行标签（不是一句长文案）",
                  len(cardq.get("labels") or []) == 5
                  and all(x == "章节/已练/正确率"
                          for x in cardq.get("labels")), cardq.get("labels"))

            # v141 日历「乱码」修整的回归：表头七天必须齐、网格不能横向
            # 溢出、格子不能大回 93px 那种占满一屏的尺寸。这里量的是
            # getBoundingClientRect / scrollWidth，不是数节点——「数得对
            # ≠ 画得对」，v140 就靠数格子漏过了裁掉两列的日历。
            # 日历在「打卡」子页里。隐藏的 pane 量到的矩形是 0，先切过去。
            await js(ws, "meTab('cal')")
            await asyncio.sleep(0.6)
            calq = await js(ws, """(() => {
              const g = document.querySelector('#me-heat .cal-w');
              const wd = document.querySelectorAll('#me-heat .cal-w .wd');
              const cs = document.querySelectorAll('#me-heat .cal-c');
              /* 容器类名是 cal-pg：.cal-nav 已被时效日历的单个按钮占用，
                 撞名会把容器锁死成 30px 宽、› 被卡片切掉（v141 二回合）。 */
              const nv = document.querySelector('#me-heat .cal-pg');
              const nr = nv && nv.getBoundingClientRect();
              const btns = nv ? [...nv.querySelectorAll('button')] : [];
              const br = btns.length ? btns[btns.length - 1].getBoundingClientRect() : null;
              const cr = document.querySelector('#me-heat').getBoundingClientRect();
              const first = cs[0] && cs[0].getBoundingClientRect();
              const last = cs[cs.length - 1] && cs[cs.length - 1].getBoundingClientRect();
              const gr = g && g.getBoundingClientRect();
              return {
                wdText: [...wd].map(w => w.textContent).join(''),
                overflow: g ? g.scrollWidth - g.clientWidth : -1,
                gridW: gr ? Math.round(gr.width) : null,
                cellW: first ? Math.round(first.width) : null,
                firstX: first ? Math.round(first.x) : null,
                lastR: last ? Math.round(last.x + last.width) : null,
                gridR: gr ? Math.round(gr.x + gr.width) : null,
                navW: nr ? Math.round(nr.width) : null,
                navH: nr ? Math.round(nr.height) : null,
                btnN: btns.length,
                lastBtnR: br ? Math.round(br.right) : null,
                cardR: Math.round(cr.right)
              };
            })()""")
            check("日历表头七天齐全（一~日）",
                  calq.get("wdText") == "一二三四五六日", calq.get("wdText"))
            check("日历网格不横向溢出", calq.get("overflow", 1) <= 0,
                  calq.get("overflow"))
            check("日历格子收进面板内（宽 ≤64px、不出容器）",
                  0 < (calq.get("cellW") or 0) <= 64
                  and calq.get("firstX") is not None
                  and calq.get("firstX") >= 0
                  and (calq.get("lastR") or 0) <= (calq.get("gridR") or -1), calq)
            # v141 真凶的回归钉：日历翻月按钮容器曾用通用类 .nav，被
            # 172px 通高的侧栏样式整个劫持——月份标题消失、按钮竖排、
            # 左侧多出一条盖住内容的空白列，用户看到的就是「乱码」。
            # v141 二回合：改叫 .cal-nav 又撞上时效日历**单个按钮**的
            # width:30px 规则，容器锁死 30px、› 被卡片内缘切掉一半。
            # 最终名 .cal-pg：装得下两个 28px 按钮（56+6=62），又必须
            # 是小块头——撞上任何全局规则都会在这两条上现形。
            check("日历翻月按钮容器是小块头（没被 .nav/.cal-nav 旧规则劫持）",
                  56 <= (calq.get("navW") or 0) <= 90
                  and 0 < (calq.get("navH") or 0) <= 44, calq)
            check("日历 › 按钮整颗在卡片内（没被内缘切掉）",
                  calq.get("btnN") == 2
                  and (calq.get("lastBtnR") or 0) <= (calq.get("cardR") or 0) - 6,
                  calq)

            # v141 知识树整棵要落在一屏内：svg 高度有上限，不再 721px。
            # v142：树在「知识树」子页里，先切过去再量。
            await js(ws, "meTab('tree')")
            await asyncio.sleep(0.7)
            treeq = await js(ws, """(() => {
              const s = document.querySelector('#me-tree svg');
              if (!s) return null;
              const r = s.getBoundingClientRect();
              return {h: Math.round(r.height), w: Math.round(r.width)};
            })()""")
            check("知识树整棵在一屏内（高 ≤460px 且有画面）",
                  treeq and 100 <= treeq.get("h", 0) <= 460
                  and treeq.get("w", 0) >= 300, treeq)

            # v141 二回合回归钉：行高压缩后上一行的百分比与下一行的章名
            # **同基线叠印**（探针实测 7 对：`0% <-> 买卖合同`……），
            # 连线还从文字底下穿过——用户看到的就是一团乱码。修法是
            # 压缩态改单行 + 文字垫面板色描边。这里把「同 x 且纵向相交
            # 的两段文字」当判据，叠回去直接红。
            tov = await js(ws, """(() => {
              const s = document.querySelector('#me-tree svg');
              if (!s) return null;
              const rows = [...s.querySelectorAll('text')].map(t => {
                const b = t.getBoundingClientRect();
                return {x: Math.round(b.left), y: Math.round(b.top),
                        h: Math.round(b.height), s: t.textContent || ''};
              });
              const hits = [];
              for (let i = 0; i < rows.length; i++)
                for (let j = 0; j < rows.length; j++) {
                  if (i === j) continue;
                  const a = rows[i], b = rows[j];
                  if (Math.abs(a.x - b.x) < 3 && a.y < b.y
                      && b.y - a.y < Math.max(a.h, b.h) * 0.6)
                    hits.push(a.s + ' <-> ' + b.s);
                }
              return {n: rows.length, hits: [...new Set(hits)].slice(0, 5)};
            })()""")
            check("知识树没有文字叠印（压缩行距后仍是单行可读）",
                  tov and tov.get("n", 0) > 0 and not tov.get("hits"), tov)

            # v142：长章名不能顶出树框右缘。svg 是 overflow:visible，文字
            # 超出去不会被 svg 自己裁，而是被外层 .tree-w 裁——表现就是
            # 最后半个字没了。这里按真实矩形比，不数字数。
            tedge = await js(ws, """(() => {
              const box = document.getElementById('me-tree');
              if (!box) return null;
              const br = box.getBoundingClientRect();
              const lim = br.right - 6;
              let worst = -999, name = '';
              [...box.querySelectorAll('text')].forEach(t => {
                const b = t.getBoundingClientRect();
                const over = Math.round(b.right - lim);
                if (over > worst) { worst = over; name = t.textContent || ''; }
              });
              return {over: worst, txt: (name || '').slice(0, 14),
                      boxW: Math.round(br.width)};
            })()""")
            check("知识树文字不出右缘（长章名不贴框、不被裁半个字）",
                  tedge and tedge.get("over", 99) <= 0, tedge)

            # v142：选择器原来是五等分列，每颗只有 114px，「交通事故与
            # 人身损害 0%」要 157px → 被裁掉 43px，连「民法典·合同」都得
            # 打省略号，用户得猜才能选法。改成按内容宽度排之后，五颗按钮
            # 的文字都不许比盒子宽（留 1px 亚像素容差）。
            pickq = await js(ws, """(() => {
              const box = document.getElementById('me-tree-pick');
              if (!box) return null;
              const rows = [...box.querySelectorAll('[data-tree]')].map(b => ({
                t: (b.textContent || '').trim().slice(0, 12),
                cut: Math.round(b.scrollWidth - b.clientWidth),
                w: Math.round(b.getBoundingClientRect().width)}));
              return {n: rows.length, rows,
                      worst: rows.length
                        ? Math.max.apply(null, rows.map(r => r.cut)) : 0};
            })()""")
            check("知识树的法典选择器五个名字都完整（不被等宽列切掉半截）",
                  pickq and pickq.get("n") == 5
                  and pickq.get("worst", 99) <= 1, pickq)

            # v145 窄屏三处「看着像坏了」：
            #  · 概览四宫格塌成一列（隔壁错题页的同类四宫格一直是 2×2）；
            #  · 打卡那句「…打卡 2 / 天，还有 12 天没练。」数字和量词被拆到
            #    两行——中文排版允许在数字与汉字之间断行，宽度上再也调不出来；
            #  · 知识树缩成 260 宽的缩略图：viewBox 700 铺进去，11.5px 的章名
            #    实际只有 4.3px，一个字都认不出。
            # 三条都得真把视口压到 390 才测得到，桌面下全是绿的。
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 390, "height": 844, "deviceScaleFactor": 1,
                "mobile": True})
            await asyncio.sleep(0.6)
            await js(ws, "meTab('overview')")
            await asyncio.sleep(0.5)
            g390 = await js(ws, """(() => {
              const box = document.getElementById('me-summary');
              const cs = [...box.querySelectorAll('.me-card')];
              const xs = new Set(cs.map(e =>
                Math.round(e.getBoundingClientRect().left)));
              const ys = new Set(cs.map(e =>
                Math.round(e.getBoundingClientRect().top)));
              return {n: cs.length, cols: xs.size, rows: ys.size,
                      w: cs.length
                        ? Math.round(cs[0].getBoundingClientRect().width) : 0,
                      over: Math.round(document.documentElement.scrollWidth
                                       - document.documentElement.clientWidth)};
            })()""")
            # 卡宽下限 110：390 实测 124px。再窄就说明卡被挤成一条了，
            # 那时「2×2」也没意义。
            check("窄屏（390）概览四宫格是 2×2（不塌成一列、整页不横滚）",
                  g390.get("n") == 4 and g390.get("cols") == 2
                  and g390.get("rows") == 2 and g390.get("over") == 0
                  and (g390.get("w") or 0) >= 110, g390)
            await js(ws, "meTab('cal')")
            await asyncio.sleep(0.6)
            nw390 = await js(ws, """(() => {
              const box = document.querySelector('#me-heat .cal-ring-t');
              if (!box) return null;
              const spans = [...box.querySelectorAll('.nw')];
              const lh = parseFloat(getComputedStyle(box).lineHeight) || 20;
              const hs = spans.map(s =>
                Math.round(s.getBoundingClientRect().height));
              return {n: spans.length, lh: Math.round(lh), hs,
                      wrapped: hs.filter(h => h > lh * 1.5).length,
                      txt: (box.textContent || '').trim().slice(0, 40)};
            })()""")
            check("窄屏（390）打卡文案不把数字与量词拆到两行",
                  nw390 and nw390.get("n") == 3
                  and nw390.get("wrapped") == 0, nw390)
            await js(ws, "meTab('tree')")
            await asyncio.sleep(0.7)
            tr390 = await js(ws, """(() => {
              const box = document.getElementById('me-tree');
              const s = box && box.querySelector('svg');
              if (!s) return null;
              const r = s.getBoundingClientRect();
              /* 必须量章名（tr-lbl, 11.5px），不能量根节点——
                 根是 15px，缩到 232 宽还剩 5px，判据会松一档 */
              const t = s.querySelector('text.tr-lbl')
                || s.querySelector('text');
              const fs = t ? parseFloat(getComputedStyle(t).fontSize) : 0;
              return {w: Math.round(r.width),
                      sw: Math.round(box.scrollWidth),
                      cw: Math.round(box.clientWidth),
                      /* 屏幕上真正的大小：视图缩放后字号还要乘 svg 实宽/700 */
                      font: Math.round(fs * (r.width / 700) * 10) / 10,
                      over: Math.round(document.documentElement.scrollWidth
                                       - document.documentElement.clientWidth)};
            })()""")
            check("窄屏（390）知识树不是缩略图（字够大、卡内可横滑、整页不横滚）",
                  tr390 and (tr390.get("w") or 0) >= 560
                  and (tr390.get("sw") or 0) > (tr390.get("cw") or 0) + 60
                  and (tr390.get("font") or 0) >= 9
                  and tr390.get("over") == 0, tr390)
            await send(ws, "Emulation.setDeviceMetricsOverride", {
                "width": 1440, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
            await asyncio.sleep(0.6)

            # v145：徽章改成**默认展开**。原来（v141）默认收起是因为徽章和
            # 日历挤在同一个长页面里，二十来枚摊开会把日历推到两屏之外；
            # v142 把每个小功能拆成独立子页后，点「徽章」只看到一行标题 +
            # 一颗按钮 + 五百多像素空白——等于这一页没做。
            # 折叠能力保留，判据只看 aria-expanded，不假设它从哪一态开始。
            await js(ws, "meTab('badge')")
            await asyncio.sleep(0.5)
            bd0 = await js(ws, """(() => {
              const b = document.getElementById('me-badges-body');
              const btn = document.getElementById('me-badge-btn');
              const hint = document.getElementById('me-badge-hint');
              const all = b ? [...b.querySelectorAll('.bd-i')] : [];
              return {hidden: b ? b.hidden : null,
                      expanded: btn ? btn.getAttribute('aria-expanded') : null,
                      label: btn ? (btn.textContent || '').trim() : null,
                      hintHidden: hint ? hint.hidden : null,
                      n: all.length,
                      vis: all.filter(e => e.offsetParent !== null).length,
                      h: b ? Math.round(b.getBoundingClientRect().height) : 0};
            })()""")
            check("徽章子页默认就摊开（点进来看到墙，不是一行标题加一颗按钮）",
                  bd0.get("hidden") is False
                  and bd0.get("expanded") == "true"
                  and (bd0.get("vis") or 0) >= 18
                  and (bd0.get("h") or 0) >= 200, bd0)
            check("默认态按钮写「收起」、那句「六组共 21 枚」一起收着",
                  (bd0.get("label") or "").startswith("收起")
                  and bd0.get("hintHidden") is True, bd0)
            await js(ws, "document.getElementById('me-badge-btn').click()")
            await asyncio.sleep(0.45)
            bd1 = await js(ws, """(() => {
              const b = document.getElementById('me-badges-body');
              const btn = document.getElementById('me-badge-btn');
              const hint = document.getElementById('me-badge-hint');
              return {hidden: b.hidden,
                      expanded: btn.getAttribute('aria-expanded'),
                      label: (btn.textContent || '').trim(),
                      hintHidden: hint.hidden};
            })()""")
            check("点「收起」能折回去（露出提示行、按钮变成「展开全部」）",
                  bd1.get("hidden") is True and bd1.get("expanded") == "false"
                  and (bd1.get("label") or "").startswith("展开全部")
                  and bd1.get("hintHidden") is False, bd1)
            await js(ws, "document.getElementById('me-badge-btn').click()")
            await asyncio.sleep(0.3)
            bd2 = await js(ws, """(() => {
              const b = document.getElementById('me-badges-body');
              return {hidden: b.hidden,
                      vis: [...b.querySelectorAll('.bd-i')]
                        .filter(e => e.offsetParent !== null).length};
            })()""")
            check("再点「展开全部」又能摊开（能反复开合）",
                  bd2.get("hidden") is False and (bd2.get("vis") or 0) >= 18,
                  bd2)

            # 日历翻月：上个月格子数变了，说明真的按月查而不是画死本月。
            d0 = await js(ws, "(document.querySelector('#me-heat .cal-hd .t') || {}).textContent")
            await js(ws, """(() => { const b = document.querySelector(
                '#me-heat [data-cal]'); if (b) b.click(); })()""")
            await asyncio.sleep(1.2)
            d1 = await js(ws, "(document.querySelector('#me-heat .cal-hd .t') || {}).textContent")
            check("日历能翻到上个月", bool(d0) and bool(d1) and d0 != d1,
                  {"before": d0, "after": d1})

            # 知识树换法典：五棵树的按钮都要在（不是只有练过的那棵），
            # 换过去以后**树根的名字**要跟着变——两边必须同一口径取数，
            # 一个取「第一个节点」、另一个取「全部节点拼起来」，无论怎么点都不等，
            # 那是假绿。
            picks = await js(ws, """(() => [].map.call(
                document.querySelectorAll('#me-tree-pick [data-tree]'),
                function (b) { return b.getAttribute('data-tree'); }))()""")
            check("知识树能换五种法（选择器给全五枚）",
                  len(picks or []) >= 5, picks)
            ROOT = ("(document.querySelector('#me-tree .tr-node') || {})"
                    ".textContent")
            r0 = await js(ws, ROOT)
            await js(ws, """(() => { const bs = document.querySelectorAll(
                '#me-tree-pick [data-tree]');
                if (bs.length > 1) bs[1].click(); })()""")
            await asyncio.sleep(0.8)
            t2 = await js(ws, "document.querySelectorAll('#me-tree .tr-node').length")
            r1 = await js(ws, ROOT)
            check("换法典后知识树重画（树根换了名字）",
                  t2 >= 2 and bool(r0) and bool(r1) and r0 != r1,
                  {"nodes": t2, "before": r0, "after": r1})
            check("三个新页面都不给切法入口（v139 收口无漏网）",
                  lib.get("rail") == "none" and st.get("rail") == "none"
                  and me.get("rail") == "none",
                  {"library": lib.get("rail"), "study": st.get("rail"),
                   "me": me.get("rail")})
            await js(ws, "window.LawNav.panel('chat')")

            # 18.5 空态（v142）：交付包发出去时库是空的，评审第一眼看到的
            #      就是这一屏。两处必须钉住：
            #      ① 折线卡不能画一条贴底的通栏直线 —— 看着像「真有一条 0
            #         的数据」，另外三张卡空态写的都是「还没有…」；
            #      ② 空树不能把唯一的虚叶摆到 466px 处，拖出一根 276px 的
            #         长线（整棵树就剩一条横线）。
            #      **必须换一个零学习数据的账号**：自检账号答过题，拿它验
            #      空态会永远绿——那种绿比红还坏。
            zuser = None
            try:
                zuser = store.create_user("ui-empty-demo", "ui-empty-pw-123",
                                          role="user", ip="127.0.0.1")
            except Exception:                       # noqa: BLE001
                zuser = next((u for u in store.list_users(size=200)["items"]
                              if u["username"] == "ui-empty-demo"), None)
            if zuser:
                _temp_users.append(zuser["id"])
                store.save_profile(zuser["id"], {"real_name": "空态自检"},
                                   onboarding="done")
                ztok = store.issue_token(zuser["id"], ua="ui-empty")
                await send(ws, "Network.setCookie", {
                    "name": TOKEN_NAME, "value": ztok, "domain": "127.0.0.1",
                    "path": "/", "httpOnly": True, "sameSite": "Lax"})
                await send(ws, "Page.navigate", {"url": base + "/"})
                await asyncio.sleep(2.0)
                await js(ws, "try{localStorage.removeItem('law_me_tab')}"
                             "catch(e){}"
                             "window.LawNav.me()")
                await asyncio.sleep(1.8)
                zch = await js(ws, """(() => {
                  const c = document.getElementById('ch-daily');
                  if (!c) return null;
                  return {svg: !!c.querySelector('svg'),
                          txt: (c.innerText || '').replace(/\\s+/g, '')
                                 .slice(0, 30)};
                })()""")
                check("零数据时不画折线（一条贴底直线会冒充「真有数据」）",
                      zch and zch.get("svg") is False
                      and "还没有作答记录" in (zch.get("txt") or ""), zch)
                await js(ws, "meTab('tree')")
                await asyncio.sleep(1.1)
                ztr = await js(ws, """(() => {
                  const xs = [...document.querySelectorAll('#me-tree .tr-node')]
                    .map(n => Math.round(n.getBoundingClientRect().left));
                  xs.sort((a, b) => a - b);
                  return {n: xs.length, xs,
                          gap: xs.length >= 3 ? xs[2] - xs[1] : -1};
                })()""")
                # 实测：空态叶节点距枝节点 137px；有数据时约 220px（叶列固定
                # 在 466 svg 单位处，缩放后 ≈214）。取 175 当分界，两边各留
                # 约 40px 余量。
                check("零数据时树是三段短链（虚叶不拖一根长线出去）",
                      ztr and ztr.get("n") == 3
                      and 0 < ztr.get("gap", 999) <= 175, ztr)
                try:
                    store._conn.execute(
                        "DELETE FROM auth_tokens WHERE user_id=?",
                        (zuser["id"],))
                    store._conn.execute("DELETE FROM users WHERE id=?",
                                        (zuser["id"],))
                    store._conn.commit()
                except Exception as e:              # noqa: BLE001
                    print(f"  [WARN] 空态账号没删干净：{e}")

            # 19. 第一次使用：新账号登录 -> 档案引导 -> 问第一句话。
            #     v143 在这条链路上发现三处，全都有同一个特点——**单测和截图
            #     都看不见**：
            #       ① 弹窗是 aria-modal，却既没进 LawUI.trapFocus、也没进
            #          Esc 的层栈。遮罩把整页盖住，Tab 却能一路走到后面的
            #          侧栏（实测第 7 步到 .law-ready、第 13 步到导航项）。
            #       ② Esc 什么都按不动，而点遮罩就能关——同一个意思两种结果。
            #       ③ 短视口（横屏手机 844x390）下弹窗 479px 高、垂直居中，
            #          上下各出屏 44px，两颗按钮都在屏外；.dlg 当时既没有
            #          max-height 也没有 overflow，**连滚都滚不动**。
            #     必须用一个真 pending 的账号：自检账号是 done，拿它验这条
            #     会永远绿。
            fuser = None
            try:
                fuser = store.create_user("ui-first-demo", "ui-first-pw-123",
                                          role="user", ip="127.0.0.1")
            except Exception:                       # noqa: BLE001
                fuser = next((u for u in store.list_users(size=200)["items"]
                              if u["username"] == "ui-first-demo"), None)
            if fuser:
                _temp_users.append(fuser["id"])
                store.save_profile(fuser["id"], {}, onboarding="pending")
                ftok = store.issue_token(fuser["id"], ua="ui-first")
                await send(ws, "Network.setCookie", {
                    "name": TOKEN_NAME, "value": ftok, "domain": "127.0.0.1",
                    "path": "/", "httpOnly": True, "sameSite": "Lax"})
                await send(ws, "Page.navigate", {"url": base + "/"})
                await asyncio.sleep(2.4)

                fr = await js(ws, """(() => {
                  const d = document.getElementById('onb');
                  if (!d || d.hidden) return null;
                  const vis = id => { const e = document.getElementById(id);
                    return !!e && !e.hidden && e.getClientRects().length > 0; };
                  const steps = [...document.querySelectorAll('#onb-steps .onb-step')]
                    .filter(s => getComputedStyle(s).display !== 'none').length;
                  const b = d.getBoundingClientRect();
                  return {steps, next: vis('onb-next'), save: vis('onb-save'),
                          skip: vis('onb-skip'),
                          page0: !document.querySelector(
                            ".onb-page[data-p='0']").hidden,
                          w: Math.round(b.width), h: Math.round(b.height)};
                })()""")
                check("新账号首次登录自动弹档案引导（三步：下一步在、保存在无）",
                      fr and fr.get("steps") == 3 and fr.get("next") is True
                      and fr.get("save") is False and fr.get("skip") is True
                      and fr.get("page0") is True, fr)

                # 键盘：一连按 9 下 Tab。判据不能只看「有没有出界」就完事——
                # 合成 KeyboardEvent 不触发浏览器默认动作，焦点根本不会动，
                # 那样测出来永远「没出界」。必须走 CDP 的输入通道。
                ids = []
                for _ in range(9):
                    await press_tab(ws)
                    ids.append(await js(ws,
                        "(()=>{const a=document.activeElement;"
                        "return (a.closest&&a.closest('#onb')?'':'出界:')"
                        "+(a.id||a.className||a.tagName);})()"))
                check("首登弹窗把 Tab 关在弹窗里（不会走到遮罩后面去）",
                      ids and not any(x.startswith("出界") for x in ids)
                      and "pf-real_name" in ids[1:], ids)

                # 短视口：横屏手机 / 窗口拉矮。先量装不装得下，再量滚到底
                # 之后两颗按钮在不在视野里——只写 max-height 不写 overflow
                # 的话第二条会红，这正是修之前的状态。
                await send(ws, "Emulation.setDeviceMetricsOverride", {
                    "width": 844, "height": 390, "deviceScaleFactor": 1,
                    "mobile": True})
                await asyncio.sleep(0.6)
                short = await js(ws, """(() => {
                  const d = document.getElementById('onb');
                  const b = d.getBoundingClientRect();
                  const st = getComputedStyle(d);
                  return {top: Math.round(b.top), bottom: Math.round(b.bottom),
                          vh: innerHeight, over: st.overflowY,
                          canScroll: d.scrollHeight > d.clientHeight + 1};
                })()""")
                await js(ws, "(()=>{const d=document.getElementById('onb');"
                             "d.scrollTop = d.scrollHeight;})()")
                await asyncio.sleep(0.25)
                reach = await js(ws, """(() => {
                  const nb = document.getElementById('onb-next');
                  const b = nb.getBoundingClientRect();
                  return b.top >= -1 && b.bottom <= innerHeight + 1;
                })()""")
                check("短视口（844×390）下弹窗不出屏，且滚得到「下一步」",
                      short and short.get("vh") == 390
                      and short.get("top", -1) >= 0
                      and short.get("bottom", 9999) <= short.get("vh", 0)
                      and short.get("over") == "auto"
                      and short.get("canScroll") is True
                      and reach is True, f"{short} / 滚到底后可见={reach}")
                await send(ws, "Emulation.setDeviceMetricsOverride", {
                    "width": 1440, "height": 900, "deviceScaleFactor": 1,
                    "mobile": False})
                await asyncio.sleep(0.4)

                await press_key(ws, "Escape", "Escape", 27)
                await asyncio.sleep(0.7)
                esc = await js(ws, """(() => ({
                  open: !document.getElementById('onb').hidden,
                  layers: _layers.length}))()""")
                fprof = AuthStore(DB_PATH).get_profile(fuser["id"])
                check("首登弹窗按 Esc 能关，且状态不留 pending（否则下次登录又弹）",
                      esc and esc.get("open") is False and esc.get("layers") == 0
                      and fprof["onboarding"] == "skipped",
                      f"{esc} / {fprof['onboarding']}")

                # Esc 已经把状态置成 skipped，这里手动重开走完三步，验
                # 「保存」这一出口（同时也是劳动法内核下那句提示的对照组）。
                await js(ws, "onbOpen(null)")
                await asyncio.sleep(0.45)
                await js(ws, "document.getElementById('onb-next').click()")
                await asyncio.sleep(0.2)
                await js(ws, "document.getElementById('onb-next').click()")
                await asyncio.sleep(0.2)
                await js(ws, "document.getElementById('pf-real_name').value='张三';"
                             "document.getElementById('onb-save').click()")
                await asyncio.sleep(1.0)
                t_lab = await js(ws, "([...document.querySelectorAll('.law-toast')]"
                                     ".pop()||{}).textContent||''")
                check("劳动法内核保存后的提示会提「申请书」（申请书只这个内核有）",
                      "申请书" in (t_lab or ""), f"{t_lab!r}")

                await js(ws, "applyProfile('consumer'); onbOpen(null)")
                await asyncio.sleep(0.5)
                cn = await js(ws, """(() => {
                  const bar = document.getElementById('onb-steps');
                  return {hidden: bar.hidden,
                          barH: Math.round(bar.getBoundingClientRect().height),
                          next: !document.getElementById('onb-next').hidden,
                          save: !document.getElementById('onb-save').hidden};
                })()""")
                check("非劳动法内核只有一步：步骤条收起、只能「保存并开始」",
                      cn and cn.get("hidden") is True and cn.get("barH") == 0
                      and cn.get("next") is False and cn.get("save") is True, cn)
                await js(ws, "document.getElementById('onb-save').click()")
                await asyncio.sleep(1.0)
                t_con = await js(ws, "([...document.querySelectorAll('.law-toast')]"
                                     ".pop()||{}).textContent||''")
                check("非劳动法内核保存后的提示不提「申请书」（串味）",
                      bool(t_con) and "申请书" not in t_con, f"{t_con!r}")

            # 20. 登录页状态行（v127 起正常态是空的）。
            #     以前这行报「已收录 2066 条条文 · 9 部法律 · LLM 就绪（未调用）」，
            #     而且余额扣光之后仍写「在线」。现在规整成：正常一律不写，
            #     只有语料是示例、或模型不可用时才亮一句警告——这两种不说，
            #     用户会以为系统坏了或者被骗。所以断言改成条件式：
            #     安静要证明真的空，亮起要证明亮的是该亮的那句。
            meta = json.load(urllib.request.urlopen(base + "/api/meta", timeout=5))
            await send(ws, "Network.clearBrowserCookies")
            await send(ws, "Page.navigate", {"url": base + "/login"})
            await asyncio.sleep(1.8)
            stat = await js(ws, "document.getElementById('stat').textContent")
            live = meta.get("llm_live")
            if meta.get("corpus_seed"):
                check("示例语料时登录页明确告警",
                      "示例语料" in (stat or ""), stat)
            elif live == "error":
                check("模型不可用时登录页说明降级回放",
                      "回放" in (stat or ""), stat)
            else:
                check("登录页正常态不再展示规模与就绪提示",
                      not (stat or "").strip(), f"{stat!r}")
                check("登录页不出现「已收录」字样",
                      "已收录" not in (stat or ""), f"{stat!r}")
            # 把会话 cookie 放回去，避免这个套件跑完浏览器处于半登录状态。
            await send(ws, "Network.setCookie", {
                "name": TOKEN_NAME, "value": token, "domain": "127.0.0.1",
                "path": "/", "httpOnly": True, "sameSite": "Lax"})

            # 21. v146：后台管理是**另一个页面**（/admin，不在 index 里），
            #     原来只有一套 212px 侧栏 + 主区的桌面布局：390 宽时侧栏吃掉
            #     一半，主区剩 178，卡片和六列表格全被裁在右缘外。
            #     窄屏改成顶栏 + 通栏，表格自己横滑。
            #     放在最后跑：这一条要导航到 /admin，之后不再回主页面。
            adm = next((u for u in users.values() if u.get("role") == "admin"),
                       None)
            if adm:
                atok = store.issue_token(adm["id"], ua="ui-admin")
                try:
                    await send(ws, "Network.setCookie", {
                        "name": TOKEN_NAME, "value": atok,
                        "domain": "127.0.0.1", "path": "/",
                        "httpOnly": True, "sameSite": "Lax"})
                    await send(ws, "Emulation.setDeviceMetricsOverride", {
                        "width": 390, "height": 844, "deviceScaleFactor": 1,
                        "mobile": True})
                    await send(ws, "Page.navigate", {"url": base + "/admin"})
                    await asyncio.sleep(2.4)
                    admq = await js(ws, """(() => {
                      const a = document.querySelector('aside');
                      const m = document.querySelector('main');
                      if (!a || !m) return null;
                      const ar = a.getBoundingClientRect();
                      const mr = m.getBoundingClientRect();
                      const cards = [...document.querySelectorAll('.cards .card')];
                      const xs = new Set(cards.map(e =>
                        Math.round(e.getBoundingClientRect().left)));
                      return {
                        over: Math.round(document.documentElement.scrollWidth
                                         - document.documentElement.clientWidth),
                        asideW: Math.round(ar.width),
                        asideBottom: Math.round(ar.bottom),
                        mainTop: Math.round(mr.top),
                        cards: cards.length, cols: xs.size};
                    })()""")
                    check("窄屏（390）后台管理不横滚：侧栏变顶栏、卡片两列",
                          admq and admq.get("over") == 0
                          and (admq.get("asideW") or 0) >= 350
                          and (admq.get("asideBottom") or 999)
                          <= (admq.get("mainTop") or 0) + 2
                          and (admq.get("cards") or 0) >= 4
                          and admq.get("cols") == 2, admq)
                finally:
                    store.revoke_token(atok)
                    await send(ws, "Emulation.setDeviceMetricsOverride", {
                        "width": 1440, "height": 900, "deviceScaleFactor": 1,
                        "mobile": False})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:          # noqa: BLE001
            proc.kill()
        # 自己起的服务必须自己收掉，否则跑一次留一个，端口越攒越多。
        stop_service(svc)
        # 自检会话必须清掉：它不是用户的数据，留在历史里会被人当成真会话。
        try:
            for sid in [seed_sid] + created:
                # 走 delete_session 而不是手写表名清单：原来那张清单只列了
                # messages/turns/feedback/sessions，**漏了 traces 和 case_slots**,
                # 于是每跑一次套件就往交付库里留几条孤儿轨迹。
                mem.delete_session(sid)
            # v104 起 /api/tasks 会把无归属会话自动收进「历史咨询」任务。
            # 只删会话不删任务，库里就会攒下一串空任务（实测跑两次剩 2 个）。
            if _created_user is not None:
                for t in mem.list_tasks(_created_user, include_archived=True):
                    mem.delete_task(t["task_id"], _created_user)
            mem._conn.commit()
        except Exception as e:     # noqa: BLE001
            print(f"  [WARN] 自检会话没清干净：{e}")
        # 自检账号删掉。从 v90 起这个账号**一定是我们自己的**（ui-check-demo），
        # 不再借用库里现成的账号，所以删它不会误伤别人的数据。
        # 这条边界以前是靠「_created_user 可能为 None」把住的，现在靠
        # 「只建自己的、只删自己的」——后者更结实，因为它不依赖调用顺序。
        if _created_user is not None:
            try:
                store._conn.execute("DELETE FROM auth_tokens WHERE user_id=?",
                                    (_created_user,))
                store._conn.execute("DELETE FROM users WHERE id=?",
                                    (_created_user,))
                store._conn.commit()
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] 临时账号没清干净：{e}")
        # 零数据 / 首登这两个账号在中途各自删过一次，走到这里通常已经不存在；
        # 只有「中途抛异常」才会漏下来。DELETE 不存在的行是空操作，无副作用。
        for uid in _temp_users:
            try:
                store._conn.execute("DELETE FROM auth_tokens WHERE user_id=?",
                                    (uid,))
                store._conn.execute("DELETE FROM users WHERE id=?", (uid,))
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] 临时账号 {uid} 没清干净：{e}")
        try:
            store._conn.commit()
        except Exception:          # noqa: BLE001
            pass
        # 自检令牌也回收。`issue_token` 只清**过期**的，三天内反复跑这个套件，
        # 令牌表会攒下一堆仍然有效的自检凭据——交付库里不该有这种东西。
        try:
            store.revoke_token(token)
        except Exception as e:     # noqa: BLE001
            print(f"  [WARN] 自检令牌没回收：{e}")

    print()
    if fails:
        print(f"{len(fails)} 项失败：" + "、".join(fails))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
