# -*- coding: utf-8 -*-
"""
CSRF 双提交的端到端验证。服务由本脚本自己起（端口随机挑），不用先开服务。

为什么用真实 HTTP 而不是调函数：
  CSRF 的全部有效性都建立在「浏览器会带哪些 cookie、会不会带自定义头」
  这件事上。直接调 server.csrf_ok() 是在测那个函数，不是在测这条防线。
  这里用 urllib + CookieJar 模拟浏览器：cookie 自动带，请求头手动控制。

测什么：
  · 页面响应下发 csrf cookie，且**不是 HttpOnly**（前端要读）
  · 登录/注册豁免（没有会话，无可伪造身份）
  · 带登录态但缺头 / 头不对 → 403 csrf_failed
  · 带登录态且头正确 → 过 CSRF，进入业务校验（用空 question 探针拿 400）
  · GET 不受影响（浏览器导航不会带自定义头）

跑法：
    ./.venv/Scripts/python.exe scripts/test_csrf.py
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _svc import start_service, stop_service   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

# 由 main() 起好服务之后填上，不再写死：写死的端口等于默认「8000 上那个
# 服务是本目录的」，而这件事从来没被验证过（详见 scripts/_svc.py）。
BASE = ""
CSRF_COOKIE = "law_csrf"
# 专用自检账号。原来这里直接用 admin / 一个写死的密码，于是它只在「admin 密码
# 恰好是那个」的机器上能跑——全新库里 admin 是随机的一次性密码，登录必 401，
# 报的还是「登录失败」，看不出根因。跟 check_ui.py 一个做法：自己建、跑完删，
# 不去碰 admin（把别人的管理员密码改了，比留一个测试号严重得多）。
CHECK_USER = "csrf-check"
CHECK_PASS = "csrf-check-pw-123"
fails = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global fails
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))
    if not ok:
        fails += 1


class Client:
    """一个「浏览器」：自带 cookie、可以指定额外请求头。"""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value
        return ""

    def raw(self, method: str, path: str, body: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
        data = None
        h = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE + path, data=data, headers=h,
                                     method=method)
        try:
            with self.opener.open(req, timeout=15) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()


def main() -> int:
    # 自己起一个服务，不再连 8000 上「碰巧存在」的那个。
    # 原因与改法见 scripts/_svc.py 顶部的说明：写死的端口是一条没说出口的
    # 前提——「8000 上那个服务是本目录的」，而它从来没被验证过。
    global BASE
    _ensure_check_user()
    svc, BASE = start_service()
    print(f"  已起服务：{BASE}")
    try:
        return _run()
    finally:
        stop_service(svc)
        _drop_check_user()


def _ensure_check_user() -> None:
    """保证专用自检账号存在且密码已知。只认这个名字，别的一概不动。"""
    from app.auth import AuthStore          # noqa: PLC0415
    from app.config import DB_PATH          # noqa: PLC0415
    store = AuthStore(DB_PATH)
    # 先清掉同名残留（可能是上一轮异常退出留下的、密码不可知的账号）
    store._conn.execute("DELETE FROM auth_tokens WHERE user_id IN "
                        "(SELECT id FROM users WHERE username=?)", (CHECK_USER,))
    store._conn.execute("DELETE FROM users WHERE username=?", (CHECK_USER,))
    store._conn.commit()
    try:
        store.create_user(CHECK_USER, CHECK_PASS, role="user", ip="127.0.0.1")
    except Exception as e:                  # noqa: BLE001
        print(f"  [WARN] 建自检账号失败：{e}")


def _drop_check_user() -> None:
    from app.auth import AuthStore          # noqa: PLC0415
    from app.config import DB_PATH          # noqa: PLC0415
    try:
        store = AuthStore(DB_PATH)
        store._conn.execute("DELETE FROM auth_tokens WHERE user_id IN "
                            "(SELECT id FROM users WHERE username=?)",
                            (CHECK_USER,))
        store._conn.execute("DELETE FROM users WHERE username=?", (CHECK_USER,))
        store._conn.commit()
    except Exception as e:                  # noqa: BLE001
        print(f"  [WARN] 自检账号没清干净：{e}")


def _run() -> int:
    c = Client()          # 用的是本文件里的 Client，它读模块级 BASE
    print("CSRF 双提交：")

    # ---- 1. 页面下发 csrf cookie，且不能是 HttpOnly
    st, hdrs, _ = c.raw("GET", "/login")
    raw_cookie = hdrs.get("Set-Cookie", "") or hdrs.get("set-cookie", "")
    has = bool(c.cookie(CSRF_COOKIE))
    check("打开页面后拿到 csrf cookie", st == 200 and has,
          c.cookie(CSRF_COOKIE)[:12] + "…" if has else "无")
    check("csrf cookie 不是 HttpOnly（前端要读）",
          "httponly" not in raw_cookie.lower(),
          raw_cookie[:80].replace("\n", " "))

    # ---- 2. 登录：没有会话，应当豁免 CSRF
    st, _, body = c.raw("POST", "/api/auth/login",
                        {"username": CHECK_USER, "password": CHECK_PASS})
    logged = (st == 200)
    check("登录请求豁免 CSRF（无会话）", logged, f"HTTP {st}")
    if not logged:
        print("  登录失败，后面的用例都无法进行。")
        return 1
    check("登录后拿到会话令牌", bool(c.cookie("law_token")))

    csrf = c.cookie(CSRF_COOKIE)

    # ---- 3. 带登录态、不带 CSRF 头 → 必须挡住
    st, _, body = c.raw("POST", "/api/chat", {"question": ""})
    try:
        code = json.loads(body.decode("utf-8")).get("code")
    except Exception:                 # noqa: BLE001
        code = ""
    check("缺 CSRF 头 → 403", st == 403 and code == "csrf_failed",
          f"HTTP {st} {code}")

    # ---- 4. 头不对 → 必须挡住
    st, _, body = c.raw("POST", "/api/chat", {"question": ""},
                        {"X-CSRF-Token": "not-the-same-value"})
    check("CSRF 头不匹配 → 403", st == 403, f"HTTP {st}")

    # ---- 5. 头正确 → 过 CSRF，进入业务校验（空问题应得 400，不是 403）
    st, _, body = c.raw("POST", "/api/chat", {"question": ""},
                        {"X-CSRF-Token": csrf})
    try:
        code = json.loads(body.decode("utf-8")).get("code")
    except Exception:                 # noqa: BLE001
        code = ""
    check("CSRF 头正确 → 进到业务校验（空问题 400）",
          st == 400 and code == "empty_question", f"HTTP {st} {code}")

    # ---- 6. GET 不校验（浏览器导航带不了自定义头）
    st, _, _ = c.raw("GET", "/api/auth/me")
    check("GET 请求不受 CSRF 影响", st == 200, f"HTTP {st}")

    # ---- 7. 改密码接口同样受保护（它在 current_user 依赖链上）
    st, _, _ = c.raw("POST", "/api/auth/password",
                     {"old": "x", "new": "y"})
    check("改密码接口缺头 → 403", st == 403, f"HTTP {st}")

    # ---- 8. 登出：手动校验的接口，也要挡住
    st, _, _ = c.raw("POST", "/api/auth/logout")
    check("登出接口缺头 → 403", st == 403, f"HTTP {st}")

    # ---- 9. 带着头登出应当成功，并且之后令牌失效
    st, _, _ = c.raw("POST", "/api/auth/logout", headers={"X-CSRF-Token": csrf})
    check("带正确头可以登出", st == 200, f"HTTP {st}")
    st, _, _ = c.raw("GET", "/api/auth/me")
    check("登出后令牌失效", st == 401, f"HTTP {st}")

    print()
    if fails:
        print(f"{fails} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
