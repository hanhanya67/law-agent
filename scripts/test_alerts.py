# -*- coding: utf-8 -*-
"""
告警规则的单元测试。

为什么必须单独测：
  `alerts()` 在干净的库上返回 `[]` —— 空数组既可能是「真的没异常」，
  也可能是「SQL 写错了永远查不到」。这两种情况在调用方看来一模一样，
  只有造出数据才能区分。

所以这里现搭一个临时库，手工塞进刚好触发 / 刚好不触发的数据，
再断言边界：7 条不算批量翻、8 条才算；5 次失败不报、6 次报。

跑法：
    ./.venv/Scripts/python.exe scripts/test_alerts.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

from app.admin import ALERT_FAIL_IP, ALERT_VIEW_SESSIONS, AdminService  # noqa
from app.auth import AuthStore      # noqa: E402
from app.memory import MemoryStore  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: object = "") -> None:
    global fails
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))
    if not ok:
        fails += 1


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="law_alerts_"))
    db = tmp / "t.db"
    try:
        auth = AuthStore(db)
        mem = MemoryStore(db)
        admin = AdminService(auth, mem)
        admin_id = auth.create_user("someone", "Passw0rd!", role="admin")["id"]

        now = time.time()

        # 会话 id 必须全局唯一递增。第一版每次调用都用 range(n)，
        # 第二次插进来的又是 s0、s1…，COUNT(DISTINCT target_id) 把它们
        # 当成重复查看，于是「看 8 个」永远凑不满——这恰好反证了
        # 去重是对的：同一个会话反复看，不该算批量翻。
        _seq = [0]

        def add_views(n: int) -> None:
            """插 n 条「看了不同会话正文」的审计记录，时间都在 1 小时内。"""
            ids = []
            for _ in range(n):
                ids.append(f"s{_seq[0]}")
                _seq[0] += 1
            with sqlite3.connect(str(db)) as c:
                c.executemany(
                    "INSERT INTO admin_audit(admin_id, admin_name, action,"
                    " target_type, target_id, detail, ip, ts)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    [(admin_id, "someone", "view_session", "session",
                      sid, "", "127.0.0.1", now - 60) for sid in ids])

        def fail_logins(n: int, ip: str = "10.0.0.9") -> None:
            with sqlite3.connect(str(db)) as c:
                c.executemany(
                    "INSERT INTO login_log(username, ip, ok, reason, ts)"
                    " VALUES(?,?,?,?,?)",
                    [("admin", ip, 0, "bad_password", now - 30)
                     for _ in range(n)])

        print("告警边界：")

        # --- 干净库：什么都不该报，但也要能区分「查得到但没异常」
        check("空库无告警", admin.alerts() == [], admin.alerts())

        # --- 批量翻会话：阈值减一不报，达到才报
        add_views(ALERT_VIEW_SESSIONS - 1)
        got = [a for a in admin.alerts() if a["title"] == "短时间批量查看会话"]
        check(f"看 {ALERT_VIEW_SESSIONS - 1} 个不报", not got, len(got))
        add_views(1)
        got = [a for a in admin.alerts() if a["title"] == "短时间批量查看会话"]
        check(f"看 {ALERT_VIEW_SESSIONS} 个报 warn", len(got) == 1,
              got[0]["detail"] if got else "无")

        # --- 重复看同一个会话不触发（去重是规则的一部分，不是副作用）
        with sqlite3.connect(str(db)) as c:
            c.execute("DELETE FROM admin_audit")
            c.executemany(
                "INSERT INTO admin_audit(admin_id, admin_name, action,"
                " target_type, target_id, detail, ip, ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(admin_id, "someone", "view_session", "session",
                  "same", "", "127.0.0.1", now - 60) for _ in range(20)])
        got = [a for a in admin.alerts() if a["title"] == "短时间批量查看会话"]
        check("同一会话看 20 次不报", not got, len(got))

        # --- 撞库：同一 IP 失败次数
        fail_logins(ALERT_FAIL_IP - 1)
        got = [a for a in admin.alerts() if a["title"] == "同一 IP 连续登录失败"]
        check(f"失败 {ALERT_FAIL_IP - 1} 次不报", not got, len(got))
        fail_logins(1)
        got = [a for a in admin.alerts() if a["title"] == "同一 IP 连续登录失败"]
        check(f"失败 {ALERT_FAIL_IP} 次报 bad", len(got) == 1,
              got[0]["detail"] if got else "无")

        # --- 锁定账号
        with sqlite3.connect(str(db)) as c:
            c.execute("UPDATE users SET locked_until=? WHERE id=?",
                      (now + 600, admin_id))
        got = [a for a in admin.alerts() if a["title"] == "有账号处于登录锁定"]
        check("锁定中的账号被报出", len(got) == 1, got[0]["detail"] if got else "无")

        # --- 概览必须带上 alerts，否则前端拿不到
        check("overview 带 alerts 字段", "alerts" in admin.overview())

        # --- 时间窗：一小时前看的会话不算
        with sqlite3.connect(str(db)) as c:
            c.execute("UPDATE admin_audit SET ts=?", (now - 7200,))
        got = [a for a in admin.alerts() if a["title"] == "短时间批量查看会话"]
        check("窗外的记录不计入", not got, len(got))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"{fails} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
