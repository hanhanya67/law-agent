# -*- coding: utf-8 -*-
"""
静态检查三个页面：JS 里引用的元素 id 是否都真实存在。

为什么需要它：前端是原生 JS、没有构建步骤，`$('u-pager')` 写错或
忘了在 HTML 里加对应的 div，编辑器不会报错、页面也不报错——只是那个
功能静默失效（分页按钮永远不出现）。这类错误只能靠「引用与定义对照」抓。

顺带检查另外两类同样静默的问题：
  · HTML 里定义了但 JS 从不引用的 id（多半是改名后的残留）
  · 页面里引用的 /api/ 路径是否都在 server.py 里注册过

跑法：
    ./.venv/Scripts/python.exe scripts/check_pages.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# 动态拼出来的 id（$('v-' + view) 这种），静态分析看不见，这里手工登记。
DYNAMIC = {
    "v-overview", "v-users", "v-sessions", "v-logins", "v-audits",
}

_ID_DEF = re.compile(r"""id\s*=\s*["']([\w-]+)["']""")
# v104：第三类引用方式 querySelector('#id') 原来不在网里。抽屉改页面那一版
# 删了 #drawer / #slots，而代码里用 querySelector 取它们的地方不会被这条
# 门禁发现——门禁只认 $() 和 getElementById()，全绿，但页面上是断的。
_ID_REF = re.compile(
    r"""(?:\$\(|getElementById\()\s*["']([\w-]+)["']"""
    r"""|querySelector\(\s*["']#([\w-]+)["']""")


def _refs(text: str) -> set[str]:
    """取出所有被引用的 id。两种写法共用一条正则，所以结果要摊平。"""
    flat = {(a or b) for a, b in _ID_REF.findall(text)}
    # 去掉以 '-' 结尾的捕获：那是 $('v-' + view) / '#p-' + p 这种拼接的前半截，
    # 不是完整 id。登记在 DYNAMIC 里的才作数。
    return {r for r in flat if not r.endswith("-")}


def check_page(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    defined = set(_ID_DEF.findall(text)) | DYNAMIC
    problems = []
    for r in sorted(_refs(text) - defined):
        problems.append(f"引用了不存在的 id：#{r}")
    # 只报「引用没定义」，不报「定义没引用」——后者常是留给 CSS 用的，误报率高。
    return problems


def check_api_paths() -> list[str]:
    server = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    # v104：原来只收 get/post。DELETE /api/chat/session/{id} 这类新路由
    # 前端一引就被判「未注册」或（更糟）压根没人查——两种都不对。
    routes = set(re.findall(
        r'@app\.(?:get|post|put|patch|delete)\("([^"]+)"\)', server))
    # 把 {xxx} 通配归一成 *，便于和前端拼出来的路径比对
    norm = {re.sub(r"\{[^}]+\}", "*", r) for r in routes}

    problems = []
    # 独立判例接口是给「只要决策卡」的调用方用的，页面不一定引用它，
    # 上面那条「页面引用 → 已注册」是单向检查，抓不到它被悄悄删掉。
    # 同时钉住共用取数函数：两条路由各写一套的话，结局计数迟早长歪。
    if "/api/case/decision" not in routes:
        problems.append("server.py 缺 /api/case/decision（独立判例接口）")
    if "_case_extras_data" not in server:
        problems.append("server.py 缺 _case_extras_data：两个 case 接口会各写一套取数逻辑")
    # v118：网络状态接口页面不一定引用（状态是 /api/status / /api/meta
    # 带下来的），单向路由检查抓不到它被删；启动时后台预热也一起钉住，
    # 否则首次 DNS 会落到第一个提问的人身上。
    if "/api/network" not in routes:
        problems.append("server.py 缺 /api/network（网络自动检测状态接口）")
    if "start_background_probe" not in server:
        problems.append("server.py 缺 start_background_probe：首次探测会拖慢第一问")
    # v124：管理员不能自助注册。注册接口必须把 role 写死为 user，且绝不
    # 从请求体里读 role —— 否则构造一个带 role=admin 的 POST 就能自提权。
    # 前端收窄入口只是体验，真正的边界在服务端这一行。
    m_reg = re.search(
        r'@app\.post\("/api/auth/register"\)([\s\S]{0,1600}?)(?=\n@app\.)',
        server)
    if not m_reg:
        problems.append("server.py 找不到 /api/auth/register 的函数体")
    else:
        reg_body = m_reg.group(1)
        if 'role="user"' not in reg_body:
            problems.append("register 没有把 role 写死为 user，管理员可被自助注册出来")
        if re.search(r'payload\.get\(\s*["\']role["\']', reg_body):
            problems.append("register 从请求体里读 role，存在自提权风险")
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for m in re.finditer(r"""['"](/(?:api|admin|login)[\w/\-]*)['"]""", text):
            p = m.group(1)
            if p in ("/admin", "/login"):
                continue                        # 页面路由，不是 API
            # 以 / 结尾的是拼接前缀（'/api/admin/users/' + id + '/status'），
            # 单独判断没有意义——最终拼出来的路径由调用处决定。
            if p.endswith("/"):
                continue
            if p not in norm and (p + "/*") not in norm:
                problems.append(f"{page.name} 引用了未注册的接口：{p}")
    return problems


def check_transition_css() -> list[str]:
    """切页过场（v107：载入遮罩 + 9 场景小剧场 + 分层淡入）的静态对账。

    为什么这条要单独查：过场是「看得见但看不见」的东西——CSS 里少一条 @keyframes、
    JS 里少挂一个类，页面不会报错，只会安静地退回硬切或者干脆不动。这种回归
    只能靠对账，靠人眼是发现不了的。

    v107 = 遮罩停留 3–4s，随机播 9 个纯 CSS 小场景之一（翻法典/仓鼠跑轮/天平/
    卷宗/盖章/咖啡/放大镜/打字/猫压卷宗），进度条按真实时间推进，揭开时目标页
    按递增延迟分层淡入。v105 的 View Transitions 共享元素变形已彻底移除。
    """
    css = (STATIC / "assets" / "theme.css").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    problems = []

    # 1. 遮罩本身：必须存在，且底色是页面同色（否则会像半透明蒙层而不是「另一页」）。
    if not re.search(r"\.pgt\s*\{", css):
        problems.append("theme.css 缺 .pgt 遮罩样式")
    if "background:var(--bg)" not in css.replace(" ", ""):
        problems.append("theme.css 的 .pgt 没用页面同色底，揭开会像蒙层")
    # 2. 九个小场景各自的关键帧都得在；少一个那场景就静止成一幅画。
    for kf in ("s-flip", "s-spin", "s-tilt", "s-drop", "s-stamp",
               "s-steam", "s-scan", "s-type", "s-tail"):
        if f"@keyframes {kf}" not in css:
            problems.append(f"theme.css 缺 @keyframes {kf}，第 {kf} 号场景会静止")
    # 3. 分层淡入的关键帧必须在（JS 靠内联 animation 引用它）。
    if "@keyframes pg-in" not in css:
        problems.append("theme.css 缺 @keyframes pg-in，揭开时不会分层淡入")

    # 4. HTML 结构：遮罩、进度条、百分比、旁白都得有；场景至少 8 个。
    for token in ('id="pgt"', 'id="pgt-fill"', 'id="pgt-pct"', 'id="pgt-cap"',
                  'class="pgt-scene"'):
        if token not in html:
            problems.append(f"index.html 缺遮罩零件 {token}")
    scenes = html.count('class="pgt-scene"')
    if scenes < 8:
        problems.append(f"只写了 {scenes} 个切页场景，需求是 8–9 个")
    if html.count("data-cap=") < scenes:
        problems.append("有场景没配 data-cap 旁白")

    # 5. JS 时序链路：遮罩开关、换页、分层错开、随机选场景都得接上。
    for fn in ("_playPageTransition", "_staggerReveal", "_pgtCancel",
               "_pgtPick", "_pgtBusy"):
        if fn not in html:
            problems.append(f"index.html 缺 {fn}，切页时序链断裂")
    if "_playPageTransition(name)" not in html:
        problems.append("showPanel 没有走 _playPageTransition")
    # 时长在 0.7–1.0s 区间。v108 把需求从「三到四秒」改成「一秒左右」，
    # 断言跟着改——但不是改松，而是把上下界都钉住：只写「别超过 1s」的话，
    # 顺手改成 50ms 也能过，那等于没有过场，换页会直接白闪一下。
    if not re.search(r"700\s*\+\s*Math\.round\(\s*Math\.random\(\)\s*\*\s*300", html):
        problems.append("切页时长不是 700–1000ms 随机（需求是一秒左右）")
    # 6. 「前快后慢」的层次：延迟必须随序号增长（Math.pow / i * i 之类），
    #    固定间隔只是等距排队，没有纵深。
    if not re.search(r"Math\.pow\(\s*i\s*,", html):
        problems.append("分层淡入的延迟不是按序号递增（前快后慢），会变成等距排队")
    # 遮罩缩短后最容易漏的一处：只改遮罩、不改分层淡入。那样遮罩一秒就撤了，
    # 页面还要再进两秒，体感比原来更慢。单块时长上限必须一起收紧。
    m_du = re.search(r"var\s+du\s*=\s*Math\.min\((\d+)\s*\+", html)
    if not m_du or int(m_du.group(1)) > 200:
        problems.append("分层淡入单块时长没随遮罩一起收紧（上限应在 200ms 左右）")

    # 7. 时效日历（v108，v114 起常驻）。静态只查「还在不在」，真渲染由
    #    check_ui 验。这条防的是改动时把整块删掉而没人发现——日历是纯 JS
    #    生成的，少了它 check_pages 原来一句都不会报，页面上只剩一行日期。
    for token in ("buildDeadlineCal", "_paintCal", "_calShift", "_calPick",
                  "_calClear", "_calMove", "_calKey", "tabIndex",
                  'id="dl-cal"', "cal-d", "cal-lg", "cal-jump"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，时效日历不完整")
    for cls in (".cal-d.dl", ".cal-d.range", ".cal-d.past", ".cal-d.today",
                ".cal-d.we", ".cal-d.sel"):
        if cls not in html:
            problems.append(f"缺日历标记样式 {cls}，特殊日子会看不出来")
    # 日历必须是**页面上的**常驻块，不是算完才 append 进结论卡里的节点。
    # 判据取 buildDeadlineCal 有没有返回节点：返回节点就说明调用方还要自己
    # 往 DOM 里塞，那它迟早又会退回到「没有结果就没有日历」的老样子。
    m_bd = re.search(r"function buildDeadlineCal\([\s\S]{0,900}?\n\}", html)
    if m_bd and re.search(r"\breturn host\b", m_bd.group(0)):
        problems.append("buildDeadlineCal 又在返回节点：日历应常驻 #dl-cal，"
                        "不该由调用方 append")

    # 7. 图片证据（多模态，v115）。同样只查静态符号；真链路在 check_ui 里跑。
    for token in ("ev-open", "ev-mask", "ev-file", "readEvidence",
                  "_evRender", "_evConfirm", "/api/evidence/extract"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，传图读案情不完整")
    # 这条是本轮真踩到的坑，必须单独立一条：`.ev-mask` 用了 display:flex，
    # 它的优先级高于 hidden 属性自带的 display:none，于是遮罩永远铺在页面上。
    # 表现不是「一个按钮没反应」，而是**整页所有点击都点在遮罩上**——改密码
    # 弹窗、日期输入、按钮悬停十几条断言一起红，看着像页面框架坏了。
    # 加回这一行之前，先想清楚要不要再经历一次那种排查。
    if ".ev-mask[hidden]" not in html.replace(" ", ""):
        problems.append("缺 .ev-mask[hidden]{display:none}：遮罩会一直盖住整页")

    # 8. 办案包（v116）。三块都是 JS 动态生成的，静态只查符号在不在；
    #    真渲染、勾选、复制由 check_ui 的 11 条断言跑。
    for token in ("exSection", "exTimeline", "exClaims", "exEvidence",
                  "renderExtras", "reloadExtras", "event === 'extras'",
                  "/api/case/extras", ".extras", ".ex-cb", ".ex-tl",
                  ".ex-ctext", ".ex-tag.todo"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，办案包不完整")

    # 8.1 v117 判例参考与显式画像。只查静态符号；真链路在 check_ui 里跑。
    # 注意 dc-sec 是 JS 里 el('details', 'ex-sec dc-sec') 用的 class 名，
    # 不是 CSS 选择器，不带点前缀；写成 ".dc-sec" 会误报缺失。其余 .dc-* 是真样式。
    for token in ("exDecision", "exPersona", "savePersona", "dcStat",
                  "dc-sec", ".dc-case", ".dc-tr", ".dc-stat",
                  "ex-stance", "ex-plain", "/api/persona"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，判例参考/画像不完整")
    # 沿用 details，不新增全屏层：v115 那次 .ev-mask[hidden] 的事故。
    if "dc-mask" in html:
        problems.append("判例卡新增了全屏遮罩，请改回 details 折叠")

    # 8.2 v118 网络自动检测与外部线索。s.network 来自 /api/status，
    # m.network 来自 /api/meta —— 两处都断掉时页面会悄悄退回「猜网络」。
    for token in ("renderExternal", "dc-net", "dc-ext", "dc-unverified",
                  "联网补充线索", "网络（自动检测）", "s.network", "m.network",
                  "external: 1"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，联网状态/线索入口不完整")
    # 外部线索必须留在本机判例卡里，不许再开一层全屏遮罩。
    if "dc-ext-mask" in html or "net-mask" in html:
        problems.append("联网线索新增了全屏遮罩，请改回 details 折叠")

    # 8.3 v119 可插拔法律内核。内核是会话级状态，页面不一定引用 /api/ 路由，
    # 单向的「页面引用的路径服务端要注册」检查抓不到它被删，所以这里把
    # 前端符号与服务端接线分别钉住。
    # v121 起顶栏那颗「当前内核」徽章删了；v122 起输入框旁的内核下拉也删了
    # （切法只剩顶部一条索引），所以这里不再要求 law-profile / pf-sel。
    for token in ("applyProfile", "lawBadge",
                  "currentProfile", "profile: currentProfile", "d.profile_id",
                  "data-profile", "exDomain", "dc-domain", "dm-item",
                  "default_profile", "m.profiles"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，内核选择器/专属块不完整")
    srv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    for token in ("get_profile_index", "session_profile", "set_session_profile",
                  "all_meta()", "_profiles.use", '"profile_id"',
                  '"profiles":', "profile_unsupported"):
        if token not in srv:
            problems.append(f"server.py 缺 {token}，内核会话绑定接线不完整")
    for name in ("profiles.py", "domains.py"):
        if not (ROOT / "app" / name).is_file():
            problems.append(f"缺 app/{name}，可插拔内核层不完整")
    if not (ROOT / "data" / "domain_statutes.jsonl").is_file():
        problems.append("缺 data/domain_statutes.jsonl，接入库语料没随包")
    if "pf-mask" in html or "domain-mask" in html:
        problems.append("内核切换新增了全屏遮罩，请改回 select + details")

    # 8.4 v121/v122 顶部索引条 + 每内核一套皮 + 换法动画。
    # 这几样都是「删掉页面照样能跑、但平台感就没了」的东西，只能靠静态
    # 门禁钉住：索引条、切换入口、圆圈动画，以及每个内核的纸色必须浅/暗
    # 两份都有。
    for token in ("pf-rail", "renderRail", "switchProfile", "data-pid",
                  "startViewTransition", "vc-circle", "--vc-x",
                  # v127：暗色下靠压暗旧帧补对比度，变量名写错不会报错，
                  # 只会安静地退回「看不出来的动画」。
                  "--vc-old-dim", "theme-sw",
                  # v128：换法圆的描边圈。这条最容易「静默失效」——元素在、
                  # 样式在，但 JS 没给它挂 view-transition-name，浏览器就当
                  # 它不存在：画面上不多任何东西，也不报错。
                  "vc-ring", "viewTransitionName", "vc-ring-grow"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，索引条/换法动画不完整")

    # v128：圆心必须回到画面正中。改回「从被点那颗按钮长出来」不会报错，
    # 只会在宽屏上表现成「从上往下刷」——这类退化读代码才看得出来。
    if "function circleFrom()" not in html:
        problems.append("circleFrom() 还带节点参数：换法圆应以画面中心为圆心")
    elif "window.innerWidth / 2" not in html:
        problems.append("circleFrom() 没取画面中心（window.innerWidth / 2）")

    # v128 反向断言：印章已经删了，别再以任何形式回来。
    # 那一版的问题是它压在索引条上——把唯一的法域指示挡住了。
    if "stampProfileSeal" in html or "vc-seal" in html:
        problems.append("换法朱印（stampProfileSeal / vc-seal）已删除，不要加回来")

    # v129 反向断言：换法收尾不许再有「落定」位移。
    # clip 圆铺满是过场的自然终点，之后再让整页上浮几像素，观感就是画面已经
    # 停了又抖一下。用带花括号的写法匹配，注释里提到类名不算违规。
    if ".vc-land{" in html or "@keyframes vc-land" in html:
        problems.append("换法收尾的 6px 上浮（vc-land）已删除，别再加回来")

    # v130：切主题不许「震」、原生部件不许跟着系统走。两条都是「不报错、
    # 只是看着不对」的退化，人眼一天能忽略十次，只能靠对账钉住。
    #  · ::view-transition-group(root) 是浏览器默认那条动画，做的是「旧帧盒子
    #    → 新帧盒子」的插值。两帧尺寸一样时它是恒等的，看不出问题；尺寸差一点
    #    （换机器、换 DPI、滚动条宽度不同）整页就会被挤一下再弹回。属于「换台
    #    机器才复现」的那一类，本地跑一百次也未必撞上，所以只能静态禁掉。
    #  · color-scheme 不看 data-theme。不显式写，Windows 开着深色模式时日读
    #    页面会配一条深色滚动条、深色下拉箭头——用户说的「还是跟随系统」多半
    #    就是这些原生部件。
    if "::view-transition-group(root){ animation:none; }" not in html:
        problems.append("root 的 group 动画没关：两帧宽度不同时整页会被横向挤压（震一下）")
    for token in ("color-scheme:light", "color-scheme:dark"):
        if token not in css:
            problems.append(f"theme.css 缺 {token}：原生滚动条/控件会按系统配色走，"
                            "看起来像还在跟随系统")
    # 设置里那行「配色」必须把当前档位写出来。只写「切换」时用户会怀疑背后还有
    # 一档（旧版本确实是三态，第三档就叫「跟随系统」），而它现在是两态。
    if "data-theme-now" not in html:
        problems.append("设置里的「配色」没写出当前档位（缺 data-theme-now）")

    # v128：收尾必须同时挂 finished 和 catch。只写 then 的话，一次没有后继的
    # 取消（页面转后台、或换法途中切主题）会把 view-transition-name 留在环上，
    # 之后每次过场都把这个圆环一起快照进去——屏幕上凭空多一圈线，而且不报错。
    if ".then(settle).catch(settle)" not in html:
        problems.append("switchProfile 的收尾没覆盖 cancel 分支："
                        "过场被取消时描边环会残留 view-transition-name")

    # 变量光是存在不够——它得真的接到 brightness() 上，否则暗色下那条
    # 「压暗旧帧」的补丁等于没写（JS 写着 .66，CSS 不消费，动画照旧看不见）。
    if "brightness(var(--vc-old-dim" not in html:
        problems.append("vc-out 没有把 --vc-old-dim 接进 brightness()，"
                        "暗色下换法仍然看不出圆在推进")

    # v122 反向门禁：这三段文案是用户点名要删的产品自我介绍，很容易在下一次
    # 「补点说明」时被写回来。它们不像功能那样一删就报错，所以只能这样钉。
    # 只看「活代码」——注释里写着「这里原来有一句 XXX」是正常的注释，
    # 拿整份文件做子串匹配会把这种解释也判成违规。
    live = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    live = re.sub(r"/\*.*?\*/", " ", live, flags=re.S)
    live = re.sub(r"^\s*//.*$", " ", live, flags=re.M)
    for junk in ("一个引擎挂多个内核", "点下面的例子可以直接试",
                 "出了什么事，我按现行法条给你找依据"):
        if junk in live:
            problems.append(f"index.html 又出现了说明性文案「{junk}」，"
                            f"聊天区应保持干净")
    # 语料统计那行是拼出来的，按拼装痕迹查：一旦有人把「条文 N 条 ·
    # M 部法律」写回来，这里立刻红。
    if "部法律 · 引用边" in live:
        problems.append("index.html 又渲染了「条文 N 条 · M 部法律 · 引用边」"
                        "统计行，它只在示例语料告警时才该出现")
    # 示例入口只留正文那四张卡。输入框底下再排一串小药丸，就是同一屏把
    # 同样四句话说了两遍，还多一个「点卡还是点药丸」的选择。
    if ".hint .ex" in live or "el('b', null, '示例')" in live:
        problems.append("输入框底下又排出了示例药丸（.hint .ex / 「示例」标签），"
                        "示例入口只留正文的四张卡")

    # 免责声明不能跟着「删说明文字」一起删掉：宽屏靠侧栏 .nav-tip，窄屏
    # （640px 以下 .nav-tip 被隐藏）靠正文 .disclaim。两份少了任何一份，
    # 就有一档屏幕在给法律意见却没有任何声明。
    for sel, why in ((".nav-tip", "宽屏侧栏"), (".disclaim", "窄屏正文")):
        if sel not in html:
            problems.append(f"index.html 缺 {sel}（{why}免责声明）：法律建议"
                            f"必须有一处可见的免责声明")

    # 每个内核必须：主色 + 纸色都在，且浅色/暗色各一份。
    # 只写浅色那套是不够的：theme.css 的 html[data-theme=\"dark\"] 与本文件的
    # [data-pid] 权重相近，暗色模式会被盖回米黄+朱红；而 v122 起纸色也进
    # 了这套令牌，漏一份就不止主色错——整页还是上一部法的背景。
    prof_mod = (ROOT / "app" / "profiles.py").read_text(encoding="utf-8")
    pids = re.findall(r'^    "(\w+)": LawProfile\(', prof_mod, re.M)
    if len(pids) < 5:
        problems.append(f"profiles.py 只解析出 {len(pids)} 个内核，索引条会缺项")
    for pid in pids:
        light = f'[data-pid="{pid}"]{{'
        if light not in html:
            problems.append(f"index.html 缺 {light}：该内核没有独立配色")
            continue
        dark = f'html[data-theme="dark"][data-pid="{pid}"]'
        dark_desc = f'html[data-theme="dark"] [data-pid="{pid}"]'
        if dark not in html or dark_desc not in html:
            problems.append(f"index.html 缺 {dark} / 后代选择器：暗色下这个内核"
                            f"会退回 theme.css 的米黄朱红")
        # 取这一段的所有声明，逐项要求纸色也在（v122 的核心诉求）
        seg = html.split(light, 1)[1].split("}", 1)[0]
        for tok in ("--accent:", "--bg:", "--panel:"):
            if tok not in seg:
                problems.append(f"[data-pid=\"{pid}\"] 缺 {tok}：这套皮只换了"
                                f"主色，背景还是上一部法的纸色")

    # 8.4b v126：只有「仲裁申请书」仍是劳动专属。
    # 「时效计算」已按法域分表（劳动一年 / 其余民法典三年），入口放开；
    # 「案情档案」在 v138 起也放开（接入内核有自己的要素，可看可改，
    # 字段表按内核分流）；但任务页与时效页都不给切法入口，避免「表上算的
    # 是哪部法」对不上。
    if "panelAllowed(" not in html:
        problems.append("index.html 缺 panelAllowed()，劳动专属入口挡不住快捷键")
    # 侧栏收起用的是 :not([data-profile="labor"])，写成 :not(.labor) 之类
    # 不会报错，只是永远不匹配——所以按整串查。
    for panel in ("doc",):
        sel = ('body[data-profile]:not([data-profile="labor"]) '
               f'.nav-i[data-panel="{panel}"]')
        if sel not in html:
            problems.append(f"index.html 缺 {sel} 的隐藏规则：非劳动法内核下"
                            f"仍能点进「{panel}」拿到劳动法内容")
    # v138：案情档案入口已对非劳动内核放开（改由 data-labor-only 藏「生成
    # 申请书」），不能再有整页收起的规则——否则接入内核的要素没地方看。
    slots_sel = ('body[data-profile]:not([data-profile="labor"]) '
                 '.nav-i[data-panel="slots"]')
    if slots_sel in html:
        problems.append("案情档案入口仍被非劳动内核收起：v138 起接入内核"
                        "也要在档案页看/改自己的要素")
    gone = ('body[data-profile]:not([data-profile="labor"]) '
            '.nav-i[data-panel="deadline"]')
    if gone in html:
        problems.append("时效计算入口仍被非劳动内核收起：v126 已按法域分表")
    # v126 决策卡与按法域时效：静态符号必须都在，真渲染由 check_ui 跑。
    for token in ("DL_TYPES_CIVIL", "syncDeadlineProfile()", "no-pf-rail",
                  "worthCard(", "comparison", "dl-interrupt", "/api/deadline"):
        if token not in html:
            problems.append(f"index.html 缺 {token}，v126 决策卡/按法域时效不完整")
    if "body.no-pf-rail#pf-rail{display:none" not in html.replace(" ", ""):
        problems.append("任务页/时效计算页仍会露出切法索引条")
    dl_src = (ROOT / "app" / "deadline.py").read_text(encoding="utf-8")
    for token in ("CIVIL_RULES", "rules_for(", "民法典_188", "民法典_194",
                  "民法典_195", "民法典_196", "民法典_199"):
        if token not in dl_src:
            problems.append(f"deadline.py 缺 {token}，通用诉讼时效规则不完整")

    # 8.5 产品名一致性。v121 之前三个页面的 <title> 都还自称「劳动法智能咨询
    # Agent」，而 h1 早就写着「多领域法规问答」——代码里改十遍，评委看见的
    # 浏览器标签上还是「劳动法」。标题也是最容易漏改的一处：它不在页面上。
    for page in ("index.html", "login.html", "admin.html"):
        txt = (ROOT / "static" / page).read_text(encoding="utf-8")
        m = re.search(r"<title>(.*?)</title>", txt, re.S)
        title = (m.group(1).strip() if m else "")
        if not title:
            problems.append(f"{page} 没有 <title>")
        elif "劳动法智能咨询" in title:
            problems.append(f"{page} 的标签页标题还是「{title}」，"
                            f"产品已改名「多领域法规问答」")
        # 页面上能看见的 H1 / 品牌也要查。改名时最容易漏的就是这两处：
        # 它们不在主界面，评审点开登录页或后台才会看见，而那时已经晚了。
        body = (ROOT / "static" / page).read_text(encoding="utf-8")
        for stale in ("劳动法智能咨询", "劳动法 Agent"):
            if stale in body:
                problems.append(f"{page} 里还留着旧产品名「{stale}」")
    if "document.title" not in html:
        problems.append("index.html 没有按内核改 document.title——"
                        "多开几个标签问不同的法时，标签页上分不出谁是谁")

    # 8.6 README 首屏。评委打开仓库第一眼就是这三行，而它一直不在门禁里：
    # 三个页面的 <title> 从 v121 起就钉住了，README 头部却停在三年前的样子——
    # 标题还叫「劳动法智能咨询 Agent · MVP」、版本写 v0.1、工具写「六个」
    # （实际九个）、整张表没有一个字提五内核。代码改十几版，首屏不动，
    # 等于把最该展示的规模藏起来了。
    head = (ROOT / "README.md").read_text(encoding="utf-8").split("\n")[:12]
    head_txt = "\n".join(head)
    if "多领域法规问答" not in head[0]:
        problems.append(f"README 第一行不是「多领域法规问答 …」：{head[0][:60]}")
    for stale in ("劳动法智能咨询", "MVP", "v0.1", "六个："):
        if stale in head_txt:
            problems.append(f"README 头部还留着旧说法「{stale}」")

    # 9. 无障碍：减少动效时必须真的关掉遮罩的动画/过渡。
    m = re.search(r"@media\s*\(prefers-reduced-motion\s*:\s*reduce\)\s*\{(.*?)\n\}",
                  css, re.S)
    if not m:
        problems.append("theme.css 缺 prefers-reduced-motion 分支")
    else:
        block = m.group(1)
        if "animation:none" not in block.replace(" ", ""):
            problems.append("减少动效分支里没有 animation:none")
        if ".pgt" not in block:
            problems.append("减少动效分支没盖住 .pgt 遮罩")
    return problems


def check_login_v124() -> list[str]:
    """v124 登录页：身份分段 + 自动循环动画 + 管理员不开放注册。

    这几样都是「删掉页面照样能登录、但需求就没了」的东西：身份分段是
    用户点名要的，循环动画是这一版唯一的产品观感，管理员态隐藏注册是
    安全边界的可见部分。静态只钉符号，真交互由 check_ui 跑。
    """
    txt = (STATIC / "login.html").read_text(encoding="utf-8")
    problems = []
    for token in ('id="roles"', 'id="role-user"', 'id="role-admin"',
                  'data-role="user"', 'data-role="admin"',
                  'lg-aurora', 'lg-daynight', 'lg-ring', 'lg-float',
                  'lg-glider', 'lg-role-note',
                  'role === \'admin\'', 'allowRegister'):
        if token not in txt:
            problems.append(f"login.html 缺 {token}，身份分段/循环动画不完整")
    # 管理员态必须真的把注册入口收掉，不能只换一句提示文字。
    if "$('tab-reg').hidden = admin || !allowRegister" not in txt:
        problems.append("login.html 没有在管理员态隐藏注册 tab")
    if "role === 'admin'" not in txt and 'role === "admin"' not in txt:
        problems.append("login.html 没有管理员分支，注册入口挡不住")
    # 循环动画要有「减少动效」降级：项目所有动画的统一约定。
    if "prefers-reduced-motion" not in txt:
        problems.append("login.html 的循环动画没有 prefers-reduced-motion 降级")
    return problems


def check_landing_v124() -> list[str]:
    """v124 空状态首页：法案胶囊 + 大输入框，建任务后切回完整界面。

    最容易出的两种回归都静默：① 有人把 body.blank 改成整块隐藏 .wrap，
    示例卡随之消失、界面交互回归一串断言全红；② setTask() 忘了同步
    blank，第一问建了任务却还停在空状态，答案永远看不见。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    problems = []
    for token in ('id="landing"', 'id="land-pills"', 'id="land-sub"',
                  'land-disclaim', 'land-pill', 'body.blank',
                  'syncLanding()', 'display:contents',
                  "classList.toggle('blank', !currentTask)",
                  'classList.remove(\'blank\')'):
        if token not in html:
            problems.append(f"index.html 缺 {token}，空状态首页不完整")
    # setTask 必须调 syncLanding，否则任务建立后不会切回完整界面。
    m = re.search(r'function setTask\(t\)\s*\{([\s\S]{0,1200}?)\n\}', html)
    if not m or "syncLanding()" not in m.group(1):
        problems.append("setTask() 没有调用 syncLanding()，建任务后仍停在空状态")
    # 示例卡必须留着可点：整块 display:none 会把交互回归一起带走。
    if "body.blank#p-chat.wrap{display:contents}" not in html.replace(" ", ""):
        problems.append(".wrap 在空状态下被整块隐藏，示例卡会一起消失")
    return problems


def check_casekit_v124() -> list[str]:
    """v124 P2/P4：分支流程图、条件式倾向卡、案件级附件开关。

    这三样都是「删掉页面照样能跑」的东西，只能静态钉符号：
      · 图与倾向卡每轮办案包都要渲染；
      · 附件按 task_id 存开关，切任务恢复、删任务级联清；
      · 服务端两个新路由与共用取数函数不能被悄悄摘掉。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    srv = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    problems = []
    for token in ('exFlow(', 'renderInclination(', 'atFlow(',
                  'atProcedureCard(', 'atDocumentCard(', 'loadAttachments(',
                  'onAttachToggle(', '/api/case/attachments',
                  'id="attach-bar"', 'id="attach-body"',
                  'id="at-procedure"', 'id="at-document"',
                  '#attach-bar[hidden]', '.inc', '.flow-paths',
                  'inclination'):
        if token not in html:
            problems.append(f"index.html 缺 {token}，流程图/倾向卡/附件不完整")
    if "exTimeline(d.timeline || {}, d.decision || {})" not in html:
        problems.append("renderExtras 没把 decision 传给时间轴，流程图取不到条件")
    if "if (_attachTid !== currentTask) loadAttachments(currentTask);" not in html:
        problems.append("setTask 没按任务重读附件开关，切案子不会恢复")
    for token in ('/api/case/attachments', 'get_attachments',
                  'set_attachments', '_casekit.build'):
        if token not in srv:
            problems.append(f"server.py 缺 {token}，案件级附件接口不完整")
    mem = (ROOT / "app" / "memory.py").read_text(encoding="utf-8")
    for token in ('task_attachments', 'get_attachment_flags',
                  'set_attachment_flags',
                  'DELETE FROM task_attachments WHERE task_id=?'):
        if token not in mem:
            problems.append(f"memory.py 缺 {token}，附件开关不持久或删任务留孤儿行")
    if not (ROOT / "app" / "casekit.py").is_file():
        problems.append("缺 app/casekit.py，五内核程序表与文书骨架没随包")
    # README 结尾那句「正式提交前必须换成全量文本」是 v124 修掉的假话：
    # 交付语料本来就是 2066 条全量，17 条 seed 只在降级回归里临时挂载。
    # 这句话不报错、不崩页，但复验的人看到会直接怀疑交付的是示例库。
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "正式提交前必须换成" in readme:
        problems.append("README 又写了「正式提交前必须换成全量文本」："
                        "交付语料早已是 2066 条全量，纯自伤")
    return problems

def check_layout_v125() -> list[str]:
    """v125 布局：侧栏常驻、对话框加长、三个功能上移、示例卡收进下拉。

    四种回归都不报错、只是「看起来还行」，只能静态钉：
      ① 有人为了首屏干净又把 .nav 收掉（任务列表凭空消失）；
      ② 输入框被改回 48px，功能条却还按大卡片排；
      ③ 示例卡被 v-if 式重建或整块 hidden，交互回归那串断言断链；
      ④ 「开庭提醒」引了库里没有的条号 —— 这一条直接查交付语料。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    problems: list[str] = []
    for token in ('id="tool-bar"', 'id="tool-verdict"', 'id="tool-procedure"',
                  'id="tool-document"', 'id="tool-panel"', 'id="ex-toggle"',
                  'id="ex-pop"', 'id="exgrid"', 'id="empty"',
                  '#tool-panel[hidden]', '#ex-pop[hidden]',
                  'toggleExPop(', 'syncToolButtons(', 'onToolToggle(',
                  'toggleVerdict(', 'renderVerdictPanel(', 'courtReminder(',
                  '_lastInclination', 'at-remind'):
        if token not in html:
            problems.append(f"index.html 缺 {token}，v125 布局不完整")

    # 侧栏常驻：反向门禁，钉死「为了干净又把左侧收掉」。
    flat = html.replace(" ", "").replace("\n", "")
    if "body.blank{--nav-w:0px" in flat:
        problems.append("body.blank 又把 --nav-w 归零，空状态侧栏会消失")
    if re.search(r"body\.blank\.nav[^{]*\{display:none", flat):
        problems.append("body.blank 又把 .nav 隐藏，任务列表会消失")
    if "left:var(--nav-w);right:0;top:0" not in html:
        problems.append("空状态顶栏没有避开侧栏，会压在导航上")
    if "width:100%;align-self:stretch" not in html.replace(" ", ""):
        problems.append("空状态 footer 没撑满，flex 会把它收缩、输入框又变窄")

    # 对话框加长：两个态都要，只改常规态会让空状态还是一条细缝。
    if not re.search(r"#q\{[\s\S]{0,120}?min-height:(9[6-9]|1\d\d)px", html):
        problems.append("常规态 #q 没加长（应 >=96px）")
    if "min-height:112px;max-height:240px" not in html:
        problems.append("空状态 #q 没加长")

    # 「搬家忘删原件」：两个 id="exgrid" 时 renderExamples() 只填第一个，
    # 正文照样铺四张卡、下拉点开是空的，而 .ex-card.click() 回归仍然全绿。
    # 先剥 HTML 注释：注释里写一句「别留两个 id="exgrid"」也会把自己数进去，
    # 这个坑 v122 的反向门禁踩过一次。
    markup = re.sub(r"<!--[\s\S]*?-->", "", html)
    for dup in ('id="empty"', 'id="exgrid"'):
        if markup.count(dup) != 1:
            problems.append(f"{dup} 出现 {markup.count(dup)} 次，示例卡搬家漏删原件")
    if not re.search(r'id="ex-pop"[^>]*>[\s\S]{0,400}?id="exgrid"', markup):
        problems.append("#exgrid 不在 #ex-pop 里，下拉打开会是空白")

    # 示例卡 DOM 常驻：showEmpty 不能再摘卡片，下拉靠 hidden 控制。
    if "emptyEl.hidden = !on" in html:
        problems.append("showEmpty 又在摘示例卡，下拉点开会是空白")
    m = re.search(r"function renderExamples\(force\)\s*\{([\s\S]{0,1600}?)\n\}", html)
    if not m or "toggleExPop(false)" not in m.group(1):
        problems.append("示例卡点击后没有收起下拉")
    ui = (ROOT / "scripts" / "check_ui.py").read_text(encoding="utf-8")
    if "document.querySelector('.ex-card').click()" not in ui:
        problems.append("交互回归不再点 .ex-card，门禁假设失效（需同步改脚本）")

    # 端点接线：三个按钮都要有 onclick，不能只摆着好看。
    for bid in ('tool-verdict', 'tool-procedure', 'tool-document', 'ex-toggle'):
        if f"getElementById('{bid}').onclick" not in html:
            problems.append(f"#{bid} 没有绑定点击")

    # 开庭提醒只在劳动法内核引《劳动争议调解仲裁法》第三十五条；
    # 条号必须在交付语料里真实存在，否则就是编库外条号。
    corpus = ROOT / "data" / "statutes.jsonl"
    if not corpus.is_file():
        problems.append("缺 data/statutes.jsonl，无法核验开庭提醒的条号")
    else:
        import json
        hit = False
        for line in corpus.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if (d.get("law_short") == "调解仲裁法"
                    and str(d.get("article_no")) == "35"
                    and "开庭五日前" in (d.get("content") or "")):
                hit = True
                break
        if not hit:
            problems.append("开庭提醒引的第三十五条不在交付语料里")
    if "pid === 'labor'" not in html:
        problems.append("开庭提醒没按内核分流，会给非劳动案件串劳动法条")
    return problems


def check_asset_version() -> list[str]:
    """三个页面引用的 /assets 版本号必须一致，且必须写着。

    这是「改了代码但没生效」那一类坑：页面本身走 no-store、每次都是新的，
    脚本却可能被浏览器按启发式新鲜度直接吃掉本地副本——于是**新页面配着
    旧 ui.js 跑**。v127 把主题从「三态循环（日读/夜读/跟随系统）」改成两态
    开关，如果浏览器还在跑旧脚本，点开关就会跳出「跟随系统」那一档，看着
    像开关失灵或者还在跟系统。版本号只是给浏览器一个必须换副本的理由；
    真正的门是 server.py 给 /assets 补的 no-cache。这里钉住的是「三处写在
    一条线上」——免得改了一个漏两个，又回到同一个坑。
    """
    problems: list[str] = []
    seen: dict[str, set[str]] = {}
    for page in ("index.html", "login.html", "admin.html"):
        path = STATIC / page
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for asset, pat in (("ui.js", r"/assets/ui\.js\?v=(\d+)"),
                           ("theme.css", r"/assets/theme\.css\?v=(\d+)")):
            m = re.search(pat, text)
            if not m:
                problems.append(f"{page} 引用 /assets/{asset} 没写 ?v= 版本号")
                continue
            seen.setdefault(asset, set()).add(m.group(1))
    for asset, vers in seen.items():
        if len(vers) > 1:
            problems.append(f"/assets/{asset} 版本号三个页面不一致：{sorted(vers)}"
                            "（改了就一起改，否则总有页面吃到旧副本）")
    return problems


def check_theme_two_modes() -> list[str]:
    """配色只能是日读 / 夜读两档，且**首帧就成立**。

    v131 补的三件事，都是「不报错、只是看着不对」的类型：

    1. 首帧引导脚本。ui.js 在 </body> 之前才执行，那之前整页按默认浅色画过
       一遍——选了夜读的人每次刷新都看到一道白光。更麻烦的是「只有两档」这件
       事在那几十毫秒里并不成立：页面的样子由「默认浅色」决定，而不是由用户
       的选择决定。所以三个页面都在样式表**之前**用一个内联脚本把 data-theme
       钉死，只读 localStorage，一眼都不看系统。
    2. 过场里 old(root) 的动画名要能被关掉（--vc-out-name）。换法需要旧帧
       淡出，换主题不需要——它连着 opacity 和 filter，圆外那部分会先整体变暗
       再被圆盖掉，读起来就是「切换时闪一下 / 震一下」。这条通路一断，两种
       切换就只能共用同一个观感。
    3. 反向断言：任何页面、任何脚本都不许再出现 prefers-color-scheme。
       只有 prefers-reduced-motion 是合法的（那是动效偏好，不是配色）。
    """
    problems: list[str] = []
    texts: dict[str, str] = {}
    for page in ("index.html", "login.html", "admin.html"):
        path = STATIC / page
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        texts[page] = text
        head = text.split("</head>", 1)[0]
        if "localStorage.getItem('law.theme')" not in head:
            problems.append(f"{page} 缺首帧配色引导脚本："
                            "夜读用户每次刷新都会先看到一道白光")

    ui = (STATIC / "assets" / "ui.js").read_text(encoding="utf-8")
    # 只查**代码里的调用**，不查注释——注释里本来就要写「不看 prefers-color-scheme」
    # 来解释为什么删掉它，按字样匹配会把解释当成违规。
    if "matchMedia('(prefers-color-scheme" in ui or \
       'matchMedia("(prefers-color-scheme' in ui:
        problems.append("ui.js 还在问 prefers-color-scheme：配色只允许读 localStorage")
    for page, text in texts.items():
        if "matchMedia('(prefers-color-scheme" in text or \
           "@media (prefers-color-scheme" in text:
            problems.append(f"{page} 还在问 prefers-color-scheme：配色只允许两档")
    # 主题切换那一趟必须把 old(root) 的动画关掉。少了这行，圆外的旧帧会
    # 淡到 88%（--vc-old-op 从来没人设过，恒用兜底值）并被 brightness() 过一遍，
    # 观感就是整页先暗一下再被圆盖掉。
    if "'--vc-out-name', 'none'" not in ui:
        problems.append("换主题时没关掉 old(root) 的动画（--vc-out-name: none）："
                        "圆外的旧帧会先整体变暗，看着像闪一下")

    html = texts.get("index.html", "")
    if "animation-name:var(--vc-out-name,vc-out)" not in html.replace(" ", ""):
        problems.append("index.html 的 old(root) 没接 --vc-out-name："
                        "主题那种「旧帧不动」的切换方式接不上")
    if "'--vc-out-name', 'vc-out'" not in html:
        problems.append("换法没把 --vc-out-name 显式写回 vc-out："
                        "先切主题再换法，旧帧不会淡出")
    return problems


def check_note_before_token_v136() -> list[str]:
    """`note` 事件不许出现在第一个 `token` 之前。

    原因是渲染层：`index.html` 的 `note` 分支是把文本当**纯文本节点** append
    进答案区的（`.answer`），而正文是 `ctx.answer.textContent += d.text` 累加的。
    所以一条在正文之前到达的 note，会被后续 token 卷进答案开头，变成
    「（初判…）【结论】…」粘成一行；复制 / 导出 / 反馈读的都是
    `answer.textContent`，于是这句旁注被当正文带走。

    v135 就这么埋了一句「（初判「clarify」，先按原问检索…）」，v136 删掉。
    当时为什么没被发现：`note` 这条事件在 34 套回归里**零覆盖**，
    `check_ui.py` 只验了 `degraded` 是独立块。这条静态断言补的就是这个盲区。

    要新增「正文之前的提示」，正确做法是像 `degraded` 那样渲染成独立块，
    而不是删掉这条断言。
    """
    problems: list[str] = []
    src = (ROOT / "app" / "agent.py").read_text(encoding="utf-8")
    first_token = src.find('emit("token"')
    if first_token < 0:
        return ["agent.py 里找不到 emit(\"token\")，判据失效"]
    head = src[:first_token]
    if 'emit("note"' in head:
        line = src[:head.find('emit("note"')].count("\n") + 1
        problems.append(
            f"agent.py:{line} 在第一个 token 之前发了 note —— "
            "它会被拼进答案正文开头，且随复制/导出带走")
    return problems


def check_labor_only_v134() -> list[str]:
    """非劳动法内核下，个人档案 / 首登引导 / 新建任务的劳动争议文案要收起。

    后端七处都按内核分流了，前端这三处却写死在 HTML 里：婚家分区点
    「新建任务」弹的仍是「某某公司拖欠工资」，个人档案整列单位信息照常
    显示，首次登录引导第 2 步整页在问「向哪个仲裁委申请」。这类问题不报错、
    也不被 Python 侧那 34 套回归看见，只能靠两层一起守：这里静态钉标记，
    check_ui.py 用真实浏览器验 computed display 和可见文本。
    """
    problems: list[str] = []
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    # 1) CSS 判据：除劳动法外全部隐藏。必须带 body[data-profile] 前缀——
    #    首帧属性还没设上时不能误伤劳动法内核。
    if ('body[data-profile]:not([data-profile="labor"]) '
            '[data-labor-only]{display:none}') not in html:
        problems.append("index.html 缺 [data-labor-only] 隐藏规则，或非劳动内核判据写错")

    # 2) 五个劳动争议字段逐个打标。少一个都会在婚家分区露出来。
    for fid in ("pp-company_name", "pp-company_code", "pp-company_legal_rep",
                "pp-company_address", "pp-commission"):
        m = re.search(r'<div class="fld"([^>]*)>\s*<label for="' + fid + r'"', html)
        if not m:
            problems.append(f"个人档案页找不到 {fid} 的 .fld")
        elif "data-labor-only" not in m.group(1):
            problems.append(f"{fid} 没打 data-labor-only，非劳动内核下仍会显示")

    # 3) 首登引导：两步整页、步骤条两项、tip 里的劳动专属半句。
    for token in ('<div class="onb-page" data-p="1" data-labor-only hidden>',
                  '<div class="onb-page" data-p="2" data-labor-only hidden>',
                  '<span class="onb-step" data-s="1" data-labor-only>',
                  '<span class="onb-step" data-s="2" data-labor-only>'):
        if token not in html:
            problems.append(f"首登引导缺 {token[:52]}，劳动争议步骤没被收起")
    if not re.search(r'<p class="tip">[^<]*<span data-labor-only>', html):
        problems.append("首登引导 tip 没把劳动争议半句包进 data-labor-only")

    # 4) 状态机：非劳动法只留第 0 步。只隐藏不缩步数，用户会在第 2、3 步
    #    看到两张空白页——比串味更像坏了。
    if "ONB_LAST = (currentProfile === 'labor') ? 2 : 0;" not in html:
        problems.append("onbOpen 没按内核重算 ONB_LAST，非劳动内核会走到空白的第 2/3 步")

    # 5) 新建任务与住址 placeholder 不再写死劳动争议例子。
    if "某某公司拖欠工资" in html:
        problems.append("新建任务的 window.prompt 仍写着「某某公司拖欠工资」")
    if "profileName(currentProfile)" not in html:
        problems.append("新建任务的命名提示没按当前内核生成")
    if "申请书要写这一栏" in html:
        problems.append("住址 placeholder 仍写着「申请书要写这一栏」")

    # 6) 反向：标记不等于删字段。labor 内核下十个字段一个都不能少，
    #    否则「按内核收起」会悄悄退化成「把功能砍了」。
    for fid in ("pp-company_name", "pp-company_code", "pp-company_legal_rep",
                "pp-company_address", "pp-commission",
                "pf-company_name", "pf-company_code", "pf-company_legal_rep",
                "pf-company_address", "pf-commission"):
        if f'id="{fid}"' not in html:
            problems.append(f"{fid} 被删了：非劳动内核只该隐藏，不该砍字段")
    return problems


def check_v139() -> list[str]:
    """v139：只有智能咨询能切法案 + 三个新页面接线齐全。

    这一节专治「静态看不出来的接线断裂」：新增 .view 但没进 PANELS，
    页面永远 display:none；或者只把 no-pf-rail 加到新页面上、忘了把
    旧的两页白名单改成全集，用户从档案页照样能换法。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    problems: list[str] = []
    for vid in ("p-library", "p-study", "p-me"):
        if f'id="{vid}"' not in html:
            problems.append(f"缺 {vid} 页面容器")
        if f'data-panel="{vid[2:]}"' not in html:
            problems.append(f"缺 {vid[2:]} 侧栏入口")
    m = re.search(r"var PANELS = \[([\s\S]*?)\];", html)
    if not m:
        problems.append("找不到 PANELS 定义")
    else:
        for name in ("library", "study", "me"):
            if f"'{name}'" not in m.group(1):
                problems.append(f"PANELS 没登记 {name}，页面永远不显示")
    for hook in ("libInit()", "studyInit()", "meInit()"):
        if hook not in html:
            problems.append(f"_afterPanel 没挂 {hook}，进页不拉数据")
    # 接线：三个页面各自的取数函数必须真的定义。
    for fn in ("function libInit(", "function studyInit(", "function meInit(",
               "function setVpid(", "function renderVpid("):
        if fn not in html:
            problems.append(f"缺函数 {fn[:-1]}")

    # 核心 bug：no-pf-rail 必须是「除 chat 外全收」，不能只挡两页。
    m2 = re.search(r"classList\.toggle\('no-pf-rail'\s*,\s*([^)]*)\)", html)
    if not m2:
        problems.append("找不到 no-pf-rail 的 toggle，切法入口没收口")
    else:
        expr = m2.group(1).replace(" ", "")
        if "!=='chat'" not in expr and '!=="chat"' not in expr:
            problems.append(f"no-pf-rail 不是「除 chat 外全收」：{expr}")
        if "==='tasks'" in expr or "==='deadline'" in expr:
            problems.append("no-pf-rail 还是旧的两页白名单，档案/历史页仍能换法")
    # toggle 必须在 panelAllowed 拒绝之后，否则切页被拒也会误收索引条。
    i_reject = html.find("if (!panelAllowed(name))")
    i_toggle = html.find("classList.toggle('no-pf-rail'")
    if not (0 <= i_reject < i_toggle):
        problems.append("no-pf-rail 的 toggle 必须放在 panelAllowed 拒绝之后")

    # switchProfile 入口的硬闸：CSS 挡不住键盘 roving tabindex 和程序调用。
    m3 = re.search(r"function switchProfile\(pid\) \{([\s\S]{0,700}?)"
                   r"if \(pid === currentProfile\)", html)
    if not m3:
        problems.append("找不到 switchProfile 的入口段")
    elif ".view.on" not in m3.group(1) or "p-chat" not in m3.group(1):
        problems.append("switchProfile 缺「非 chat 页直接 return」的 JS 硬闸")

    # 页内法典选择器不许改全局法域。
    m4 = re.search(r"function setVpid\(page, pid\) \{([\s\S]*?)\n\}", html)
    if m4 and ("applyProfile(" in m4.group(1)
               or "currentProfile =" in m4.group(1)):
        problems.append("页内法典选择器改了全局 currentProfile，会串到咨询会话")
    return problems


def main() -> int:
    total = 0
    for page in sorted(STATIC.glob("*.html")):
        probs = check_page(page)
        if probs:
            print(f"[FAIL] {page.name}")
            for p in probs:
                print("   ·", p)
            total += len(probs)
        else:
            n = len(_refs(page.read_text(encoding="utf-8")))
            print(f"[PASS] {page.name}（{n} 处元素引用全部有定义）")

    probs = check_api_paths()
    if probs:
        print("[FAIL] 接口引用")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 页面引用的接口都在 server.py 里注册过")

    probs = check_transition_css()
    if probs:
        print("[FAIL] 切页过场")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 切页过场：遮罩/载入动画/分层淡入/减少动效 都在")

    probs = check_asset_version()
    if probs:
        print("[FAIL] 静态资源版本号")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 静态资源版本号三页一致（不会吃到旧 ui.js）")

    probs = check_theme_two_modes()
    if probs:
        print("[FAIL] 配色两档")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 配色两档：首帧钉死、不看系统、换主题旧帧不动")

    probs = check_login_v124()
    if probs:
        print("[FAIL] 登录页 v124")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 登录页 v124：身份分段/自动明暗/花纹/注册收口 都在")

    probs = check_landing_v124()
    if probs:
        print("[FAIL] 空状态首页 v124")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 空状态首页 v124：胶囊/大输入框/任务切换/示例卡 都在")

    probs = check_casekit_v124()
    if probs:
        print("[FAIL] 流程图/倾向卡/案件附件 v124")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 流程图/倾向卡/案件附件 v124：符号/接口/持久化 都在")

    probs = check_layout_v125()
    if probs:
        print("[FAIL] 对话框布局 v125")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 对话框布局 v125：侧栏常驻/加长/三功能上移/示例下拉 都在")

    probs = check_labor_only_v134()
    if probs:
        print("[FAIL] 劳动争议文案按内核收起 v134")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 劳动争议文案按内核收起 v134："
              "字段打标/引导缩步/命名中性/反向未删 都在")

    probs = check_note_before_token_v136()
    if probs:
        print("[FAIL] 旁注不许抢在正文之前 v136")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 旁注不许抢在正文之前 v136：note 全在 token 之后")

    probs = check_v139()
    if probs:
        print("[FAIL] 只有智能咨询可切法案 / 三个新页面 v139")
        for p in probs:
            print("   ·", p)
        total += len(probs)
    else:
        print("[PASS] 只有智能咨询可切法案 / 三个新页面 v139："
              "全集收口/JS 硬闸/页内选择器不改全局/接线齐全 都在")

    print()
    if total:
        print(f"共 {total} 处问题")
    else:
        print("全部通过")
    return 0 if not total else 1


if __name__ == "__main__":
    sys.exit(main())
