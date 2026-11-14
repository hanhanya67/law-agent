# -*- coding: utf-8 -*-
"""
Web 入口。前端只做一件事：把 Agent 的执行轨迹显示出来。

为什么 SSE 而不是 WebSocket：
  Agent 的输出是单向的、有顺序的、一次请求一批事件。
  SSE 天生就是干这个的，比 WebSocket 少一半代码，断线还能自动重连。
  以后要加"用户中途打断"再上双向通道，现在不需要。

为什么用线程 + 队列桥接：
  AgentRunner.run 是同步阻塞的（LLM 调用、工具执行都是同步的）。
  直接在 async 函数里跑会卡死事件循环。用一个 worker 线程跑 run，
  用 queue 把事件搬回事件循环——这是最简单也最不容易出错的写法。

关于鉴权边界，三条硬规矩：
  1. /api/chat 与 /api/chat/* 必须登录，且**只允许访问自己的会话**。
     不加归属校验的话，session_id 是前端传的，换个 id 就能读别人的咨询记录。
  2. /api/admin/* 每次请求都重新查库确认 role，不信 cookie 里的任何字段
     ——cookie 里只有令牌，角色是查出来的，所以封号/降级对下一个请求即生效。
  3. 页面路由顺手挡一层（/admin 非管理员直接 302 回首页）。这只是体验，
     真正的边界在 API 上：页面是空壳，数据全靠接口。

启动：
  python -m uvicorn app.server:app --reload --port 8000
  然后打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import queue
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from .admin import AdminService
from .agent import AgentRunner, _is_greeting
from .auth import AuthError, AuthStore, PROFILE_FIELDS
from . import deadline as _dl
from . import casekit as _casekit
from . import dslots as _dslots
from . import docgen as _docgen
from . import extras as _extras
from . import facts as facts
from . import health as _health
from . import library as _library
from . import netprobe as _netprobe
from . import persona as _persona
from . import profiles as _profiles
from . import study as _study
from . import vision as _vision
from . import websearch as _websearch
from .config import (ADMIN_PASSWORD, ADMIN_USERNAME, ALLOW_REGISTER,
                     CORPUS_IS_SEED, CORPUS_PATH, CSRF_NAME, DB_PATH,
                     LLM_ENABLED, LLM_RECOVER_AFTER, MODE_NOTE,
                     REG_PER_IP_HOUR, RETRIEVAL_BACKEND, SECURE_COOKIE,
                     STATIC_DIR, TOKEN_NAME, TOKEN_TTL_HOURS, TRUST_PROXY)
from .llm import LLM
from .memory import MemoryStore
from .retrieval import LawIndex, get_profile_index
from .schemas import SLOT_LABELS, CaseSlots

try:
    from fastapi import Depends, FastAPI, Request
    from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                                   StreamingResponse)
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover
    raise SystemExit(
        "缺少 Web 依赖。请先安装：\n"
        "    pip install fastapi uvicorn\n"
        "或者只用命令行版本（不需要任何 Web 依赖）：\n"
        "    python -m app.cli"
    )

app = FastAPI(title="多领域法规智能问答引擎 · 可插拔法律内核", version="0.3.0")

# 后台探一次网络：把 DNS/握手的耗时挪到启动阶段，别留给第一个提问的人。
# 探测只做 TCP 握手、结果进缓存；没探完时一律保守按离线处理。
_netprobe.start_background_probe()

# 前端共用资源（design token + 交互脚本）。单独挂 /assets 而不是整目录挂
# /static：页面本身走 _page() 有 no-store，如果整个 static 目录都暴露，
# 有人能绕开路由直接拿 login.html，虽然无害但没必要开这个口子。
_ASSET_DIR = Path(STATIC_DIR) / "assets"
_ASSET_DIR = Path(STATIC_DIR) / "assets"


class _FreshStatic(StaticFiles):
    """静态资源每次都回源校验。

    默认的 StaticFiles 只给 ETag / Last-Modified，不给 Cache-Control，
    浏览器于是按「启发式新鲜度」（Last-Modified 到现在这段时长的 10%）直接
    用本地副本，连一次请求都不发。后果不是慢，是**跑旧代码**：ui.js 改了、
    HTML 里的 ?v= 忘了同步，用户看到的是新页面配旧脚本——症状离根因很远
    （v127 那次表现为「主题还在跟随系统」）。

    no-cache 不是不缓存：副本留着，但每次先问一句（没变就是 304，几十字节）。
    真正换文件的理由仍然靠 ?v=，这里只是不让「忘了改版本号」变成线上事故。
    """

    async def get_response(self, path, scope):        # noqa: D102
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if _ASSET_DIR.is_dir():
    app.mount("/assets", _FreshStatic(directory=str(_ASSET_DIR)), name="assets")


_index = LawIndex(CORPUS_PATH)
_store = MemoryStore(DB_PATH)
_auth = AuthStore(DB_PATH)
_llm = LLM()
# v139：学习数据全在 law.db，四张新表；备份脚本不用改。
_studydb = _study.StudyStore(DB_PATH)
_runner = AgentRunner(_index, _llm, _store)
_admin = AdminService(_auth, _store)

# 首次启动、库里没有可用管理员时建一个。密码配在环境变量里就用它，
# 没配就随机生成并**只打印这一次**——不落盘、不写日志。
_boot_admin, _boot_pwd = _auth.ensure_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
if _boot_admin:
    print("=" * 66)
    print(f"  已创建首个管理员：{_boot_admin['username']}")
    if _boot_pwd:
        print(f"  一次性密码：{_boot_pwd}")
        print("  只显示这一次，登录后请立刻在「账号」里改掉。")
    else:
        print("  密码取自环境变量 ADMIN_PASSWORD。")
    print("=" * 66)


# ------------------------------------------------------------------ 小工具

def _client_ip(request: Request) -> str:
    """取客户端 IP。默认**不信任** X-Forwarded-For——本地直连时这个头
    可以随便伪造，信了它等于把登录限流拱手让出来（每次换一个 XFF 就绕过）。"""
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _fail(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


# 业务错误码 → HTTP 状态码。没列的一律 400。
_STATUS = {"bad_credentials": 401, "locked": 429, "ip_limited": 429,
           "banned": 403, "no_user": 404, "last_admin": 409}


def _err(e: AuthError) -> JSONResponse:
    return _fail(_STATUS.get(e.code, 400), e.code, e.message)


def _set_cookie(resp: JSONResponse, token: str) -> None:
    # httponly：JS 读不到，XSS 也偷不走。
    # samesite=lax：跨站发起的 POST 不带 cookie，挡掉大部分 CSRF。
    # secure：本地 http 下必须关，否则浏览器直接不存 cookie，
    #         表现是「登录成功但立刻又退出」，很难从现象反推。
    resp.set_cookie(TOKEN_NAME, token, max_age=TOKEN_TTL_HOURS * 3600,
                    httponly=True, samesite="lax", secure=SECURE_COOKIE,
                    path="/")


def _ensure_csrf(resp, request: Request):
    """确保浏览器持有一个 CSRF 令牌。

    双提交的前提是「同源的 JS 读得到、别的站读不到」。所以这个 cookie
    **必须 httponly=False** —— 前端要把它塞进请求头。它不承担保密职责，
    承担的是「跨站拿不到同源 cookie」这件事。
    """
    if not request.cookies.get(CSRF_NAME):
        resp.set_cookie(CSRF_NAME, secrets.token_urlsafe(24),
                        max_age=TOKEN_TTL_HOURS * 3600,
                        httponly=False, samesite="lax", secure=SECURE_COOKIE,
                        path="/")
    return resp


def _verify_csrf(request: Request) -> None:
    """双提交校验：cookie 里的值必须与请求头完全一致。

    为什么只保护「需要登录的写请求」：
      · 登录/注册本身没有会话，没有可被伪造的身份，豁免（代价是被迫登录成
        攻击者账号这类低危场景，而且需要受害者先点开一个恶意页面）。
      · GET 不改变状态，校验它只会误伤（浏览器不会给普通导航带自定义头）。
    """
    c = request.cookies.get(CSRF_NAME, "")
    h = request.headers.get("x-csrf-token", "")
    if not c or not h or not secrets.compare_digest(c, h):
        raise _CsrfBlocked()


def current_user(request: Request) -> dict:
    """依赖：必须登录。令牌每次请求都回库校验，过期/封号/改密都能立刻生效。

    CSRF 校验放在这里，而不是每个接口各写一遍：所有写接口都必然依赖
    current_user，改一处就覆盖全部。漏掉的只可能是「新增的写接口连登录
    都不要求」——那种接口本身就是更大的洞。
    """
    user = _auth.resolve_token(request.cookies.get(TOKEN_NAME))
    if not user:
        raise _Unauthorized()
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        _verify_csrf(request)
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise _Forbidden()
    return user


class _Unauthorized(Exception):
    pass


class _Forbidden(Exception):
    pass


class _CsrfBlocked(Exception):
    pass


@app.exception_handler(_Unauthorized)
def _on_unauth(request: Request, exc: _Unauthorized):
    return _fail(401, "unauthorized", "请先登录")


@app.exception_handler(_Forbidden)
def _on_forbidden(request: Request, exc: _Forbidden):
    return _fail(403, "forbidden", "需要管理员权限")


@app.exception_handler(_CsrfBlocked)
def _on_csrf(request: Request, exc: _CsrfBlocked):
    return _fail(403, "csrf_failed",
                 "请求缺少或错误的 CSRF 令牌，请刷新页面后重试")


def _page(name: str, status: int = 200, request: Request | None = None):
    # no-store：登录页和首页都不能被浏览器缓存，否则退出登录后按返回键
    # 还能看到上一页的内容，用户会以为没退成功。
    resp = FileResponse(Path(STATIC_DIR) / name, status_code=status,
                        headers={"Cache-Control": "no-store"})
    # 打开任何一个页面都补一次 CSRF cookie。这样即使用户清过 cookie、
    # 或者直接跳进来没经过登录流程，前端第一次写请求也不会 403。
    if request is not None:
        _ensure_csrf(resp, request)
    return resp


# ------------------------------------------------------------------ 页面

@app.get("/")
def home(request: Request):
    if not _auth.resolve_token(request.cookies.get(TOKEN_NAME)):
        return RedirectResponse("/login", status_code=302)
    return _page("index.html", request=request)


@app.get("/login")
def login_page(request: Request):
    if _auth.resolve_token(request.cookies.get(TOKEN_NAME)):
        return RedirectResponse("/", status_code=302)
    return _page("login.html", request=request)


@app.get("/admin")
def admin_page(request: Request):
    user = _auth.resolve_token(request.cookies.get(TOKEN_NAME))
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != "admin":
        return RedirectResponse("/", status_code=302)
    return _page("admin.html", request=request)


# ------------------------------------------------------------------ 认证

@app.post("/api/auth/register")
def register(payload: dict, request: Request):
    if not ALLOW_REGISTER:
        return _fail(403, "register_closed", "本站未开放注册，请联系管理员开号")
    ip = _client_ip(request)
    if _auth.count_registrations(ip) >= REG_PER_IP_HOUR:
        return _fail(429, "reg_limited",
                     f"同一地址每小时最多注册 {REG_PER_IP_HOUR} 个账号")
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    try:
        user = _auth.create_user(username, password, role="user", ip=ip)
        # 注册完直接登录：让用户再输一遍同样的账号密码是纯粹的形式主义。
        _, token = _auth.login(username, password, ip,
                               request.headers.get("user-agent", ""))
    except AuthError as e:
        return _err(e)
    resp = JSONResponse({"user": user, "ok": True})
    _set_cookie(resp, token)
    _ensure_csrf(resp, request)
    return resp


@app.post("/api/auth/login")
def login(payload: dict, request: Request):
    try:
        user, token = _auth.login((payload.get("username") or "").strip(),
                                  payload.get("password") or "",
                                  _client_ip(request),
                                  request.headers.get("user-agent", ""))
    except AuthError as e:
        return _err(e)
    resp = JSONResponse({"user": user, "ok": True})
    _set_cookie(resp, token)
    _ensure_csrf(resp, request)
    return resp


@app.post("/api/auth/logout")
def logout(request: Request):
    # 这个接口没走 current_user（没有令牌时也应当安静成功），所以 CSRF 手动校验。
    # 不校验的话，别人可以诱导你访问一个页面，把你的登录态踢掉。
    if request.cookies.get(TOKEN_NAME):
        _verify_csrf(request)
    _auth.revoke_token(request.cookies.get(TOKEN_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(TOKEN_NAME, path="/")
    return resp


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    """登录态 + 档案完成度。

    onboarding 跟着 /me 一起返回，是为了让首页 boot 时只发一个请求就知道
    「要不要弹档案」——多一次往返会让弹窗晚半秒才出来，首页会先闪一下。
    """
    prof = _auth.get_profile(int(user["id"]))
    return {"user": user, "onboarding": prof["onboarding"],
            "profile_filled": prof["filled"]}


@app.post("/api/auth/password")
def change_password(payload: dict, request: Request,
                    user: dict = Depends(current_user)):
    try:
        _auth.change_password(int(user["id"]), payload.get("old") or "",
                              payload.get("new") or "")
    except AuthError as e:
        return _err(e)
    # 改密会让本人所有令牌作废，包括手上这个。所以顺手清 cookie，
    # 让前端明确地跳回登录页，而不是拿着一个已死的令牌继续请求碰 401。
    resp = JSONResponse({"ok": True, "relogin": True})
    resp.delete_cookie(TOKEN_NAME, path="/")
    return resp


# ------------------------------------------------------------------ 个人档案
#
# 为什么档案独立于案情要素（CaseSlots）：要素是「这一个案子发生了什么」，
# 换个案子就全变；档案是「你是谁」，换案子不变。混在一起会导致每开一个
# 新任务都要重填一遍姓名和单位。

@app.get("/api/profile")
def get_profile(user: dict = Depends(current_user)):
    """读自己的档案。这是唯一返回身份证全号的接口——用户要能看见才能改。
    后台审计、日志一律走 mask_id()，不经过这里。"""
    prof = _auth.get_profile(int(user["id"]))
    prof["labels"] = {k: v[0] for k, v in PROFILE_FIELDS.items()}
    prof["limits"] = {k: v[1] for k, v in PROFILE_FIELDS.items()}
    return prof


@app.put("/api/profile")
def put_profile(payload: dict, user: dict = Depends(current_user)):
    """保存档案。支持两种入参：{"profile": {...}} 或直接扁平字段。

    请求体里带的字段才更新，没带的保持原值——设置页一次只改一栏，
    整行覆盖会把它没渲染的字段清空。传空字符串等于清除该项。
    """
    patch = payload.get("profile")
    if not isinstance(patch, dict):
        patch = {k: v for k, v in (payload or {}).items() if k in PROFILE_FIELDS}
    onb = payload.get("onboarding")
    try:
        prof = _auth.save_profile(
            int(user["id"]), patch,
            onboarding=onb if onb in ("done", "skipped") else "done")
    except AuthError as e:
        return _err(e)
    return {"ok": True, "profile": prof}


@app.post("/api/profile/skip")
def skip_profile(user: dict = Depends(current_user)):
    """首次登录选「先跳过，仅咨询」。只改状态，不抹掉已填内容。"""
    return {"ok": True, "profile": _auth.skip_profile(int(user["id"]))}


# ---- v137 用户说过的事实：看得见、能删 ----
@app.get("/api/profile/facts")
def get_profile_facts(user: dict = Depends(current_user)):
    """系统从对话里记下的用户事实。

    这里刻意**不做推断**：每条都带 source（用户原话片段），
    档案页原样列出来，用户点删除就真删——记忆如果不可见、不可改，
    记错了比不记更糟。
    """
    return {"ok": True,
            "items": _store.list_profile_facts(int(user["id"])),
            "labels": facts.FACT_LABELS}


@app.post("/api/profile/facts")
def save_profile_facts(payload: dict, user: dict = Depends(current_user)):
    """改或删一条。value 传空字符串表示删除。"""
    key = str(payload.get("key") or "").strip()
    if key not in facts.FACT_LABELS:
        return JSONResponse({"ok": False, "error": "不认识的字段"},
                            status_code=400)
    patch = {key: {"value": str(payload.get("value") or ""),
                   "source": "用户在档案页修改"}}
    changed = _store.add_profile_facts(int(user["id"]), patch)
    return {"ok": True, "changed": changed,
            "items": _store.list_profile_facts(int(user["id"]))}



# ------------------------------------------------------------------ 对话

def llm_live(snap: dict) -> tuple[str, str]:
    """把运行态压成（状态, 一句话原因）。

    为什么公开页也要知道这个：`mode` 是启动时的配置结论，进程起来就不变了。
    余额被扣光、key 被停，配置那边看起来一切正常，登录页却还在写「LLM 在线」——
    用户注册进来看见每句回答都带「回放」两个字，会觉得被宣传语骗了。

    未知一律当「能打电话但没打过」（idle），不假装正常——和 `/api/status`
    里「一次都没调过就说正常是没根据的」是同一条原则。
    """
    if not LLM_ENABLED:
        return "disabled", ""
    if int(snap.get("calls", 0)) == 0:
        return "idle", ""
    if snap.get("ok"):
        return "ok", ""
    return "error", str(snap.get("text") or "调用失败")


@app.get("/api/meta")
def meta():
    """公开接口，登录页也用它显示「已收录多少条」。

    不返回 fallback_reason——那里面是 LLM 初始化的失败原因，属于运维信息，
    不该给未登录的人看。
    """
    st = _index.stats()
    live, note = llm_live(_health.snapshot())
    # 语料是不是只有示例（17 条 vs 正式 2066 条）。这件事必须说出来：
    # 缺正式语料时程序不报错、页面照常打开，只是答案变成「本库未收录」——
    # 用户看到「条文 17 条」不会知道这有多糟，它不是「少」，是「几乎没有」。
    seed_corpus = CORPUS_IS_SEED
    net = _netprobe.status()
    return {"articles": st["articles"], "laws": st["laws"],
            "citation_edges": st["citation_edges"],
            "retrieval": RETRIEVAL_BACKEND,
            "corpus_seed": seed_corpus,
            "llm_enabled": LLM_ENABLED,
            "mode": MODE_NOTE,
            # 配置上开了不等于现在能用：余额扣光、key 被停都是运行中的事。
            # 登录页那行「LLM 在线」如果只按开关算，用户看到的就是一句假话。
            "llm_live": live,
            # 只给三五个字的类别（余额不足/密钥无效），不给供应商原文。
            "llm_live_note": note,
            "allow_register": ALLOW_REGISTER,
            # 自动检测网络的结果，公开页也能看到「现在是离线还是在线」。
            # 只给结论与类别，不给探测目标主机名（那是运维信息）。
            "network": {"online": bool(net["online"]), "mode": net["mode"],
                        "label": net["label"]},
            # v119：公开接口只给「有哪些内核」和默认内核，不给当前会话
            # 的内核——那是会话级状态，得从 /api/chat/session/{id} 读。
            "product": "多领域法规智能问答引擎 · 可插拔法律内核",
            "profiles": _profiles.all_meta(),
            "default_profile": _profiles.DEFAULT_ID,
            "profile_note": "内核在新建咨询时选择并绑定到该会话；"
                            "系统不根据提问自动猜测领域。"}


@app.get("/api/status")
def health_status(user: dict = Depends(current_user)):
    """运行中的模型状态。登录后才给：错误详情里会带供应商返回的原文。

    和 `/api/meta` 的分工：meta 说「配置上有没有开」，这里说「现在通不通」。
    """
    snap = _health.snapshot()
    # 先铺 snap 再写 state/text：反过来的话 snap 里的同名键会把文案覆盖掉
    # （snap 也有 text/detail 两个键，成功时是空串）。
    out = dict(snap)
    # 判定只留一份：`llm_live()` 是给 meta / 管理概览用的同一套结论。
    # 两边各写一个 if 链，早晚会因为改一处忘另一处而对不上——那正是
    # 「登录页说回放、徽章还说在线」这类 bug 的成因。
    live, note = llm_live(snap)
    if live == "disabled":
        out.update({"state": "replay", "text": "回放模式",
                    "detail": MODE_NOTE, "llm_enabled": False})
    elif live == "idle":
        # 一次都没调过就说「正常」是没根据的。说「尚未调用」才诚实。
        out.update({"state": "idle", "text": "尚未调用", "detail": "",
                    "llm_enabled": True})
    elif live == "error":
        out.update({"state": "error", "text": note or "调用失败",
                    "detail": snap["detail"], "llm_enabled": True})
    else:
        out.update({"state": "ok", "text": "模型正常", "detail": "",
                    "llm_enabled": True})
    # 网络状态和模型状态分开报：有 key 但断网、没 key 但网通是两件不同的事，
    # 揉成一个布尔会让「模型异常」被误读成「网断了」。
    out["network"] = _netprobe.status()
    return out


@app.get("/api/network")
def network_status(refresh: int = 0, user: dict = Depends(current_user)):
    """查看（或手动刷新）自动检测的网络结论。

    默认只读缓存，不发起任何请求 —— 探测在后台跑、结论有 TTL，
    不该每刷一次页面就重探一次。带 `?refresh=1` 才同步重探，
    给「刚插上网线想立刻确认」这种场景用。
    """
    if refresh:
        _netprobe.probe(force=True)
    return {"network": _netprobe.status(),
            "external_search_enabled": _websearch.enabled(),
            "note": "自动检测网络不等于自动联网：外部检索还需 "
                    "CASE_SEARCH_ADAPTER=1 并在请求里显式带 external=1。"}


@app.get("/api/chat/history")
def my_history(request: Request, user: dict = Depends(current_user)):
    return {"items": _store.user_sessions(int(user["id"]), limit=60)}


@app.get("/api/chat/search")
def chat_search(q: str = "", user: dict = Depends(current_user)):
    """只搜自己的会话。会话隔离在查询里做，不靠调用方记得传 user_id。"""
    q = (q or "").strip()
    if not q:
        return {"q": q, "items": []}
    return {"q": q, "items": _store.search_sessions(int(user["id"]), q, limit=20)}


@app.get("/api/chat/session/{session_id}")
def my_session(session_id: str, user: dict = Depends(current_user)):
    owner = _store.session_owner(session_id)
    # 严格相等判断：owner 为 None 也拒绝。这条很关键——「无主会话自动认领」
    # 听起来友好，实际是个越权面：谁先拿到这个 id 谁就能把它变成自己的。
    if owner != int(user["id"]):
        return _fail(403, "forbidden", "这个会话不属于当前账号")
    return {"session_id": session_id,
            # 带回任务归属：前端「打开历史会话」时要顺带切到对应任务，
            # 否则接着聊会落到当前那个任务里，两边要素互相污染。
            "task_id": _store.session_task(session_id),
            "profile_id": _store.session_profile(session_id),
            "slots": _store.load_slots(session_id).known(),
            # v138：接入内核的案情要素（争议焦点/主要财产/商品问题…）。和
            # 上面 `slots`（劳动法八项）是两套字段，各存一处、各显一张表。
            # 劳动法内核这里恒为空 dict，前端照旧走 SLOT_DEFS。
            "dslots": _store.load_domain_slots(session_id,
                                              _store.session_profile(session_id)),
            "messages": _store.session_messages(session_id, limit=400),
            # 带上已投过的票：否则用户重开这个会话，点赞状态会全部消失，
            # 他会以为自己没投过，再投一次，库里只是被覆盖——但体验上是「丢了」。
            "votes": _store.session_votes(session_id),
            "traces": _store.traces(session_id, limit=40)}


@app.post("/api/chat/feedback")
def chat_feedback(payload: dict, user: dict = Depends(current_user)):
    """一句话一个态度。value 传空串表示撤销。"""
    sid = (payload.get("session_id") or "").strip()
    question = (payload.get("question") or "").strip()
    value = (payload.get("value") or "").strip()
    if not sid or not question:
        return _fail(400, "bad_args", "session_id 与 question 不能为空")
    if value not in ("", "up", "down"):
        return _fail(400, "bad_value", "value 只能是 up / down / 空串")
    if _store.session_owner(sid) != int(user["id"]):
        return _fail(403, "forbidden", "这个会话不属于当前账号")
    turn_id = payload.get("turn_id")
    _store.save_feedback(sid, question, int(user["id"]),
                         turn_id if isinstance(turn_id, int) else None,
                         value, payload.get("answer") or "")
    return {"ok": True, "value": value}


_SLOT_TEXT_LIMIT = 100
_SLOT_WAGE_MAX = 10_000_000


def _coerce_slot(key: str, raw: object) -> object:
    """把前端送上来的字符串转成 CaseSlots 认的类型。

    为什么不在前端转好再发：前端是三个页面共用的，任何一处漏转，
    写进库的就是脏值（例如 monthly_wage 存成 "8000元"），而 Agent 后面
    拿它去算赔偿时会静默出错。类型校验只在服务端做一次，就只有一个真相。

    空字符串一律转成 None（= 清除该字段），不是存空串。空串会让
    `known()` 认为「这个字段已知」，追问逻辑就不再问了。
    """
    if key not in CaseSlots.model_fields:
        raise ValueError(f"未知字段：{key}")
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        return None

    if key == "monthly_wage":
        s = str(raw).replace(",", "").replace("，", "").replace("元", "").strip()
        try:
            v = int(float(s))
        except ValueError:
            raise ValueError("月工资要填数字，比如 8000") from None
        if not 0 < v <= _SLOT_WAGE_MAX:
            raise ValueError(f"月工资 {v} 看起来不对，请填 1 到 {_SLOT_WAGE_MAX} 之间")
        return v

    if key == "has_contract":
        t = str(raw).strip().lower()
        if t in ("1", "true", "是", "签了", "有", "yes", "y"):
            return True
        if t in ("0", "false", "否", "没签", "没有", "no", "n"):
            return False
        raise ValueError("是否签了书面合同只能填「是」或「否」")

    if key in ("start_date", "end_date"):
        d = _dl.parse_date(str(raw))
        if d is None:
            raise ValueError(f"{SLOT_LABELS.get(key, key)} 看不懂，"
                             f"请填 2024-03 或 2024-03-15")
        # 用户只给到月，就存到月——存成 1 号会让后面的期间计算凭空精确一天。
        txt = str(raw).strip()
        return d.isoformat() if len(txt) > 7 else d.strftime("%Y-%m")

    return str(raw)[:_SLOT_TEXT_LIMIT]


@app.post("/api/chat/session/{session_id}/slots")
def update_slots(session_id: str, payload: dict,
                 user: dict = Depends(current_user)):
    """手工修正案情要素。

    为什么要有这个入口：Agent 从对话里抽的槽位会抽错（把「试用期三个月」
    当成入职日期），而用户没有任何手段纠正，只能反复解释。改一次库里的值，
    比让他重说一遍便宜得多。

    代价要说清：手改之后，Agent 会把这当成既定事实。所以返回 changed，
    前端必须把「改了哪几项」明明白白显示出来。
    """
    if _store.session_owner(session_id) != int(user["id"]):
        return _fail(403, "forbidden", "这个会话不属于当前账号")

    patch: dict[str, object] = {}
    try:
        for k, v in (payload or {}).items():
            patch[k] = _coerce_slot(k, v)
    except ValueError as e:
        return _fail(400, "bad_slot", str(e))
    if not patch:
        return _fail(400, "empty_patch", "没有要更新的字段")

    slots = _store.load_slots(session_id)
    # 这里不能直接用 slots.merge(patch)：`merge` 会 **跳过 None**，那是给
    # 对话抽取用的语义（这一轮没抽到某个字段，不等于要把已有的清空）。
    # 但手工编辑里「留空」的含义恰恰是「删掉这一项」。两种语义必须分开走，
    # 所以这里逐个键处理。
    changed: list[str] = []
    for k, v in patch.items():
        if getattr(slots, k) != v:
            setattr(slots, k, v)
            changed.append(k)
    if changed:
        _store.save_slots(session_id, slots)
    return {"ok": True, "changed": changed, "slots": slots.known(),
            "labels": SLOT_LABELS}


@app.post("/api/chat/session/{session_id}/dslots")
def update_domain_slots(session_id: str, payload: dict,
                        user: dict = Depends(current_user)):
    """手工修正接入内核的案情要素（v138）。

    和上面 `update_slots` 是平行的一套：那一套管劳动法八项，这一套管接入
    内核自己的字段表。为什么不能复用那套——`_coerce_slot` 里
    `key not in CaseSlots.model_fields` 会直接把接入内核的字段名当「未知字段」
    拒掉，而「月工资」又会被商品价格误命中。

    规则和 dslots.merge 的对话抽取语义相反：这里「留空 = 删掉这一项」，
    是用户主动纠正，不是「这一轮没提到」。字段不在本内核字段表里的一律拒，
    不静默丢——静默丢是这套系统最难查的故障。
    """
    if _store.session_owner(session_id) != int(user["id"]):
        return _fail(403, "forbidden", "这个会话不属于当前账号")
    pid = _store.session_profile(session_id)
    p = _profiles.resolve(pid)
    # labor 走 /slots（CaseSlots 八项），不走这里。虽然它的 slot_labels 也有
    # 八个键（配置留档），但那套字段归 CaseSlots 管，混进来会把「月工资」
    # 和「所在地区」当普通字符串写进 domain_slots，两套库各存一份就串了。
    if pid == "labor":
        return _fail(400, "wrong_slots", "劳动法内核的要素请通过「案情档案」修改")
    labels = _dslots.spec(p)
    if not labels:
        return _fail(400, "no_slots", "当前内核没有可编辑的案情要素")

    cur = _store.load_domain_slots(session_id, pid)
    patch: dict[str, str] = {}
    for k, v in (payload or {}).items():
        if k not in labels:
            return _fail(400, "bad_slot", f"未知字段：{k}")
        s = _dslots.normalize(v)
        # normalize 把空串/「未知」这类占位词也归成空串——这里空串就是删除。
        patch[k] = s
    if not patch:
        return _fail(400, "empty_patch", "没有要更新的字段")

    changed: list[str] = []
    for k, v in patch.items():
        if not v:
            if cur.get(k):
                cur.pop(k, None)
                changed.append(k)
        elif cur.get(k) != v:
            cur[k] = v
            changed.append(k)
    if changed:
        _store.save_domain_slots(session_id, pid, cur)
    return {"ok": True, "changed": changed,
            "dslots": _dslots.known(p, cur),
            "labels": labels}


# ------------------------------------------------------------------ 任务
#
# 任务 = 一个案子。会话挂在任务下，案情要素按任务合并，文书按任务生成。
# 为什么不做成纯前端的「分组标签」：分组标签意味着不同案子的记忆仍然
# 混在一个池子里——那和现在没区别。真正要的是要素、历史、文书三样都不串。

@app.get("/api/tasks")
def list_tasks(include_archived: int = 0, user: dict = Depends(current_user)):
    uid = int(user["id"])
    # 未分类的历史会话默认收进「历史咨询」任务，前端不再出现「未分类」段落。
    _store.assign_loose_to_task(uid)
    items = _store.list_tasks(uid, include_archived=bool(include_archived))
    return {"items": items, "unassigned": []}


@app.post("/api/tasks")
def create_task(payload: dict, user: dict = Depends(current_user)):
    title = (payload.get("title") or "").strip()
    # 任务一建就记下它属于哪部法：空任务没有会话可回落，界面得知道
    # 该给它上哪套颜色。非法取值走 resolve 的兜底，不猜。
    pid = _profiles.resolve(payload.get("profile")).id
    t = _store.create_task(int(user["id"]), title, profile_id=pid)
    return {"ok": True, "task": t}


def _auto_task_title(question: str, profile_name: str = "") -> str:
    """把第一句提问变成任务名。

    为什么只取第一句：用户往往先问一句、再补案情（「公司没签合同能要
    二倍工资吗？我 2024 年 3 月入职……」）。取整段会变成一个摘要，
    而任务卡上只有一行位置，摘要注定被截断成半句话。
    为什么去掉句尾的「吗/呢/吧」：「……能要二倍工资吗」读起来是一句话，
    去掉语气词才像一件要办的事——任务名不是提问的复制品。
    寒暄单独处理：「你好」能当标题但没有任何信息量，叫「劳动用工咨询」
    反而准确，用户接着说的正事也不会被一个叫「你好」的任务框住。

    截断用 20 字：任务卡是两列布局，一行放得下 20 个汉字左右，
    再多就被 CSS 打点，而「…」放在数据库里至少是诚实的截断。
    """
    if _is_greeting(question or ""):
        return (profile_name + "咨询") if profile_name else "新咨询"
    s = re.sub(r"\s+", " ", (question or "").strip())
    # 「请问一下，公司拖欠工资……」里的客套话进标题只会把正题挤到被截断的
    # 后半句去。只在句首剥，句子中间出现的「请问」是内容的一部分。
    s = re.sub(r"^(请问|想问|想咨询|麻烦问|请教|咨询)(一下|一句|下)?[，,：:、\s]*",
               "", s).strip()
    s = re.split(r"[。！？!?\n]", s)[0].strip()
    s = s.rstrip("？?。.!！，,、；;：: “”\"' ").strip()
    for tail in ("吗", "呢", "吧"):
        if s.endswith(tail) and len(s) > 6:
            s = s[:-1]
            break
    if len(s) > 20:
        s = s[:20] + "…"
    return s or (profile_name + "咨询" if profile_name else "新咨询")


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str, user: dict = Depends(current_user)):
    uid = int(user["id"])
    t = _store.get_task(task_id, uid)
    if not t:
        return _fail(404, "no_task", "任务不存在或不属于当前账号")
    return {"task": t,
            "sessions": _store.task_sessions(task_id),
            "slots": _store.load_task_slots(task_id).known(),
            "labels": SLOT_LABELS}


@app.delete("/api/chat/session/{session_id}")
def del_session(session_id: str, user: dict = Depends(current_user)):
    """删除一条历史会话（级联清子表）。校验归属，越权返回 403。"""
    if not _store.delete_session(session_id, int(user["id"])):
        return _fail(403, "forbidden", "删除失败：会话不存在或不属于当前账号")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def del_task(task_id: str, user: dict = Depends(current_user)):
    """删除一个任务及其下全部会话（级联）。校验归属，越权返回 403。"""
    if not _store.delete_task(task_id, int(user["id"])):
        return _fail(403, "forbidden", "删除失败：任务不存在或不属于当前账号")
    return {"ok": True}


@app.patch("/api/tasks/{task_id}")
def patch_task(task_id: str, payload: dict, user: dict = Depends(current_user)):
    uid = int(user["id"])
    if _store.task_owner(task_id) != uid:
        return _fail(403, "forbidden", "这个任务不属于当前账号")
    title = payload.get("title")
    status = payload.get("status")
    if title is None and status is None:
        return _fail(400, "empty_patch", "没有要更新的字段")
    ok = _store.update_task(task_id, uid,
                            title=None if title is None else str(title),
                            status=None if status is None else str(status))
    if not ok:
        return _fail(400, "bad_patch", "更新失败：标题为空或状态不合法")
    return {"ok": True, "task": _store.get_task(task_id, uid)}


@app.post("/api/doc/arbitration")
def make_arbitration(payload: dict, user: dict = Depends(current_user)):
    """按「个人档案 + 整个案子的对话」填一份仲裁申请书骨架。

    故意不生成「请求事项」和「法律依据」：那两处是这份文件里唯一有法律
    后果的部分，让模型代笔等于把责任转移给一个会编的东西。这里只把已知
    事实填进固定位置，把必须本人确认的地方标成待填。

    支持两种范围：
      · task_id    —— 整个任务（多个会话）合并计算，这是默认用法；
      · session_id —— 单个会话（老的快捷入口还这么调）。
    """
    uid = int(user["id"])
    tid = (payload.get("task_id") or "").strip()
    sid = (payload.get("session_id") or "").strip()

    source: dict = {}
    if tid:
        if _store.task_owner(tid) != uid:
            return _fail(403, "forbidden", "这个任务不属于当前账号")
        slots = _store.load_task_slots(tid).known()
        msgs = _store.task_messages(tid, limit=600)
        source = {"task_id": tid,
                  "n_sessions": len(_store.task_sessions(tid)),
                  "n_messages": len(msgs)}
    elif sid:
        if _store.session_owner(sid) != uid:
            return _fail(403, "forbidden", "这个会话不属于当前账号")
        slots = _store.load_slots(sid).known()
        msgs = _store.session_messages(sid, limit=400)
        source = {"session_id": sid, "n_sessions": 1, "n_messages": len(msgs)}
    else:
        return _fail(400, "no_source", "缺少 task_id 或 session_id")

    # n_messages 是「消息条数」，n_turns 才是「轮数」（用户提问数）。
    # 前端收据上要写「N 轮对话」，拿 n_messages 去贴「轮」会翻倍——
    # 用户对着任务卡片上的「5 轮」看到「10 轮」，第一反应是「它多算了一半」。
    # 必须在滤寒暄**之前**算：任务列表卡片上的「N 轮」是含寒暄的全量，
    # 收据写 5、卡片写 6 同样会让人以为漏了一轮。两个界面说同一件事，
    # 就得是同一个数。
    source["n_turns"] = sum(1 for m in msgs
                            if (m.get("role") or "") == "user")

    # 寒暄在进摘录前就滤掉：它们被当成事实陈述写进申请书，比少写一句
    # 更糟——那是当事人自己签字交给仲裁委的东西。
    msgs = [m for m in msgs if not _is_greeting(m.get("content") or "")]
    statements = _docgen.pick_statements(msgs)

    doc = _docgen.arbitration(slots, profile=_auth.get_profile(uid),
                              statements=statements)
    doc["filename"] = _docgen.filename()
    doc["source"] = source
    # 把摘录原文一并返回，前端能逐条列出「引用了你说的哪几句」。
    # 不可核对的东西，用户不会信，也不会改。
    doc["statements"] = statements
    return doc


@app.post("/api/deadline")
def calc_deadline(payload: dict, user: dict = Depends(current_user)):
    """期限推算（v126 起按法域走：劳动争议一年 / 其余四内核民法典三年）。

    为什么单开一个接口：deadline.compute() 早就写好、回归全绿，但此前
    只有 Agent 内部的工具能调——用户想算时效，只能先问一句话，再指望 Agent
    自己想起来去调那个工具。界面上没有任何入口。

    「不给单一日期」这条原则在这里同样成立：起算点有争议时 compute() 返回
    candidates 列表和 conservative 标记，这里原样交出去，由前端全部摆出来，
    不替用户挑一个看起来最舒服的。中止 / 中断只给提示，不改日期。
    """
    dispute = (payload.get("dispute") or "").strip()
    onset = (payload.get("onset") or "").strip()
    pid = _profiles.resolve(payload.get("profile")).id
    if not dispute:
        return _fail(400, "no_dispute", "请选择争议类型")
    if not onset:
        return _fail(400, "no_onset", "请填起算日期")

    # 三态必须原样传下去：True / False / None 各自走不同分支，
    # 用 `payload.get(...) or False` 会把「没说」压成「在职」，那是替用户猜。
    relation_ended = payload.get("relation_ended")
    if relation_ended is None:
        relation_ended = None
    else:
        relation_ended = bool(relation_ended)

    as_of = None
    raw_as_of = (payload.get("as_of") or "").strip()
    if raw_as_of:                       # 只有测试会传，用来固定「今天」
        from datetime import date as _date
        try:
            as_of = _date.fromisoformat(raw_as_of)
        except ValueError:
            return _fail(400, "bad_as_of", "as_of 需要 YYYY-MM-DD")

    out = _dl.compute(dispute, onset, relation_ended=relation_ended,
                      end_text=(payload.get("end_text") or "").strip(),
                      as_of=as_of, profile=pid,
                      interruption=(payload.get("interruption") or "").strip())
    # 状态文案归后端管：前端自己拼一套「紧急/临近」的措辞，早晚会和这里对不上
    out["levels"] = _dl.LEVELS
    return out


@app.post("/api/evidence/extract")
def extract_evidence(payload: dict, user: dict = Depends(current_user)):
    """上传一张截图，读出案情要素，**连同每个字段的原图依据**。

    为什么另开一个接口、而不是让 Agent 自己决定去调读图工具：
    读图的结果必须先落到人眼前确认。直接进槽位的话，模型把 12,000 看成
    8,000，用户完全无从察觉——赔偿金会按错的月薪一路算到底，全程没有
    任何提示。所以流程是三步：

      1. 这个接口：读图 → 返回「字段 + 它来自图里哪句话 + 可靠程度」
      2. 前端：逐项摆出来，默认全勾，用户可以取消
      3. 勾中的走已有的 /api/chat/session/{id}/slots 写进槽位

    第三步复用现成接口是有意的：槽位合并、变更记录、追问逻辑都长在那边，
    再开一条写槽位的路，早晚会和它长出两套行为。
    """
    # v119：图片证据录入是按劳动争议的 8 个案情要素设计的（工资条 / 合同 /
    # 辞退通知）。在别的内核里让它抽出「月工资」「离职方式」会直接串库，
    # 所以明确关掉并说清原因，而不是悄悄返回一堆用不上的字段。
    _sid = (payload.get("session_id") or "").strip()
    _pid = (_store.session_profile(_sid) if _sid else
            _profiles.resolve(payload.get("profile")).id)
    if _pid != "labor":
        return _fail(400, "profile_unsupported",
                     "当前内核不启用图片证据录入：该能力按劳动争议的案情要素设计。"
                     "请切回「劳动用工」内核，或直接用文字描述。")
    raw = str(payload.get("image") or "").strip()
    if not raw:
        return _fail(400, "no_image", "没有收到图片")

    # 前端用 FileReader 读出来是 data URL，把前缀剥掉。**不根据前缀猜类型**：
    # 真正的格式由字节内容决定（vision.sniff_image），声明值是用户设备给的，
    # 微信存下来的图经常名不副实。
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]

    # base64 相比原字节膨胀 4/3。先按长度粗筛，别为一个 200 MB 的字符串
    # 去申请解码缓冲——那一下能把进程顶死，返回的错误还看不懂。
    if len(raw) > _vision.MAX_BYTES * 2:
        return _fail(413, "too_large",
                     f"图片太大，上限 {_vision.MAX_BYTES // 1024 // 1024} MB")

    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return _fail(400, "bad_base64", "图片数据不完整，请重新上传")
    try:
        return _vision.extract(data, _llm)
    except _vision.ImageError as e:
        return _fail(400, "bad_image", str(e))


def _case_extras_data(payload: dict, uid: int) -> dict | JSONResponse:
    """按已有案情算出五块办案包。两个 HTTP 接口共用这一段。

    归属校验沿用 /api/doc/arbitration 的写法：task_id / session_id 是前端传的，
    不校验就能读到别人案子里的入职时间和月薪。显式立场、槽位白名单、
    最近提问回退也都在这里 —— 只写一遍，免得「只要判例卡」那条路由
    和整包路由哪天各自长出不同的口径。
    """
    tid = (payload.get("task_id") or "").strip()
    sid = (payload.get("session_id") or "").strip()
    q = str(payload.get("question") or "").strip()[:500]
    source: dict = {"task_id": tid or None, "session_id": sid or None}

    slots: dict = {}
    if tid:
        if _store.task_owner(tid) != uid:
            return _fail(403, "forbidden", "这个任务不属于当前账号")
        slots = _store.load_task_slots(tid).known()
        msgs = _store.task_messages(tid, limit=50)
    elif sid:
        if _store.session_owner(sid) != uid:
            return _fail(403, "forbidden", "这个会话不属于当前账号")
        slots = _store.load_slots(sid).known()
        msgs = _store.session_messages(sid, limit=50)
    else:
        msgs = []

    if not q:
        # 最近一条真实提问比「请在输入框里重新问一遍」有用得多。
        q = next((str(m.get("content") or "") for m in reversed(msgs)
                  if (m.get("role") or "") == "user" and
                  not _is_greeting(str(m.get("content") or ""))), "")

    # 允许调用方补槽位，但只认 CaseSlots 那 8 个字段：其余一律丢，
    # 否则前端随手带的 profile / 姓名会顺着办案包流到界面上。
    patch = payload.get("slots")
    if isinstance(patch, dict):
        slots.update({k: v for k, v in patch.items()
                      if k in CaseSlots.model_fields and v is not None})

    def _obj(key: str) -> dict | None:
        v = payload.get(key)
        return v if isinstance(v, dict) else None

    # 立场只认两个来源：本次请求显式传的、或此前用户点过并落库的。
    # 绝不从提问语气里推 —— 那正是 persona 模块刻意不提供的能力。
    stance = _persona.normalize_stance(payload.get("stance"))
    pcfg = payload.get("persona") if isinstance(payload.get("persona"), dict) else None
    saved = _store.get_user_facts(uid)
    if stance is None:
        stance = _persona.normalize_stance(saved.get("stance"))
    if pcfg is None:
        pcfg = {"tone_mode": saved.get("tone_mode"),
                "confirmed": bool(saved.get("confirmed"))}
    # external=1 才真去联网。默认 False —— 「自动检测到网络」和
    # 「系统可以自作主张往外发请求」是两回事，后者必须用户显式点头。
    with_external = str(payload.get("external") or "").strip().lower() in (
        "1", "true", "yes", "on")
    # 内核优先读会话绑定的——历史会话回放不会重发 SSE extras，办案包靠这个
    # 接口重算，若按「当前请求上下文」取就会退回劳动法，图与答案对不上。
    pid = (_store.session_profile(sid) if sid else
           _profiles.resolve(payload.get("profile")).id)
    with _profiles.use(pid):
        out = _extras.from_case(
            q, slots=slots, verdict=_obj("verdict"),
            deadline_data=_obj("deadline_data"), calc_data=_obj("calc_data"),
            stance=stance, persona=pcfg, with_external=with_external)
    out["source"] = source
    out["question"] = q
    out["profile"] = pid
    return out


@app.post("/api/case/extras")
def case_extras(payload: dict, user: dict = Depends(current_user)):
    """按已有案情重算「办案包」，不必再问一句、也不调模型。

    聊天流里每轮 answered 都会随 SSE 发一份 extras；这个接口是给
    「翻回旧会话」「改完案情要素后重算」「外部按同一份规则核查」三件事用的。
    三条路都走 extras.from_case，和答案流、MCP 服务是同一个纯函数，
    免得三处各长出一套不一致的结果。
    """
    return _case_extras_data(payload, int(user["id"]))


@app.post("/api/case/decision")
def case_decision(payload: dict, user: dict = Depends(current_user)):
    """只取「判例参考与申诉决策卡」，不拉整包。

    参数与 /api/case/extras 完全相同；返回的就是 from_case()["decision"]，
    与 MCP 的 case_decision、SSE extras 里的 decision 逐字段一致——
    三处对账不必各写一套断言。需要更多/更少相似案件时用 MCP 的
    case_decision(limit=1..8)，这里固定返回默认条数。

    独立开一条路由只为「只要判例卡」的调用方省掉另外四块的传输；
    归属校验、槽位白名单、显式立场全在 _case_extras_data，不在这里重写。
    """
    out = _case_extras_data(payload, int(user["id"]))
    if isinstance(out, JSONResponse):
        return out                       # 403 / 参数错误原样透传
    return out["decision"]


# ---------------------------------------------------------- v124 案件级附件
@app.get("/api/case/attachments")
def get_attachments(task_id: str = "",
                    user: dict = Depends(current_user)):
    """读某任务的附件开关，并**只按打开的开关**生成内容。

    两个开关默认全关，此时只回 flags，不生成任何时间表或文书 ——
    用户没要就不算，也不把整份骨架塞进每次轮询里。内容和案情、语料
    一起实时重算，库里只存「有没有打开」，不存会过期的正文。
    """
    uid = int(user["id"])
    tid = (task_id or "").strip()
    if not tid:
        return _fail(400, "no_task", "缺少 task_id")
    if _store.task_owner(tid) != uid:
        return _fail(403, "forbidden", "这个任务不属于当前账号")
    flags = _store.get_attachment_flags(tid)
    out: dict = {"task_id": tid, "flags": flags}
    if not (flags["procedure"] or flags["document"]):
        return out

    t = _store.get_task(tid, uid) or {}
    pid = t.get("profile_id") or "labor"
    slots = _store.load_task_slots(tid).known()
    # 寒暄先滤掉：它们被当成事实陈述摘进骨架，比少一句更糟。
    msgs = [m for m in _store.task_messages(tid, limit=600)
            if not _is_greeting(m.get("content") or "")]
    kit = _casekit.build(pid, slots=slots,
                         profile=_auth.get_profile(uid), messages=msgs)
    out["profile"] = pid
    kit["document"]["filename"] = _docgen.filename_for(pid)
    if flags["procedure"]:
        out["procedure"] = kit["procedure"]
    if flags["document"]:
        out["document"] = kit["document"]
    return out


@app.post("/api/case/attachments")
def set_attachments(payload: dict, user: dict = Depends(current_user)):
    """只改请求里带的那一个开关，另一个保持原值。校验归属，越权 403。"""
    uid = int(user["id"])
    tid = (payload.get("task_id") or "").strip()
    if not tid:
        return _fail(400, "no_task", "缺少 task_id")
    patch = {}
    for k in ("procedure", "document"):
        if k in payload:
            patch[k] = bool(payload.get(k))
    flags = _store.set_attachment_flags(tid, uid, patch)
    if flags is None:
        return _fail(403, "forbidden", "这个任务不属于当前账号")
    return {"task_id": tid, "flags": flags}


@app.get("/api/persona")
def get_persona(user: dict = Depends(current_user)):
    """读用户**显式选过**的立场与表达偏好。

    这里没有「按历史提问推断」这一步：返回的就是用户点过什么，没点过
    就是空。前端据此显示「未选择」，而不是显示一个系统猜出来的身份。
    """
    return {
        "facts": _store.get_user_facts(int(user["id"])),
        "options": [dict(x) for x in _persona.STANCES],
        "note": "立场由你选择，系统不根据语气判断身份；未选择时按职工视角给参考。",
    }


@app.put("/api/persona")
def put_persona(payload: dict, user: dict = Depends(current_user)):
    """保存显式选择。非法值直接 400，不「就近猜一个」。"""
    raw_stance = payload.get("stance")
    if raw_stance is not None and _persona.normalize_stance(raw_stance) is None:
        return _fail(400, "bad_stance", "立场取值不合法")
    raw_tone = payload.get("tone_mode")
    if raw_tone is not None and raw_tone not in ("plain", "standard"):
        return _fail(400, "bad_tone_mode", "表达模式只能是 plain / standard")
    facts = _store.save_user_facts(
        int(user["id"]), stance=raw_stance, tone_mode=raw_tone,
        confirmed=bool(payload.get("confirmed", True)))
    return {"facts": facts, "options": [dict(x) for x in _persona.STANCES]}


# ---------------------------------------------- v139 法典图书馆 / 自主学习
# 三块功能共用一个边界：pid 由 _profiles.resolve() 归一（非法值回落劳动法），
# 用户身份一律从 current_user 取，绝不从请求体读 user_id。写接口的 CSRF
# 由 current_user 统一校验，这里不重复写。
def _pid_of(raw: Any) -> str:
    return _profiles.resolve(raw).id


@app.get("/api/library/books")
def library_books(pid: str = "labor", user: dict = Depends(current_user)):
    """书目 + 目录树（不含正文）。按内核分库，看不到别的法域的书。"""
    return _library.books(_pid_of(pid))


@app.get("/api/library/law")
def library_law(pid: str = "labor", law: str = "",
                user: dict = Depends(current_user)):
    """某部法全文。民法典 1260 条，按需拉，不塞进目录接口。"""
    return _library.law_text(_pid_of(pid), law)


@app.get("/api/library/article")
def library_article(pid: str = "labor", id: str = "",
                    user: dict = Depends(current_user)):
    """单条原文。快速查找跳转、以及判答后「看原文」都走这里。
    只认本内核索引里的 id，跨内核传 id 一律 404。"""
    a = _library.article(_pid_of(pid), id)
    if not a:
        return _fail(404, "no_article", "本内核未收录该条文")
    return a


@app.get("/api/library/search")
def library_search(pid: str = "labor", q: str = "", limit: int = 8,
                   user: dict = Depends(current_user)):
    """快速查找：条号直达 / 正文关键词，只在本内核语料里搜。"""
    return _library.search(_pid_of(pid), q, limit)


@app.post("/api/study/quiz")
def study_quiz(payload: dict, user: dict = Depends(current_user)):
    """出题。在线时模型写题面，回放包自动回落本地原文四选一，都能答。"""
    pid = _pid_of(payload.get("pid"))
    return _study.new_quiz(
        _studydb, _llm, int(user["id"]), pid,
        law_short=str(payload.get("law_short") or "")[:80],
        chapter=str(payload.get("chapter") or "")[:160],
        n=payload.get("n") or 5)


@app.post("/api/study/grade")
def study_grade(payload: dict, user: dict = Depends(current_user)):
    """判答并写进度 / 错题集。题目必须属于本人 + 本内核。"""
    pid = _pid_of(payload.get("pid"))
    out = _study.grade_answer(
        _studydb, _llm, int(user["id"]), pid,
        str(payload.get("quiz_id") or "").strip(),
        str(payload.get("answer") or "")[:2000])
    if out is None:
        return _fail(404, "no_quiz", "题目不存在或不属于当前法典")
    return out


@app.get("/api/study/dashboard")
def study_dashboard(user: dict = Depends(current_user)):
    """个人主页：五法典进度 / 知识树 / 错题 / 打卡 / 薄弱项 / 徽章。
    进度按 (user_id, pid) 分开算，一次给全，前端不需要五次请求。"""
    return _study.dashboard(_studydb, int(user["id"]))


@app.get("/api/study/wrongbook")
def study_wrongbook(pid: str = "", mastered: int = 0,
                    user: dict = Depends(current_user)):
    """错题集。pid 为空时给全部法典，按 pid 分栏由前端做。"""
    rows = _studydb.wrongbook(int(user["id"]),
                              _pid_of(pid) if pid else None, mastered)
    return {"items": rows, "count": len(rows)}


@app.post("/api/study/master")
def study_master(payload: dict, user: dict = Depends(current_user)):
    """标记 / 取消标记「已掌握」。只改本人本内核的那一条。"""
    pid = _pid_of(payload.get("pid"))
    ok = _studydb.mark_mastered(
        int(user["id"]), pid, str(payload.get("quiz_id") or "").strip(),
        bool(payload.get("mastered", True)))
    if not ok:
        return _fail(404, "no_wrong_item", "错题集里没有这一条")
    return {"ok": True}


@app.get("/api/study/daily")
def study_daily(pid: str = "labor", user: dict = Depends(current_user)):
    """每日一题：同一天同一用户同一法典不换题。"""
    return _study.daily_question(_studydb, _llm, int(user["id"]), _pid_of(pid))


@app.get("/api/study/report")
def study_report(paper_id: str = "", pid: str = "labor",
                 user: dict = Depends(current_user)):
    """导出学习报告。`paper_id` 给了就导这张卷子的评估报告。

    返回文本由前端生成 .txt，不引 PDF 依赖——项目一贯如此，
    依赖清单里也就能一直只有 pydantic + fastapi/uvicorn + httpx。
    """
    uid = int(user["id"])
    if paper_id:
        txt = _study.report_text(_studydb, uid, paper_id, _pid_of(pid))
        return {"text": txt, "filename": f"卷面评估报告-{paper_id}.txt"}
    return {"text": _study.report_text(_studydb, uid),
            "filename": "法律知识学习报告.txt"}


# ---------------------------------------------------------------- 卷子（v140）


@app.post("/api/study/paper")
def study_paper_new(payload: dict, user: dict = Depends(current_user)):
    """生成一张卷子。题数 1–30，题型配比可选（choice/judge/blank）。

    **只出题不判题**：判答仍走 /api/study/grade，进度、错题、遗忘曲线
    只有一条写入路径。交卷只做汇总，不产生第二套统计口径。
    """
    pid = _pid_of(payload.get("pid"))
    kind = str(payload.get("kind") or "quiz")[:20]
    only = payload.get("quiz_ids")
    return _study.create_paper(
        _studydb, _llm, int(user["id"]), pid,
        law_short=str(payload.get("law_short") or "")[:80],
        chapter=str(payload.get("chapter") or "")[:160],
        n=payload.get("n") or 10,
        qtypes=payload.get("qtypes"),
        kind=kind,
        only_quiz_ids=[str(x) for x in only] if isinstance(only, list) else None)


@app.post("/api/study/paper/submit")
def study_paper_submit(payload: dict, user: dict = Depends(current_user)):
    pid = _pid_of(payload.get("pid"))
    rep = _study.submit_paper(
        _studydb, int(user["id"]), pid,
        str(payload.get("paper_id") or "").strip(),
        int(payload.get("elapsed") or 0))
    if rep is None:
        return _fail(404, "no_paper", "这张卷子不存在或不属于当前账号")
    return rep


@app.get("/api/study/paper/{paper_id}")
def study_paper_detail(paper_id: str, pid: str = "labor",
                       user: dict = Depends(current_user)):
    """报告页：汇总 + 逐题回顾。未交卷时 submitted=false，不编造分数。"""
    out = _study.paper_detail(_studydb, int(user["id"]),
                              _pid_of(pid), paper_id)
    if out is None:
        return _fail(404, "no_paper", "这张卷子不存在或不属于当前账号")
    return out


@app.get("/api/study/calendar")
def study_calendar(month: str = "", user: dict = Depends(current_user)):
    """打卡日历。month 形如 2026-11；不给就是本月。"""
    return _study.calendar_of(_studydb, int(user["id"]), month[:7])


@app.post("/api/chat")
async def chat(payload: dict, request: Request,
               user: dict = Depends(current_user)):
    question = (payload.get("question") or "").strip()
    if not question:
        return _fail(400, "empty_question", "question 不能为空")

    uid = int(user["id"])
    incoming = (payload.get("session_id") or "").strip()
    # 任务归属也要校验：task_id 同样是前端传的，不校验的话把别人的
    # 会话挂到自己任务下（或反过来读别人的任务要素）都只需要改个 id。
    task_id = (payload.get("task_id") or "").strip() or None
    if task_id and _store.task_owner(task_id) != uid:
        return _fail(403, "forbidden", "这个任务不属于当前账号")

    if incoming:
        # 已有会话必须属于当前用户。不通过就 403，**不静默新建**——
        # 静默新建会让用户以为自己接着上次聊，实际记忆全丢。
        if _store.session_owner(incoming) != uid:
            return _fail(403, "forbidden", "这个会话不属于当前账号")
        session_id = incoming
        # 会话和 task_id 必须是同一件事。前端总是送当前的 task_id，所以这条
        # 在正常操作里不会触发；但两者不一致时后果不小：要素会从**另一个任务**
        # 继承下来，再随 save_slots 写回本会话所属的任务——等于把甲案的
        # 事实漏进乙案。宁可直接拒，也不要悄悄串。
        owned_task = _store.session_task(session_id)
        if task_id and owned_task and owned_task != task_id:
            return _fail(400, "task_mismatch",
                         "这个会话属于另一个任务，不能按当前任务继续")
    else:
        session_id = uuid.uuid4().hex[:12]

    # v119 内核绑定：已有会话以库里绑定的为准（本次传什么都不改，防串库）；
    # 新会话用本次显式选的，非法值回落劳动法。内核只在这里定一次，
    # 之后 worker 线程、SSE extras、办案包接口读的是同一个结论。
    if incoming:
        pid = _store.session_profile(session_id)
        # 会话自己记得属于哪个任务，请求没带就按库里的补上。不补的话
        # 「打开一条历史会话接着问」拿不到任务级要素（要素按任务合并），
        # 同一个案子从任务里问和从历史记录里问会得到两套记忆。
        # 放在 task_mismatch 校验之后：校验管的是「两个都给了且不一致」。
        if not task_id:
            task_id = _store.session_task(session_id) or None
    else:
        pid = _profiles.resolve(payload.get("profile")).id
        # 显式选了内核，却给了一个别的内核的任务——这是前端状态错乱的典型
        # 症状（在任务里切了法，却没退任务）。不报错，也不改那个任务的内核
        # （任务的法域是建它的时候定的，一次误传不该改写历史），而是把这次
        # 当成一次新咨询：新开一个任务，原任务一个字节都不动。
        # 只在请求里**真的带了** profile 时才这么判：不带 profile 的老调用方
        # 会被 resolve 兜底成劳动法，拿这个兜底去比对会把消费内核的任务误拆。
        if task_id and payload.get("profile"):
            if (_store.get_task(task_id, uid) or {}).get("profile_id") != pid:
                task_id = None
        # v123：无任务、无会话 = 一次新咨询。给它建一个任务，而不是留一条
        # 无主会话。留无主会话的后果是 /api/tasks 会把**所有**无主会话扫进
        # 同一个「历史咨询」桶里：先问食安、再问劳动法，两次咨询变成同一个
        # 任务，任务的徽章还跟着最后一个会话变色——看着就像前一个任务被
        # 覆盖了。任务标题取第一句提问，用户不用再手工起名。
        if not task_id:
            t = _store.create_task(
                uid, _auto_task_title(question, _profiles.resolve(pid).name),
                profile_id=pid)
            task_id = t.get("task_id") or None
        _store.ensure_session(session_id, uid, task_id, profile_id=pid)
        _store.set_session_profile(session_id, pid)

    q: queue.Queue = queue.Queue()
    meta_box = {"cites": 0, "unverified": 0}

    def emit(event: str, data: dict) -> None:
        # 顺手把自检结果留下来。turns 表要记「这轮有没有未核验引用」，
        # 而 verify 事件只在流里出现一次，出了 run() 就取不到了。
        if event == "verify":
            meta_box["cites"] = len(data.get("citations") or [])
            meta_box["unverified"] = len(data.get("issues") or [])
        q.put((event, data))

    def work() -> None:
        t0 = time.time()
        try:
            # 只把地区递进去。档案里还有姓名、身份证号，那些是写申请书用的，
            # 不该进 Agent 的上下文——传全量 dict 只是图省事，代价是把
            # 「答案里会不会冒出真名」变成一个要靠下游自律的问题。
            prof = _auth.get_profile(uid) or {}
            p = _profiles.resolve(pid)
            # 劳动法内核沿用全局单例（31 套件的基线）；接入内核才换独立索引。
            # contextvar 不会从请求线程继承到 worker 线程，所以 use() 必须
            # 写在 work() 里面，包住整段 run —— 包括 run 内 emit('extras')。
            runner = (_runner if p.id == "labor"
                      else AgentRunner(get_profile_index(p), _llm, _store))
            with _profiles.use(p):
                state = runner.run(question, session_id=session_id, emit=emit,
                                    user_id=uid, task_id=task_id,
                                    profile={"region": prof.get("region") or ""})
            tid = _store.save_turn(session_id, uid, question, state.status,
                                   len(state.scratchpad),
                                   int((time.time() - t0) * 1000),
                                   meta_box["cites"], meta_box["unverified"])
            # 把 turn_id 交回前端，评价时带上来，便于在库里和这轮对上。
            q.put(("turn", {"turn_id": tid}))
        except Exception as e:                      # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            try:
                _store.save_turn(session_id, uid, question, "failed", 0,
                                 int((time.time() - t0) * 1000), 0, 0)
            except Exception:                       # noqa: BLE001
                pass
        finally:
            q.put((None, None))

    threading.Thread(target=work, daemon=True).start()

    # 会话必然属于某个任务（没有就刚建了一个），把任务一起回给前端：
    # 前端收到才能立刻把顶部任务条画出来，用户才看得见「这次咨询成了一个
    # 任务」。只回 session_id 的话，前端 currentTask 一直是 null，下一句
    # 追问会再建一个任务——同一件事被拆成两个案子，记忆各半。
    task_payload = _store.get_task(task_id, uid) if task_id else None

    async def gen():
        yield _sse("session", {"session_id": session_id,
                               "task_id": task_id,
                               "task": task_payload})
        loop = asyncio.get_running_loop()
        while True:
            event, data = await loop.run_in_executor(None, q.get)
            if event is None:
                break
            yield _sse(event, data)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ------------------------------------------------------------------ 管理端

def degrade_alert(live: str, note: str) -> dict | None:
    """模型调用失败时给管理员的一条告警，正常返回 None。

    为什么放进 alerts 而不是只写在概览那行小字里：
      另外三条规则是「有人做了可疑的事」，这条是「现在所有人拿到的回答都是
      回放的」——后者更急，而且管理员打开概览第一眼看的是告警区。
      写在灰字段落里，等于把最该处理的事埋在一堆统计口径说明中间。
    """
    if live != "error":
        return None
    # 恢复方式必须和 `LLM.maybe_recover()` 的真实行为一致：冷却 N 秒后**下一轮
    # 提问**才会重试，所以「最多等 N 分钟」是准确的，而「立刻自动恢复」不是。
    # 早先这里写过「下一次调用成功会自动恢复，不用重启」——那时 online 一旦
    # 置 False 就没有路径置回来，那句话是假的；现在有了冷却重试才敢这么说。
    mins = max(1, LLM_RECOVER_AFTER // 60)
    return {"level": "bad",
            "title": "模型调用失败，回答正在走回放",
            "detail": f"最近一次调用失败：{note}。回放答案是模板加检索条文，"
                      f"没有模型生成的结论与建议。降级后进程暂停调用接口，"
                      f"每 {mins} 分钟随下一次提问重试一次：充值或换 key 后最多等 "
                      f"{mins} 分钟就会恢复，急着用可以重启服务。"}


@app.get("/api/admin/overview")
def admin_overview(admin: dict = Depends(require_admin)):
    data = _admin.overview()
    # 运维信息只给管理员：LLM 没初始化成功的原因在这里才有意义。
    #
    # 配置态和运行态必须分开给：`mode` 说的是「启动时有没有 key」，起来就不变了；
    # 余额扣光、key 被停都发生在启动之后。原来这里只给 `mode`，管理员打开概览
    # 看到「LLM 在线」，而每一轮回答其实都是回放——最该知道欠费的人反而看不到。
    live, note = llm_live(_health.snapshot())
    data["llm"] = {"enabled": LLM_ENABLED, "mode": MODE_NOTE,
                   "fallback_reason": _llm.fallback_reason,
                   "live": live, "live_note": note}
    alert = degrade_alert(live, note)
    if alert:
        data["alerts"] = [alert] + list(data.get("alerts") or [])
    return data


@app.get("/api/admin/users")
def admin_users(q: str = "", role: str = "", status: str = "",
                page: int = 1, size: int = 20,
                admin: dict = Depends(require_admin)):
    return _auth.list_users(q=q, role=role, status=status, page=page, size=size)


@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, admin: dict = Depends(require_admin)):
    u = _admin.user_detail(user_id)
    if not u:
        return _fail(404, "no_user", "用户不存在")
    return {"user": u}


@app.post("/api/admin/users")
def admin_create_user(payload: dict, request: Request,
                      admin: dict = Depends(require_admin)):
    try:
        u = _auth.create_user((payload.get("username") or "").strip(),
                              payload.get("password") or "",
                              role=payload.get("role") or "user",
                              ip=_client_ip(request))
    except AuthError as e:
        return _err(e)
    _auth.log_admin(admin, "create_user", "user", u["id"],
                    f"新建 {u['username']} role={u['role']}", _client_ip(request))
    return {"user": u}


@app.post("/api/admin/users/{user_id}/status")
def admin_set_status(user_id: int, payload: dict, request: Request,
                     admin: dict = Depends(require_admin)):
    try:
        _auth.set_status(user_id, payload.get("status") or "")
    except AuthError as e:
        return _err(e)
    _auth.log_admin(admin, "set_status", "user", user_id,
                    f"状态改为 {payload.get('status')}", _client_ip(request))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/role")
def admin_set_role(user_id: int, payload: dict, request: Request,
                   admin: dict = Depends(require_admin)):
    try:
        _auth.set_role(user_id, payload.get("role") or "")
    except AuthError as e:
        return _err(e)
    _auth.log_admin(admin, "set_role", "user", user_id,
                    f"角色改为 {payload.get('role')}", _client_ip(request))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(user_id: int, payload: dict, request: Request,
                         admin: dict = Depends(require_admin)):
    try:
        _auth.admin_reset_password(user_id, payload.get("password") or "")
    except AuthError as e:
        return _err(e)
    # 审计只记「重置了密码」，**不记密码本身**——审计日志是给多人看的，
    # 记进去等于把密码永久留档。
    _auth.log_admin(admin, "reset_password", "user", user_id,
                    "管理员重置密码", _client_ip(request))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unlock")
def admin_unlock(user_id: int, request: Request,
                 admin: dict = Depends(require_admin)):
    _auth.unlock(user_id)
    _auth.log_admin(admin, "unlock", "user", user_id, "解除登录锁定",
                    _client_ip(request))
    return {"ok": True}


@app.get("/api/admin/sessions")
def admin_sessions(q: str = "", user_id: int | None = None,
                   page: int = 1, size: int = 20, owner: str = "any",
                   admin: dict = Depends(require_admin)):
    return _admin.sessions(q=q, user_id=user_id, page=page, size=size,
                           owner=owner)


@app.get("/api/admin/session/{session_id}")
def admin_session_detail(session_id: str, request: Request,
                         admin: dict = Depends(require_admin)):
    data = _admin.session_detail(session_id)
    if data is None:
        return _fail(404, "no_session", "会话不存在")
    # 「管理员可看正文，但留痕」——留痕只有写在**打开正文的这一刻**才有意义。
    # 写在列表页就成了噪音：管理员翻一次列表会产生几十条审计。
    _auth.log_admin(admin, "view_session", "session", session_id,
                    f"查看会话正文（用户：{data['username']}）",
                    _client_ip(request))
    return data


@app.get("/api/admin/turns")
def admin_turns(status: str = "", limit: int = 30,
                admin: dict = Depends(require_admin)):
    return {"items": _admin.recent_turns(status=status, limit=limit)}


@app.get("/api/admin/logins")
def admin_logins(limit: int = 100, admin: dict = Depends(require_admin)):
    return {"items": _auth.recent_logins(limit=limit)}


@app.get("/api/admin/audits")
def admin_audits(limit: int = 100, admin: dict = Depends(require_admin)):
    return {"items": _auth.recent_audits(limit=limit)}


# ------------------------------------------------------------------ SSE

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    import uvicorn
    # 端口默认 8000；尊重 PORT 环境变量，这样和「启动-网页.bat」里的
    # `set "PORT=8000"` 是同一个来源——两处不会各说各话。
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
