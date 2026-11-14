# -*- coding: utf-8 -*-
"""网络自动检测与受控联网回归（v118，全程不依赖外网）。

这套测试要守住的不是「能不能连上网」，而是四条一旦破了很难被发现的边界：

  * **自动检测绝不阻塞答案链路**：普通 `probe()` 不发同步网络 IO；缓存
    没就绪时给保守的 checking/离线，而不是让第一个提问的人干等 DNS。
  * **自动检测 ≠ 自动联网**：默认 `/` 外部检索关闭时，即使显式带
    `with_external=True`，也不许发出任何请求。
  * **无网自动降级**：允许了外部检索但检测不到网络，要如实标 degraded，
    本地 21 件已核验案例一件不少、结局计数一个不变。
  * **线索不进结论**：外部条目只放 `scope.external`，带 `unverified`，
    不进 `similar.items`、不改金额/条号、不给胜率。含百分比或
    「建议申诉」的片段一律整条丢掉。

真联网用的是**本机回环**，不是 mock：临时 TCP 监听验握手/拒绝，临时
HTTP 服务验 URL 编码、JSON 解析、大小上限、格式错误、请求失败。
这样「链路真的通」和「链路断时优雅降级」都能在不碰外网的前提下验证。

篡改自检（改完记得改回来）：
  · 删掉 `websearch._BAD_TEXT` 的过滤 → [6] 反伪精确那组会红；
  · 把 `netprobe.probe()` 的非 force 分支改成同步探测 → [2] 会红；
  · 让 `cases.build_decision` 把外部条目并进 `similar.items` → [7] 会红；
  · 把 `source_policy()` 的 label 改成「已联网」→ [4] 会红。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("DEEPSEEK_API_KEY", None)   # 强制回放：本套件不该花一分钱

from app import cases as C            # noqa: E402
from app import extras as X           # noqa: E402
from app import netprobe as N         # noqa: E402
from app import websearch as W        # noqa: E402

TOTAL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}   {detail}")


_NKEYS = ("LAW_NET_MODE", "LAW_NET_PROBE_HOST", "LAW_NET_PROBE_PORT",
          "LAW_NET_TIMEOUT_MS", "LAW_NET_TTL")
_WKEYS = ("CASE_SEARCH_ADAPTER", "LAW_CASE_SEARCH_URL",
          "LAW_CASE_SEARCH_TIMEOUT_MS", "MAX_BYTES", "MAX_ITEMS")
_CKEYS = ("CASE_SEARCH_ADAPTER",)
_ORIG = ({k: getattr(N, k) for k in _NKEYS},
         {k: getattr(W, k) for k in _WKEYS},
         {k: getattr(C, k) for k in _CKEYS})


def restore() -> None:
    """每节之间把模块级配置恢复干净，别让上一节的夹具污染下一节。"""
    for k, v in _ORIG[0].items():
        setattr(N, k, v)
    for k, v in _ORIG[1].items():
        setattr(W, k, v)
    for k, v in _ORIG[2].items():
        setattr(C, k, v)
    C.register_search_adapter(None)       # 同时清掉 W._custom
    N.clear_cache()


def allow_external(online: bool = True) -> None:
    """模拟「允许了外部检索」+「自动检测结果是 XX」。"""
    W.CASE_SEARCH_ADAPTER = True
    C.CASE_SEARCH_ADAPTER = True
    N.LAW_NET_MODE = "online" if online else "offline"


# ---------------------------------------------------------------- 本地夹具

def tcp_listener() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    port = int(s.getsockname()[1])

    def _accept() -> None:
        while True:
            try:
                conn, _ = s.accept()
                conn.close()
            except OSError:
                return

    threading.Thread(target=_accept, name="netprobe-fixture", daemon=True).start()
    return s, port


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {"items": []}
    seen: dict = {}

    def do_GET(self) -> None:                              # noqa: N802
        type(self).seen["path"] = self.path
        body = json.dumps(type(self).payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:                  # noqa: D102
        pass


def http_server() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, name="websearch-fixture",
                     daemon=True).start()
    return srv, int(srv.server_address[1])


GOOD_ITEMS = [
    {"title": "劳动争议典型案例（示例）", "url": "https://www.mohrss.gov.cn/xx/1",
     "snippet": "官方发布的案例要点，供进一步查证。"},
    {"title": "工资支付相关规定", "url": "https://www.court.gov.cn/xx/2",
     "snippet": "关于拖欠工资的处理口径。"},
    {"title": "这个不能要：胜率 73%", "url": "https://example.com/a",
     "snippet": "胜诉率 73%，包赢。"},
    {"title": "这个也不能要", "url": "https://example.com/b",
     "snippet": "建议申诉，稳赚。"},
    {"title": "非法来源要丢掉", "url": "ftp://example.com/c", "snippet": "x"},
]


# ---------------------------------------------------------------- [1] 模式

def test_modes() -> None:
    print("\n[1] 三种模式与探测目标")
    restore()
    for m, want, reason in (("offline", False, "forced_offline"),
                            ("online", True, "forced_online")):
        N.LAW_NET_MODE = m
        s = N.status()
        ok(f"{m} 模式结论固定为 {want} 且不探测",
           s["online"] is want and s["probed"] is False
           and s["reason"] == reason, str(s))
        ok(f"{m} 模式 label 明说是手动设定", "手动设为" in s["label"], s["label"])

    N.LAW_NET_MODE = "拼错了"
    ok("非法模式回落 auto，不静默关掉检测",
       N.mode() == "auto", N.mode())
    ok("三模式白名单钉死", N._MODES == ("auto", "online", "offline"),
       str(N._MODES))

    N.LAW_NET_MODE = "auto"
    # 把后台补探的目标钉在本机拒绝端口上，别让测试去碰真实外网 DNS。
    N.LAW_NET_PROBE_HOST = "127.0.0.1"
    N.LAW_NET_PROBE_PORT = 1
    N.LAW_NET_TIMEOUT_MS = 60
    N.clear_cache()
    s = N.probe()
    ok("auto 且缓存未就绪 → 保守按离线，不阻塞",
       s["online"] is False and s["reason"] == "checking"
       and s.get("provisional") is True, str(s))
    ok("checking 状态有可读文案", "暂按离线" in N.status()["label"], s["reason"])

    N.LAW_NET_PROBE_HOST = ""
    N.LAW_NET_PROBE_PORT = 443
    host, port = N.target()
    ok("未显式配置时探测目标跟随大模型 API 主机",
       bool(host) and host == urllib.parse.urlparse(
           N.DEEPSEEK_BASE_URL).hostname and port == 443, f"{host}:{port}")
    N.LAW_NET_PROBE_HOST = "127.0.0.1"
    N.LAW_NET_PROBE_PORT = 99999
    ok("非法端口回落 443，不抛",
       N.target()[1] == 443, str(N.target()))
    ok("硬上限常量没被悄悄改小",
       W.MAX_ITEMS == 8 and W.MAX_BYTES == 256 * 1024,
       f"{W.MAX_ITEMS}/{W.MAX_BYTES}")


# ---------------------------------------------------------------- [2] 探测

def test_tcp_probe() -> None:
    print("\n[2] 真实 TCP 握手、缓存与非阻塞")
    restore()
    srv, port = tcp_listener()
    try:
        N.LAW_NET_MODE = "auto"
        N.LAW_NET_PROBE_HOST = "127.0.0.1"
        N.LAW_NET_PROBE_PORT = port
        N.LAW_NET_TIMEOUT_MS = 800
        N.LAW_NET_TTL = 300
        N.clear_cache()
        a = N.probe(force=True)
        ok("本地监听可达 → online=True / reason=ok",
           a["online"] is True and a["reason"] == "ok", str(a))
        ok("探测结果记下目标与耗时",
           a["host"] == "127.0.0.1" and a["port"] == port
           and a["latency_ms"] >= 0 and a["probed"] is True, str(a))
        ok("status() 给出人类可读的在线文案",
           "网络可用" in N.status()["label"], N.status()["label"])

        calls = {"n": 0}
        real = N._tcp

        def counting(*args, **kw):
            calls["n"] += 1
            return real(*args, **kw)

        N._tcp = counting
        b = N.probe()
        ok("缓存命中：普通 probe() 不再做网络 IO",
           calls["n"] == 0 and b["online"] is True, str(calls))
        ok("缓存命中时 checked_at 不变",
           b["checked_at"] == a["checked_at"], str(b["checked_at"]))
        N._tcp = real

        N.clear_cache()
        s = N.probe()
        ok("缓存清空后普通 probe() 仍不做同步 IO",
           s["reason"] == "checking" and s["online"] is False, str(s))
        ok("显式 force 才同步重探并拿到可用结论",
           N.probe(force=True)["online"] is True)
    finally:
        srv.close()

    N.clear_cache()
    down = N.probe(force=True)
    ok("监听关闭 → 自动判离线，不抛异常",
       down["online"] is False and down["reason"] in
       ("refused", "unreachable", "timeout", "error"), str(down))
    ok("离线 label 明说已自动降级",
       "降级" in N.status()["label"], N.status()["label"])


# ---------------------------------------------------------------- [3] 异常

def test_fail_closed() -> None:
    print("\n[3] 任何异常一律按离线")
    restore()
    N.LAW_NET_MODE = "auto"
    N.LAW_NET_PROBE_HOST = "127.0.0.1"
    N.LAW_NET_PROBE_PORT = 1
    N.LAW_NET_TIMEOUT_MS = 60
    N.clear_cache()
    real_tcp = N._tcp

    def boom(*_args, **_kw):
        raise RuntimeError("模拟底层探测代码炸了")

    N._tcp = boom
    try:
        s = N.probe(force=True)
        ok("底层探测抛异常也归成离线，绝不外抛",
           s["online"] is False, str(s))
    except Exception as e:                                  # noqa: BLE001
        ok("底层探测抛异常也归成离线，绝不外抛", False, repr(e))
    finally:
        N._tcp = real_tcp
    ok("effective_online() 与 status() 结论一致",
       N.effective_online() is False)

    real_target = N.target
    N.target = lambda: (_ for _ in ()).throw(RuntimeError("bad host"))
    N.clear_cache()
    try:
        ok("target() 拼装失败也不外抛",
           N.probe(force=True)["online"] is False)
    finally:
        N.target = real_target

    N.LAW_NET_PROBE_HOST = "127.0.0.1"
    N.LAW_NET_PROBE_PORT = 1
    N.LAW_NET_TIMEOUT_MS = 50
    N.clear_cache()
    s2 = N.probe(force=True)
    ok("极短超时也不抛", s2["online"] is False, str(s2))
    ok("reason 只给类别，不带系统异常原文",
       s2["reason"] in ("refused", "unreachable", "timeout", "error", "dns"),
       s2["reason"])
    ok("探测全程只握手不发业务请求（源码里没有 urlopen）",
       "urlopen" not in (ROOT / "app" / "netprobe.py").read_text(encoding="utf-8"))
    restore()


# ---------------------------------------------------------------- [4] 默认

def test_default_off() -> None:
    print("\n[4] 默认不联网：自动检测不等于自动联网")
    restore()
    ok("外部检索默认关闭", W.enabled() is False)
    r = W.search("拖欠工资")
    ok("默认 search() 直接返回 disabled，items 为空",
       r["status"] == "disabled" and r["items"] == [], str(r))
    ok("默认说明明说未发出请求", "默认关闭" in r["note"], r["note"])

    p = C.source_policy()
    ok("默认来源 label 仍是「本地判例库，未联网」",
       p["label"] == "本地判例库，未联网", p["label"])
    ok("默认 web_enabled=False / degraded=False",
       p["web_enabled"] is False and p["degraded"] is False, str(p))
    ok("默认也带自动检测结论，不假装不知道网络",
       isinstance(p["network"], dict) and "label" in p["network"], str(p["network"]))

    d = C.build_decision("没签劳动合同，还拖欠两个月工资")
    ok("build_decision 顶层仍是四键",
       set(d) == {"scope", "similar", "cost_benefit", "policy"}, str(sorted(d)))
    ok("不请求外部时 scope.external.status=disabled",
       d["scope"]["external"]["status"] == "disabled", str(d["scope"]["external"]))
    e = X.from_case("没签劳动合同，还拖欠两个月工资", with_external=True)
    ok("即使显式 with_external=True，未允许也不发请求",
       e["decision"]["scope"]["external"]["items"] == [],
       str(e["decision"]["scope"]["external"]))
    ok("默认办案包仍是五块",
       set(e) == {"timeline", "claims", "evidence_check", "decision", "persona"},
       str(sorted(e)))
    restore()


# ---------------------------------------------------------------- [5] 降级

def test_no_network_degrade() -> None:
    print("\n[5] 允许了外部检索但没网：自动降级且不改结论")
    restore()
    allow_external(online=False)
    p = C.source_policy()
    ok("web_enabled 被网络门控压成 False",
       p["web_enabled"] is False and p["external_requested"] is True, str(p))
    ok("降级如实标 degraded 并给出原因",
       p["degraded"] is True and "自动跳过" in p["degrade_note"], str(p["degrade_note"]))
    ok("降级时 label 不冒充已联网",
       p["label"] == "本地判例库，未联网", p["label"])

    r = W.search("拖欠工资")
    ok("search() 返回 no_network / degraded=True / 空线索",
       r["status"] == "no_network" and r["degraded"] is True
       and r["items"] == [], str(r))
    ok("降级文案说明本地判例库不受影响", "本地" in r["note"], r["note"])

    q = "没签劳动合同，还拖欠两个月工资"
    plain = C.build_decision(q)
    ext = C.build_decision(q, with_external=True)
    ok("降级后相似案件件数不变",
       len(ext["similar"]["items"]) == len(plain["similar"]["items"]),
       f"{len(ext['similar']['items'])} / {len(plain['similar']['items'])}")
    ok("降级后结局分布逐字段不变",
       ext["similar"]["distribution"] == plain["similar"]["distribution"])
    ok("降级后成本收益逐字段不变",
       ext["cost_benefit"] == plain["cost_benefit"])
    ok("scope.external 标了 degraded",
       ext["scope"]["external"]["degraded"] is True,
       str(ext["scope"]["external"]))
    restore()


# ---------------------------------------------------------------- [6] 真联网

def test_real_http() -> None:
    print("\n[6] 本机回环真 HTTP：URL 编码 / 解析 / 过滤 / 各种失败")
    restore()
    srv, port = http_server()
    try:
        allow_external(online=True)
        W.LAW_CASE_SEARCH_URL = f"http://127.0.0.1:{port}/s?q={{q}}"
        W.LAW_CASE_SEARCH_TIMEOUT_MS = 2000
        _Handler.seen = {}
        _Handler.payload = {"items": GOOD_ITEMS}

        r = W.search("拖欠工资 加班费", limit=3)
        ok("真发起了 HTTP 请求并返回 ok",
           r["status"] == "ok" and len(r["items"]) == 2, str(r["status"]))
        ok("URL 里的 {q} 被正确编码",
           "拖欠工资" in urllib.parse.unquote(_Handler.seen.get("path", "")),
           _Handler.seen.get("path", ""))
        ok("每条线索都带 unverified 与标签",
           all(x.get("unverified") is True and "未经" in x.get("tag", "")
               for x in r["items"]), str(r["items"]))
        ok("含 73% / 胜诉率 / 建议申诉的条目被整条丢掉",
           r["filtered"] >= 2 and not any(
               re.search(r"\d+\s*%|胜诉率|建议申诉",
                         x["title"] + x["snippet"]) for x in r["items"]),
           f"filtered={r['filtered']}")
        ok("非 https 且非回环的来源 URL 被丢掉",
           all(x["url"].startswith("https://") for x in r["items"]),
           str([x["url"] for x in r["items"]]))
        ok("limit 生效且不超过硬上限",
           len(r["items"]) <= 3 <= W.MAX_ITEMS, str(len(r["items"])))
        ok("返回体明说「不作结论依据」",
           "不作结论依据" in r["note"], r["note"])

        # 只扫文案，不扫 URL：合法链接里的 %XX 编码不是「胜率 73%」，
        # 混在一起扫会把正常线索误判成伪精确（假红灯）。
        textblob = r["note"] + " ".join(
            x["title"] + x["snippet"] + x.get("tag", "") for x in r["items"])
        ok("外部线索文案里没有任何百分比数字",
           not re.search(r"\d+(?:\.\d+)?\s*%", textblob), textblob[:160])
        ok("外部线索文案里不出现胜率/成功率/建议申诉",
           not re.search(r"胜诉率|成功率|赢面|(不?建议|应当|不该)(申诉|起诉|仲裁)",
                         textblob), textblob[:160])
        ok("检索源只回传模板，不把提问编码串写进输出",
           "{q}" in r["source"] and "%" not in r["source"], r["source"])

        W.LAW_CASE_SEARCH_URL = "ftp://example.com/?q={q}"
        ok("非 https 的检索源地址被拦下（bad_url）",
           W.search("x")["status"] == "bad_url")

        W.LAW_CASE_SEARCH_URL = ""
        ok("允许联网但未配置检索源 → no_source，不发请求",
           W.search("x")["status"] == "no_source")

        W.LAW_CASE_SEARCH_URL = f"http://127.0.0.1:{port}/s?q={{q}}"
        _Handler.payload = {"foo": 1}
        ok("返回体不是约定 JSON → bad_response，整批丢弃",
           W.search("x")["status"] == "bad_response")

        _Handler.payload = {"items": GOOD_ITEMS}
        W.MAX_BYTES = 80
        ok("超过大小上限 → too_large，整批丢弃",
           W.search("x")["status"] == "too_large")
        W.MAX_BYTES = _ORIG[1]["MAX_BYTES"]

        W.register(lambda q, lim: (_ for _ in ()).throw(RuntimeError("boom")))
        ok("自定义适配器抛异常 → adapter_error，不崩",
           W.search("x")["status"] == "adapter_error")
        W.register(None)

        W.register(lambda q, lim: [
            {"title": "自接数据源", "url": "https://example.org/a",
             "snippet": "一条来自调用方适配器的线索。"}])
        ok("register() 注入的适配器真的生效",
           W.search("x")["status"] == "ok"
           and W.search("x")["items"][0]["title"] == "自接数据源")
        W.register(None)
    finally:
        srv.shutdown()
        srv.server_close()

    allow_external(online=True)
    W.LAW_CASE_SEARCH_URL = "http://127.0.0.1:1/s?q={q}"
    W.LAW_CASE_SEARCH_TIMEOUT_MS = 400
    ok("目标不可达 → request_error，自动降级不抛",
       W.search("x")["status"] == "request_error")

    # v117 留下的单参数注册位，现在必须真的生效而不是只改标签。
    C.register_search_adapter(lambda q: [
        {"title": "老签名适配器", "url": "https://example.org/b", "snippet": "ok"}])
    ok("cases.register_search_adapter 老签名仍可用且真的联网",
       W.enabled() is True and C.external_hints("x")["status"] == "ok")
    ok("注册后 source_policy 标成 external",
       C.source_policy()["mode"] == "external"
       and C.source_policy()["web_enabled"] is True,
       str(C.source_policy()["mode"]))
    restore()


# ---------------------------------------------------------------- [7] 边界

def test_no_conclusion_pollution() -> None:
    print("\n[7] 外部线索绝不进结论 / 金额 / 分布 / 胜率")
    restore()
    srv, port = http_server()
    try:
        allow_external(online=True)
        W.LAW_CASE_SEARCH_URL = f"http://127.0.0.1:{port}/s?q={{q}}"
        _Handler.payload = {"items": GOOD_ITEMS}
        q = "公司没签劳动合同，还拖欠两个月工资，能要多少钱"

        plain = C.build_decision(q)
        online = C.build_decision(q, with_external=True)
        ok("有外部线索时顶层仍是四键",
           set(online) == {"scope", "similar", "cost_benefit", "policy"},
           str(sorted(online)))
        ok("外部线索只出现在 scope.external",
           len(online["scope"]["external"]["items"]) == 2
           and all("外部" not in x["case_id"] for x in online["similar"]["items"]),
           str(online["scope"]["external"]["status"]))
        ok("相似案件列表与离线逐字段完全一致",
           online["similar"]["items"] == plain["similar"]["items"])
        ok("结局分布逐字段完全一致",
           online["similar"]["distribution"] == plain["similar"]["distribution"])
        ok("成本收益逐字段完全一致",
           online["cost_benefit"] == plain["cost_benefit"])
        ok("金额行仍是全角下划线（不拿 BLANK 当期望值）",
           any("＿＿＿＿" in str(r.get("value")) for r in
               online["cost_benefit"]["rows"]),
           str(online["cost_benefit"]["rows"][:2]))
        # 结论卡整份扫，只把外部线索的 url 字段剔掉（%XX 是链接编码，
        # 不是胜率）；其余部分一个字符都不放过。
        scrub = json.loads(json.dumps(online, ensure_ascii=False))
        for x in scrub["scope"]["external"].get("items", []):
            x.pop("url", None)
        blob = json.dumps(scrub, ensure_ascii=False)
        from app.costs import bad_percentages as _bad_pct
        ok("整份决策卡（除链接编码外）不含任何百分比数字",
           not _bad_pct(blob), blob[:160])
        ok("整份决策卡不出现建议/不建议申诉",
           not re.search(r"(不?建议|应当|不该)(申诉|起诉|仲裁)", blob))
        ok("无胜率声明仍在", "不提供胜诉率" in online["policy"]["no_win_rate"])

        a = X.from_case(q)
        b = X.from_case(q, with_external=True)
        ok("办案包前三块联网前后逐字段一致",
           all(a[k] == b[k] for k in
               ("timeline", "claims", "evidence_check")),
           str(sorted(set(a) | set(b))))
        ok("办案包仍是五块，没多出顶层键",
           set(b) == {"timeline", "claims", "evidence_check", "decision", "persona"},
           str(sorted(b)))
        ok("联网只影响 decision 这一块",
           b["persona"] == a["persona"])
    finally:
        srv.shutdown()
        srv.server_close()
    restore()


def main() -> int:
    print("网络自动检测与受控联网回归（v118，强制回放 / 只用本机回环）")
    print("=" * 62)
    test_modes()
    test_tcp_probe()
    test_fail_closed()
    test_default_off()
    test_no_network_degrade()
    test_real_http()
    test_no_conclusion_pollution()
    restore()
    print("\n" + "=" * 62)
    if FAILED:
        print(f"{TOTAL} 项：{TOTAL - len(FAILED)} 通过，{len(FAILED)} 失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"{TOTAL} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
