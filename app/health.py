# -*- coding: utf-8 -*-
"""模型服务的运行时健康状态。

为什么不在 `/api/meta` 里一次性算完：
  那个接口返回的是**启动时**的配置（有没有 key），一旦进程起来就不再变。
  但真实故障是运行中才发生的——余额被扣光、key 被停、供应商 5xx。
  用户看到的却是徽章一直绿着、问一句转圈半天然后报错，分不清「我网断了」
  还是「服务欠费了」。

做法：在唯一出网的那一处（`_api._Completions.create`）挂两个钩子，
成功记一笔、失败记一笔。状态就是「最近一次调用成不成功」，不做推算。
"""
from __future__ import annotations

import time

# 只保留「最近一次」的结果，不留历史——历史在这件事上没有用，
# 用户关心的是「我现在再问一句会不会通」。
_STATE: dict[str, object] = {
    "last_ok": 0.0,
    "last_err": 0.0,
    "err_kind": "",
    "err_msg": "",
    "calls": 0,
    "errors": 0,
}

# 错误种类决定文案。用户不需要看 HTTP 状态码，需要知道「等一会」还是「去充值」。
KIND_TEXT = {
    "quota": "余额不足",
    "auth": "密钥无效",
    "network": "网络不通",
    "server": "服务端异常",
    "other": "调用失败",
}


def _kind(status: int | None) -> str:
    if status == 402:
        return "quota"
    if status in (401, 403):
        return "auth"
    if status is not None and status >= 500:
        return "server"
    return "other"


def note_ok() -> None:
    _STATE["last_ok"] = time.time()
    _STATE["calls"] = int(_STATE["calls"]) + 1


def note_error(status: int | None = None, msg: str = "",
               kind: str | None = None) -> None:
    _STATE["last_err"] = time.time()
    _STATE["err_kind"] = kind or _kind(status)
    # 只留一行，别把整页 HTML 错误页塞进界面
    _STATE["err_msg"] = (msg or "").strip().replace("\n", " ")[:160]
    _STATE["calls"] = int(_STATE["calls"]) + 1
    _STATE["errors"] = int(_STATE["errors"]) + 1


def snapshot() -> dict[str, object]:
    last_ok = float(_STATE["last_ok"])
    last_err = float(_STATE["last_err"])
    kind = str(_STATE["err_kind"])
    # 最后一次调用是失败 → 现在就是坏的。一次失败就报，不做「连续 N 次」的
    # 平滑：这个服务一次调用就几毛钱，宁可报得早，也不要用户连撞三次墙。
    failed = last_err > last_ok
    return {
        "ok": not failed,
        "kind": kind if failed else "",
        "text": KIND_TEXT.get(kind, "调用失败") if failed else "",
        "detail": str(_STATE["err_msg"]) if failed else "",
        "last_ok": last_ok,
        "last_err": last_err,
        "calls": int(_STATE["calls"]),
        "errors": int(_STATE["errors"]),
    }
