"""受控的外部检索通道 —— 只产「线索」，绝不产「结论」。

v117 留了个 `register_search_adapter()` 注册位却没有任何实现，`source_policy()`
标成 external 时其实一次网都没联过。这一层把链路补全，同时守住四条边界：

1. **默认关闭。** `CASE_SEARCH_ADAPTER=1` 只是「允许」，还要调用方显式请求
   （SSE / 接口里的 `external=1`、前端那个按钮）才真发请求。不带 key 的
   默认包放在有网机器上，也不会悄悄往外部发任何东西。
2. **先看网络。** 拿 `netprobe` 的结论做门控；无网直接返回 `no_network`，
   判例参考照旧来自本地已核验的 21 件典型案例，一个字都不改。
3. **只认约定好的 JSON，不猜 HTML。** 官方源没有稳定的公开检索接口，
   把某个网页的 DOM 结构写死在代码里，结果只会是「看起来能用、其实永远
   解析失败」。`LAW_CASE_SEARCH_URL` 用 `{q}` 占位，返回 `{"items":[...]}`；
   没配就明说 `no_source`，不假装搜过。
4. **线索不进结论。** 外部条目带 `unverified: true`，只放在
   `scope.external`，**不进 `similar.items`、不参与结局分布计数、
   不覆盖任何条号与金额**。带百分比胜率、成功率、「建议申诉」字样的
   条目直接丢掉 —— 免得外部文案把「本系统不提供胜诉率」的承诺撞穿。

硬上限都在代码里：超时、响应体 256KB、最多 8 条、字段长度截断。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import netprobe
from .config import (CASE_SEARCH_ADAPTER, LAW_CASE_SEARCH_TIMEOUT_MS,
                     LAW_CASE_SEARCH_URL)

MAX_ITEMS = 8
MAX_BYTES = 256 * 1024
MAX_TITLE = 120
MAX_SNIPPET = 300

_custom: Callable[[str, int], list[dict]] | None = None

# 这些词出现在外部片段里，会让整份决策卡和「不给胜率 / 不替人决定」
# 的承诺自相矛盾。宁可少给一条线索，也不放它进来。
_BAD_TEXT = re.compile(
    r"\d+(?:\.\d+)?\s*%|胜诉率|成功率|赢面|(?:不?建议|应当|不该)(?:申诉|起诉|仲裁)")


def enabled() -> bool:
    return bool(CASE_SEARCH_ADAPTER) or _custom is not None


def register(fn: Callable[[str, int], list[dict]] | None) -> None:
    """调用方自接数据源时用。注册后依然受网络探测门控和线索边界约束。"""
    global _custom
    _custom = fn


def has_custom() -> bool:
    return _custom is not None


def _allow_url(url: str) -> bool:
    """只允许 https；本地回环的 http 是给离线端到端测试留的口子。"""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:                                       # noqa: BLE001
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        host = (u.hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")
    return False


def _clean_items(data: Any, limit: int) -> tuple[list[dict], int]:
    """只放行结构完整、文案不越界的条目。返回 (条目, 被丢掉的条数)。"""
    raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("外部检索返回体缺 items 数组")
    out: list[dict] = []
    dropped = 0
    for it in raw[: MAX_ITEMS * 3]:
        if not isinstance(it, dict):
            dropped += 1
            continue
        title = str(it.get("title") or "").strip()[:MAX_TITLE]
        url = str(it.get("url") or "").strip()
        snippet = str(it.get("snippet") or "").strip()[:MAX_SNIPPET]
        if not title or not _allow_url(url):
            dropped += 1
            continue
        if _BAD_TEXT.search(title) or _BAD_TEXT.search(snippet):
            dropped += 1
            continue
        out.append({"title": title, "url": url, "snippet": snippet,
                    "unverified": True,
                    "tag": "外部线索 · 未经本地条号核验"})
        if len(out) >= limit:
            break
    return out, dropped


def _base() -> dict[str, Any]:
    return {"enabled": enabled(), "items": [], "status": "disabled",
            "degraded": False, "filtered": 0, "source": "",
            "note": "外部检索默认关闭；本案参考仍来自本地已核验的判例库。",
            "no_conclusion": "外部线索不参与结局分布，不改变结论、条号与金额。"}


def search(question: str, limit: int = 4) -> dict[str, Any]:
    """检索外部线索。任何失败都返回明确 status，绝不抛到答案链路。"""
    base = _base()
    if not enabled():
        return base
    net = netprobe.status()
    if not net["online"]:
        return {**base, "status": "no_network", "degraded": True,
                "network": net["label"],
                "note": "未检测到可用网络，已自动跳过外部检索；"
                        "判例参考仍来自本地已核验的判例库。"}
    lim = max(1, min(int(limit), MAX_ITEMS))
    q = str(question or "").strip()[:200]

    if _custom is not None:
        try:
            data = {"items": _custom(q, lim)}
        except Exception as e:                              # noqa: BLE001
            return {**base, "status": "adapter_error",
                    "note": f"外部检索适配器失败，已跳过：{type(e).__name__}"}
    else:
        tpl = LAW_CASE_SEARCH_URL
        if not tpl:
            return {**base, "status": "no_source",
                    "note": "已允许联网但未配置检索源（LAW_CASE_SEARCH_URL），"
                            "本次没有发出任何请求。"}
        url = tpl.replace("{q}", urllib.parse.quote(q))
        if not _allow_url(url):
            return {**base, "status": "bad_url",
                    "note": "检索源地址不合法（仅允许 https，本地测试可用回环 http），已拦下。"}
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "law-agent/1.0 (local, read-only)",
                              "Accept": "application/json"})
            with urllib.request.urlopen(
                    req, timeout=max(0.2, LAW_CASE_SEARCH_TIMEOUT_MS / 1000.0)) as r:
                raw = r.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return {**base, "status": "too_large",
                        "note": "外部检索返回体超过 256KB，已整批丢弃。"}
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:                              # noqa: BLE001
            return {**base, "status": "request_error", "source": tpl,
                    "note": f"外部检索请求失败，已自动降级：{type(e).__name__}"}
        # 只回传模板，不回传拼好 {q} 的地址：后者会把用户提问的编码串
        # 写进输出，既多余又可能让「不含百分比」那类断言被 URL 编码撞红。
        base["source"] = tpl

    try:
        items, dropped = _clean_items(data, lim)
    except Exception as e:                                  # noqa: BLE001
        return {**base, "status": "bad_response",
                "note": f"外部检索返回格式不符，已整批丢弃：{type(e).__name__}"}
    return {**base, "status": "ok" if items else "empty",
            "items": items, "filtered": dropped,
            "note": f"共 {len(items)} 条外部线索，均未经本地条号核验；"
                    "只作进一步查证的入口，不作结论依据。" if items else
                    "外部源没有返回可用线索；本地判例库不受影响。"}
