# -*- coding: utf-8 -*-
"""一个够用的 HTTP 客户端，给脚本用（不是产品代码）。

为什么要有它：
  凡是要打真接口的脚本（造数据、验接口、截图前登录），都得处理三件同样的事——
  1. 先 GET /login 拿到 csrf cookie，否则所有写请求 403
  2. 把 cookie 里的值抄进 X-CSRF-Token 头
  3. `/login` 返回的是 HTML，直接 json.loads 会炸

  这三件事我在一个下午里重写了四遍，第四次还是漏了第 3 条。抽出来只写一遍。

用法：
    from _client import Client
    c = Client()
    c.login("admin", "Lv2026#law")
    print(c.get("/api/admin/overview"))
"""
from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


class Client:
    # base 不给默认值：默认值就是一条没说出口的前提（「8000 上那个服务是我的」），
    # 而这条前提从来没被验证过——详见 scripts/_svc.py 顶部的说明。
    # 调用方应当传自己起的那个服务的地址。
    def __init__(self, base: str, csrf_name: str = "law_csrf"):
        self.base = base.rstrip("/")
        self.csrf_name = csrf_name
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    # ---------- 底层 ----------
    def _token(self) -> str:
        return {c.name: c.value for c in self.jar}.get(self.csrf_name, "")

    def raw(self, path: str, data: dict | None = None,
            method: str | None = None) -> tuple[int, bytes]:
        if path.startswith("http"):
            url = path
        else:
            url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body,
            method=method or ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        tok = self._token()
        if tok:
            req.add_header("X-CSRF-Token", tok)
        try:
            with self.opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def json(self, path: str, data: dict | None = None,
             method: str | None = None) -> tuple[int, object]:
        """永远返回 (状态码, 解析结果或原文片段)。不抛 JSONDecodeError。"""
        st, raw = self.raw(path, data, method)
        try:
            return st, json.loads(raw or b"{}")
        except Exception:            # noqa: BLE001
            return st, {"_raw": raw[:200].decode("utf-8", "replace")}

    # ---------- 常用 ----------
    def warm(self) -> None:
        """取一次页面把 csrf cookie 种上。写请求之前必须先做。"""
        self.raw("/login")

    def login(self, username: str, password: str) -> bool:
        self.warm()
        st, d = self.json("/api/auth/login",
                          {"username": username, "password": password})
        return st == 200

    def get(self, path: str):
        return self.json(path)

    def post(self, path: str, data: dict):
        self.warm()          # cookie 过期时也能自愈
        return self.json(path, data)

    # ---------- 便捷查询 ----------
    def history(self) -> list[dict]:
        return (self.get("/api/chat/history")[1] or {}).get("items") or []

    def session(self, sid: str) -> dict:
        return self.get("/api/chat/session/" + urllib.parse.quote(sid))[1] or {}

    def overview(self) -> dict:
        return self.get("/api/admin/overview")[1] or {}
