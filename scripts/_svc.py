# -*- coding: utf-8 -*-
"""给「需要一个服务」的脚本用的公共件。

为什么要有它：早先每个脚本都写死 `BASE = "http://127.0.0.1:8000"`，
等于各自埋了一条没说出口的前提——「8000 上那个服务是我的」。
这条前提从来没被验证过，也不成立：开发机上的 8000 常年属于另一个目录。
后果不是报错，而是**测错了实例**，症状离根因隔了好几层（详见 README
「界面交互串台」一节）。

所以对外的约定只有一条：**需要服务就自己起一个，端口向系统要。**
自己起还有个附带好处——断言的一定是本目录的代码与本目录的库。

用法：

    from _svc import start_service
    svc, base = start_service()      # 起好并等到 /api/meta 就绪
    try:
        ...                          # 用 base 拼 URL
    finally:
        stop_service(svc)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    """向系统要一个当前空闲的端口。

    存在「问完还没用就被别人抢走」的窗口，但比起写死一个常年被占的端口，
    这点竞态可以忽略——真撞上了是 uvicorn 起不来，会立刻报错，不会静默串台。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(base: str, timeout: float = 60.0) -> bool:
    """等服务就绪。全新库第一次要建表、灌 2066 条条文，慢是正常的。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/meta", timeout=2) as f:
                if json.load(f).get("articles"):
                    return True
        except Exception:            # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def start_service(root: Path | None = None,
                  env: dict[str, str] | None = None) -> tuple[subprocess.Popen, str]:
    """起一个属于本目录的服务，返回 (进程, base)。

    用 `sys.executable` 而不是 `uvicorn`：不保证 PATH 里有 uvicorn 这个命令，
    但能跑这个脚本的解释器里一定装了它（requirements.txt 里写着）。

    env 用于把服务指到一个临时库（LAW_DB_PATH）。不给就继承当前环境——
    那是老行为，不动。
    """
    root = Path(root or ROOT)
    port = free_port()
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(root), env=run_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    if not wait_ready(base):
        stop_service(proc)
        raise RuntimeError(f"服务没起来（端口 {port}）")
    return proc, base


def stop_service(proc: subprocess.Popen) -> None:
    """收掉自己起的服务。跑一次留一个进程，端口会越攒越多。"""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:                # noqa: BLE001
        proc.kill()
