# -*- coding: utf-8 -*-
"""
账号体系的接口级测试。用独立临时库，不碰 data/law.db。

为什么用 HTTP 层测而不是只测 AuthStore：
  绝大多数真实漏洞出在「路由忘了挂依赖」「cookie 没带上」「越权少判一个
  条件」这类接线问题上——AuthStore 单独测得再全，也发现不了
  /api/chat 忘了校验 session 归属。

用例按「攻击者会怎么试」组织，不按「函数怎么分」组织：
  未登录能不能直接问、能不能打管理接口、能不能拿别人的 session_id、
  封号之后手上那个 cookie 还能不能用、非管理员能不能靠改请求绕过。

跑法（零 API 花费，回放模式下不需要 Key）：
    ./.venv/Scripts/python.exe scripts/test_auth.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设：config 在导入时就确定了库路径。
_TMP = Path(tempfile.mkdtemp(prefix="lawtest_"))
os.environ["LAW_DB_PATH"] = str(_TMP / "test.db")
os.environ["ALLOW_REGISTER"] = "1"
os.environ["REG_PER_IP_HOUR"] = "100"   # 本机 IP 全是 testclient，别被限流拦住

from fastapi.testclient import TestClient          # noqa: E402
from app import server as srv                       # noqa: E402
from app.config import CSRF_NAME                    # noqa: E402

PASS, FAIL = [], []


class CsrfClient(TestClient):
    """带 CSRF 头的 TestClient。

    浏览器里这件事由 static/assets/ui.js 的 fetch 包装层做：非 GET 请求
    自动把 law_csrf cookie 抄进 X-CSRF-Token。测试侧必须做等价处理，
    否则每个写请求都会先被 403 挡住——测出来的就不是「认证与越权」，
    而只是「CSRF 生效了」这一件事。

    令牌从 cookie jar 现读，而不是登录时缓存一份：登出/改密后 cookie 会
    变，缓存的那份会过期，测试会报出真实的它自己的 bug。
    """

    def request(self, method, url, **kw):        # type: ignore[override]
        m = str(method).upper()
        if m not in ("GET", "HEAD", "OPTIONS"):
            token = self.cookies.get(CSRF_NAME)
            if token:
                headers = dict(kw.get("headers") or {})
                headers.setdefault("X-CSRF-Token", token)
                kw["headers"] = headers
        return super().request(method, url, **kw)


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"    {detail}" if detail and not cond else ""))


def sid_of(text: str) -> str | None:
    m = re.search(r"event: session\ndata: (\{.*?\})\n\n", text)
    return json.loads(m.group(1))["session_id"] if m else None


def main() -> int:
    admin_pwd = "adminpass123"
    # 导入 server 时已经建过首个管理员了（_boot_admin）。这里只是把密码
    # 重置成一个测试知道的固定值——不能假设它叫 root，否则测试会在
    # 「库里有 admin 但没 root」的情况下直接 KeyError。
    admin = srv._boot_admin or srv._auth.get_user_by_name(srv.ADMIN_USERNAME)
    if not admin:
        admin, _ = srv._auth.ensure_admin("root", admin_pwd)
    srv._auth.admin_reset_password(admin["id"], admin_pwd)

    anon = CsrfClient(srv.app)
    alice = CsrfClient(srv.app)
    bob = CsrfClient(srv.app)
    boss = CsrfClient(srv.app)

    print("\n== 未登录 ==")
    r = anon.post("/api/chat", json={"question": "公司不发工资"})
    ok("未登录提问被拒 401", r.status_code == 401, f"实际 {r.status_code}")
    r = anon.get("/api/admin/overview")
    ok("未登录打管理接口被拒 401", r.status_code == 401, f"实际 {r.status_code}")
    r = anon.get("/api/chat/history")
    ok("未登录看历史被拒 401", r.status_code == 401, f"实际 {r.status_code}")
    r = anon.get("/", follow_redirects=False)
    ok("未登录访问首页跳登录页", r.status_code == 302 and "/login" in
       r.headers.get("location", ""), f"实际 {r.status_code}")
    r = anon.get("/admin", follow_redirects=False)
    ok("未登录访问后台跳登录页", r.status_code == 302, f"实际 {r.status_code}")

    print("\n== 注册 ==")
    r = alice.post("/api/auth/register",
                   json={"username": "张三", "password": "alice12345"})
    ok("中文用户名可注册", r.status_code == 200, r.text[:120])
    ok("注册即登录（cookie 生效）",
       alice.get("/api/auth/me").status_code == 200)
    r = anon.post("/api/auth/register",
                  json={"username": "张三", "password": "alice12345"})
    ok("重名被拒 400", r.status_code == 400 and r.json().get("code") == "user_exists",
       r.text[:120])
    r = anon.post("/api/auth/register",
                  json={"username": "bobtest", "password": "12345678"})
    ok("纯数字密码被拒", r.status_code == 400 and
       r.json().get("code") == "weak_password", r.text[:120])
    r = anon.post("/api/auth/register",
                  json={"username": "bobtest", "password": "bob12345678"})
    ok("第二个账号注册成功", r.status_code == 200, r.text[:120])

    print("\n== 越权 ==")
    r = alice.post("/api/chat", json={"question": "公司没签合同，我能要二倍工资吗"})
    sid_a = sid_of(r.text)
    ok("登录后可提问并拿到 session_id", r.status_code == 200 and bool(sid_a),
       r.text[:120])

    # bob 换个 client，手上是 bob 自己的 cookie
    bob.post("/api/auth/login",
             json={"username": "bobtest", "password": "bob12345678"})
    r = bob.get(f"/api/chat/session/{sid_a}")
    ok("B 读 A 的会话正文被拒 403", r.status_code == 403, f"实际 {r.status_code}")
    r = bob.post("/api/chat", json={"question": "随便问问", "session_id": sid_a})
    ok("B 往 A 的会话里写被拒 403", r.status_code == 403, f"实际 {r.status_code}")
    r = bob.post("/api/chat", json={"question": "随便问问", "session_id": "nosuchid"})
    ok("不存在的 session_id 也拒（不静默新建）", r.status_code == 403,
       f"实际 {r.status_code}")
    r = bob.get("/api/admin/overview")
    ok("普通用户打管理接口被拒 403", r.status_code == 403, f"实际 {r.status_code}")

    print("\n== 管理员 ==")
    r = boss.post("/api/auth/login",
                  json={"username": admin["username"], "password": admin_pwd})
    ok("管理员登录", r.status_code == 200, r.text[:120])
    r = boss.get("/api/admin/overview")
    ok("管理员读概览", r.status_code == 200, r.text[:120])
    # 概览里的「模型状态」必须同时给出配置态与运行态。只写 mode 的话，余额
    # 扣光之后管理员看到的还是「LLM 在线」，最该去充值的人反而不知道。
    ov = r.json().get("llm") or {}
    ok("概览分开给配置态与运行态", {"mode", "live"} <= set(ov), sorted(ov))
    ok("运行态取值在约定的四个里",
       ov.get("live") in ("ok", "idle", "error", "disabled"), ov.get("live"))
    ok("运行态为失败时必须带上原因类别",
       ov.get("live") != "error" or bool(ov.get("live_note")),
       f"{ov.get('live')} / {ov.get('live_note')!r}")
    r = boss.get("/api/admin/users")
    ok("管理员列用户", r.status_code == 200 and r.json()["total"] >= 3,
       r.text[:120])
    r = boss.get(f"/api/admin/session/{sid_a}")
    ok("管理员可看会话正文", r.status_code == 200, r.text[:120])
    r = boss.get("/api/admin/audits")
    acts = [x["action"] for x in r.json()["items"]]
    ok("看正文写了审计", "view_session" in acts, str(acts[:5]))

    print("\n== 封号 ==")
    bob_id = srv._auth.get_user_by_name("bobtest")["id"]
    r = boss.post(f"/api/admin/users/{bob_id}/status", json={"status": "banned"})
    ok("管理员封号", r.status_code == 200, r.text[:120])
    r = bob.get("/api/auth/me")
    ok("封号后旧 cookie 立刻失效 401", r.status_code == 401, f"实际 {r.status_code}")
    r = bob.post("/api/auth/login",
                 json={"username": "bobtest", "password": "bob12345678"})
    ok("封号后不能重新登录 403", r.status_code == 403, f"实际 {r.status_code}")
    r = boss.post(f"/api/admin/users/{bob_id}/status", json={"status": "active"})
    ok("恢复账号", r.status_code == 200, r.text[:120])

    print("\n== 末位管理员保护 ==")
    r = boss.post(f"/api/admin/users/{admin['id']}/status", json={"status": "banned"})
    ok("不能封自己（最后一个管理员）409", r.status_code == 409, f"实际 {r.status_code}")
    r = boss.post(f"/api/admin/users/{admin['id']}/role", json={"role": "user"})
    ok("不能降级最后一个管理员 409", r.status_code == 409, f"实际 {r.status_code}")

    print("\n== 改密与会话撤销 ==")
    r = alice.post("/api/auth/password",
                   json={"old": "alice12345", "new": "alice67890"})
    ok("改密成功并要求重登", r.status_code == 200 and r.json().get("relogin"),
       r.text[:120])
    r = alice.get("/api/auth/me")
    ok("改密后旧 cookie 失效 401", r.status_code == 401, f"实际 {r.status_code}")
    r = alice.post("/api/auth/login",
                   json={"username": "张三", "password": "alice67890"})
    ok("新密码可登录", r.status_code == 200, r.text[:120])
    r = alice.post("/api/auth/login",
                   json={"username": "张三", "password": "alice12345"})
    ok("旧密码不能登录 401", r.status_code == 401, f"实际 {r.status_code}")

    print("\n== 登出 ==")
    r = alice.post("/api/auth/logout")
    ok("登出", r.status_code == 200)
    ok("登出后 /me 401", alice.get("/api/auth/me").status_code == 401)

    print("\n== 管理员重置密码 ==")
    r = boss.post(f"/api/admin/users/{bob_id}/password",
                  json={"password": "reset12345"})
    ok("管理员重置密码", r.status_code == 200, r.text[:120])
    r = bob.post("/api/auth/login",
                 json={"username": "bobtest", "password": "reset12345"})
    ok("重置后的密码可登录", r.status_code == 200, r.text[:120])

    print("\n" + "=" * 58)
    print(f"  通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("  失败：")
        for f in FAIL:
            print("    ·", f)
    print("=" * 58)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
