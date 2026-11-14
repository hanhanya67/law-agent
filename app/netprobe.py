"""网络连通探测 —— 只回答「现在能不能连上」，不回答「该不该联网」。

这一层存在的理由：交付包要在纯离线机器上跑，也要在评委的有网笔记本上跑。
以前「有没有网」全靠猜：配置里写了 key 就以为在线，拔了网线照样等一次
API 超时；v117 的 `CASE_SEARCH_ADAPTER` 更是个死开关 —— 设 1 只是把来源
标签改成 external，从头到尾没真的连过一次。

三条硬规矩：

1. **只探 TCP，不发业务请求。** 目标默认跟着 `DEEPSEEK_BASE_URL` 的主机走
   （在线能力本来就要连它），握手成功立刻关掉。不调 API、不花钱、不触发
   风控，也绝不把用户提问带上网络。探一个不相干的公网 IP 会造出「有网但
   真正要用的那个目标不通」的假阳性。
2. **绝不阻塞答案链路。** 探测在后台线程里做，缓存没就绪时一律返回保守的
   「暂按离线」，而不是让第一个提问的人干等 DNS。成功和失败都进 TTL 缓存
   （默认 300s），断网时最怕的不是慢一次，是每问一句都重探一次。
3. **任何异常都归成离线。** DNS 失败、超时、RESET、目标拼错，都不能把异常
   抛到答案里。`source_policy()` 拿到 offline 就自动降级回本地判例库，
   答案正文、定性、金额、条号一个字不改。

边界：**「自动检测」不等于「自动联网」。** 检测到有网只是让外部检索具备
可用性；真要发检索请求还要 `CASE_SEARCH_ADAPTER=1` 且调用方显式要，
见 `websearch.py`。不带 key 的默认包不会悄悄往外部发任何请求。
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.parse
from typing import Any

from .config import (DEEPSEEK_BASE_URL, LAW_NET_MODE, LAW_NET_PROBE_HOST,
                     LAW_NET_PROBE_PORT, LAW_NET_TIMEOUT_MS, LAW_NET_TTL)

_MODES = ("auto", "online", "offline")

_lock = threading.Lock()
_inflight = False
# online 为 None 表示「还没探过」，而不是「不通」——两者在界面上完全不同。
_cache: dict[str, Any] = {
    "online": None, "checked_at": 0.0, "wall": 0.0,
    "reason": "not_checked", "latency_ms": 0,
    "host": "", "port": 0, "probed": False,
}


def mode() -> str:
    """非法取值一律回落 auto，不因为一个拼错的环境变量把联网静默关掉。"""
    return LAW_NET_MODE if LAW_NET_MODE in _MODES else "auto"


def target() -> tuple[str, int]:
    """探测目标。显式 LAW_NET_PROBE_HOST 优先，否则取大模型 API 主机名。"""
    host = LAW_NET_PROBE_HOST
    if not host:
        try:
            host = urllib.parse.urlparse(DEEPSEEK_BASE_URL).hostname or ""
        except Exception:                                   # noqa: BLE001
            host = ""
    if not host:
        host = "api.deepseek.com"
    port = LAW_NET_PROBE_PORT
    if not 1 <= port <= 65535:
        port = 443
    return host, port


def _tcp(host: str, port: int, timeout_s: float) -> tuple[bool, str, int]:
    """握手即关。返回值里的 reason 只给类别，不把系统异常原文带进界面。"""
    t0 = time.monotonic()
    ok, reason = False, "error"
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            pass
        ok, reason = True, "ok"
    except socket.gaierror:
        reason = "dns"
    except (socket.timeout, TimeoutError):
        reason = "timeout"
    except ConnectionRefusedError:
        reason = "refused"
    except OSError:
        reason = "unreachable"
    except Exception:                                       # noqa: BLE001
        reason = "error"
    return ok, reason, int((time.monotonic() - t0) * 1000)


def _snapshot() -> dict[str, Any]:
    """缓存的可读副本。锁内调用。"""
    c = dict(_cache)
    return {
        "mode": mode(),
        "online": c["online"],
        "reason": c["reason"],
        "host": c["host"], "port": c["port"],
        "latency_ms": c["latency_ms"],
        "checked_at": c["wall"] or None,
        "probed": bool(c["probed"]),
        "ttl": max(1, LAW_NET_TTL),
    }


def _fresh() -> bool:
    return (_cache["online"] is not None
            and time.monotonic() - float(_cache["checked_at"]) < max(1, LAW_NET_TTL))


def _release_inflight() -> None:
    global _inflight
    with _lock:
        _inflight = False


def _do_probe() -> dict[str, Any]:
    """真正做一次探测。网络 IO 放在锁外，避免 DNS 卡住时连累所有请求。"""
    host, port = "", 0
    ok, reason, ms = False, "error", 0
    try:
        host, port = target()
        ok, reason, ms = _tcp(host, port,
                              max(0.05, LAW_NET_TIMEOUT_MS / 1000.0))
    except Exception:                                       # noqa: BLE001
        ok, reason = False, "error"
    with _lock:
        _inflight = False
        _cache.update({"online": ok, "checked_at": time.monotonic(),
                       "wall": time.time(), "reason": reason,
                       "latency_ms": ms, "host": host, "port": port,
                       "probed": True})
        return _snapshot()


def _kick() -> None:
    """后台补探一次。重复的后台线程没有意义，用 _inflight 挡住。"""
    global _inflight
    with _lock:
        if _inflight or mode() != "auto":
            return
        _inflight = True

    def _run() -> None:
        try:
            _do_probe()
        except Exception:                                   # noqa: BLE001
            # 连 _do_probe 都炸了（不是网络问题，是代码问题）也不能把
            # _inflight 永远留在 True，否则之后所有探测都被当成「正在探测」。
            _release_inflight()

    threading.Thread(target=_run, name="netprobe", daemon=True).start()


def probe(force: bool = False) -> dict[str, Any]:
    """当前网络结论。`force=True` 同步探测，给测试与显式刷新用。

    普通调用绝不发网络请求：缓存过期或还没探过，就先给一个保守的
    provisional 结论并踢一次后台补探。这样「自动检测」不会变成
    「每个提问都先等一次 DNS」。
    """
    m = mode()
    if m != "auto":
        return {"mode": m, "online": m == "online", "reason": f"forced_{m}",
                "host": "", "port": 0, "latency_ms": 0, "checked_at": None,
                "probed": False, "ttl": max(1, LAW_NET_TTL)}
    with _lock:
        if _fresh():
            return _snapshot()
    if force:
        return _do_probe()
    _kick()
    return {**_snapshot(), "online": False, "reason": "checking",
            "provisional": True}


def status() -> dict[str, Any]:
    """给 /api/status、/api/meta 和来源标签用的可读结论。"""
    p = probe()
    online = bool(p["online"])
    if p["reason"] == "checking":
        label = "正在检测网络，暂按离线处理"
    elif online and p["mode"] == "online":
        label = "网络状态手动设为在线（未实际探测）"
    elif online:
        label = f"网络可用（{p['host']}:{p['port']} 可达，{p['latency_ms']}ms）"
    elif p["mode"] == "offline":
        label = "网络状态手动设为离线"
    else:
        label = "未检测到可用网络，已自动降级到本地判例库"
    return {**p, "label": label,
            "note": "检测只做 TCP 握手，不发送任何业务请求或提问内容；"
                    "结果缓存期内复用，外部检索仍需单独开启。"}


def effective_online() -> bool:
    """判例层唯一该用的入口：别让各处自己解释 reason。"""
    return bool(probe()["online"])


def clear_cache() -> None:
    """测试夹具用：清掉缓存，让下一次 probe 真的去探。"""
    global _inflight
    with _lock:
        _inflight = False
        _cache.update({"online": None, "checked_at": 0.0, "wall": 0.0,
                       "reason": "not_checked", "latency_ms": 0,
                       "host": "", "port": 0, "probed": False})


def start_background_probe() -> None:
    """服务启动时调一次：把首次探测挪到启动阶段，别留给第一个提问的人。"""
    if mode() == "auto":
        _kick()
