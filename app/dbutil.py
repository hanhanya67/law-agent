# -*- coding: utf-8 -*-
"""SQLite 建连的公共件：把「切 WAL」这一步做结实一点。

为什么需要单独一个函数：
  三个存储（AuthStore / MemoryStore / AdminService）都各自建连接，而建完之后
  第一件事都是 `PRAGMA journal_mode=WAL`。这一步在**刚创建出来的库**上并不总是
  一次成功——交付包解压后第一次跑，实测撞到过一次：

      sqlite3.OperationalError: disk I/O error
        self._conn.execute("PRAGMA journal_mode=WAL")

  那不是代码写错了：Windows 上新建的文件会被杀毒/搜索索引短暂占用，而上一条
  PRAGMA 恰好是这个文件上的第一个写操作。加重试之前，它表现为一条裸 traceback，
  外面看起来像「语料降级那一步坏了」——**最不像根因的地方**。

  重试的前提是「这类失败是瞬时的、重试几乎必然成功」，这一点对「文件刚被创建、
  正在被扫描」成立；对权限问题不成立，所以重试次数很少、间隔递增，失败仍然抛。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(path: str | Path, *, timeout: float = 10.0,
            attempts: int = 6) -> sqlite3.Connection:
    """打开连接并切换到 WAL，失败重试几次。

    timeout=10：两个连接（认证域和对话域）会同时写同一个文件。WAL 下读写不
    互斥，但写还是要排队，默认 5 秒在慢盘上会偶发 locked。
    """
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    last: Exception | None = None
    for i in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.OperationalError as e:      # 瞬时占用，不是配置错误
            last = e
            time.sleep(0.15 * (i + 1))
    conn.close()
    assert last is not None
    raise last
