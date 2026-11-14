# -*- coding: utf-8 -*-
"""
管理员救急工具。命令行，不需要 Web 服务。

什么时候会用到它：
  · 忘了管理员密码，又没人能进后台重置 —— 这是最要紧的场景
  · 本机第一次部署，想指定一个自己记得住的密码
  · 最后一个管理员被误操作锁在门外（代码里已经挡住「自己封自己」，
    但如果库是在别处改坏的，这里能救）

用法：
  python scripts/make_admin.py                     没有可用管理员就建一个
  python scripts/make_admin.py list                列出所有账号
  python scripts/make_admin.py passwd <用户名> [新密码]
  python scripts/make_admin.py promote <用户名>     提为管理员并解锁

为什么单独一个脚本而不是做成 HTTP 接口：
  重置管理员密码这件事不能有一个「不需要登录就能调」的接口，
  那等于把后台的钥匙挂在门上。只能由能登服务器的人来做。
"""
from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:                      # Windows 控制台默认 GBK，中文输出会炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:         # noqa: BLE001
    pass

from app.auth import AuthError, AuthStore       # noqa: E402
from app.config import (ADMIN_PASSWORD, ADMIN_USERNAME, DB_PATH)  # noqa: E402


def _ts(t: float | None) -> str:
    import datetime as dt
    if not t:
        return "从未"
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def _ask_password() -> str:
    """有终端就隐藏输入；没有（比如被别的程序调用）就退回随机生成。"""
    try:
        p1 = getpass.getpass("新密码（至少 8 位，含字母和数字）：")
        p2 = getpass.getpass("再输一次：")
    except (EOFError, OSError):
        p = secrets.token_urlsafe(9) + "K7#"
        print(f"  当前环境不能交互输入，已生成随机密码：{p}")
        return p
    if p1 != p2:
        raise SystemExit("两次输入不一致")
    return p1


def cmd_list(store: AuthStore) -> None:
    d = store.list_users(size=100)
    if not d["items"]:
        print("  库里还没有任何账号。")
        return
    print(f"  {'ID':>4}  {'用户名':<16} {'角色':<6} {'状态':<6} {'注册时间':<17} 最后登录")
    for u in d["items"]:
        role = "管理员" if u["role"] == "admin" else "用户"
        stat = "正常" if u["status"] == "active" else "已停用"
        locked = "（登录锁定中）" if (u["locked_until"] or 0) > __import__("time").time() else ""
        print(f"  {u['id']:>4}  {u['username']:<16} {role:<6} {stat:<6} "
              f"{_ts(u['created_at']):<17} {_ts(u['last_login_at'])}{locked}")


def main() -> int:
    args = sys.argv[1:]
    store = AuthStore(DB_PATH)
    print(f"库：{DB_PATH}")

    if not args:
        user, pwd = store.ensure_admin(ADMIN_USERNAME, ADMIN_PASSWORD or None)
        if not user:
            n = store.count_users()
            print(f"  已经存在可用管理员（共 {n['admin']} 个），没有改动。")
            print("  忘了密码就用：python scripts/make_admin.py passwd <用户名>")
            return 0
        print(f"  已创建管理员：{user['username']}")
        if pwd:
            print(f"  一次性密码：{pwd}")
            print("  只显示这一次，请立刻登录改掉。")
        else:
            print("  密码取自环境变量 ADMIN_PASSWORD。")
        return 0

    cmd = args[0]

    if cmd == "list":
        cmd_list(store)
        return 0

    if cmd == "passwd":
        if len(args) < 2:
            print("用法：python scripts/make_admin.py passwd <用户名> [新密码]")
            return 2
        name = args[1]
        u = store.get_user_by_name(name)
        if not u:
            print(f"  没有这个账号：{name}")
            return 1
        pwd = args[2] if len(args) > 2 else _ask_password()
        try:
            store.admin_reset_password(u["id"], pwd)
        except AuthError as e:
            print(f"  失败：{e.message}")
            return 1
        print(f"  已重置「{u['username']}」的密码。该账号所有登录状态已失效。")
        return 0

    if cmd == "promote":
        if len(args) < 2:
            print("用法：python scripts/make_admin.py promote <用户名>")
            return 2
        u = store.get_user_by_name(args[1])
        if not u:
            print(f"  没有这个账号：{args[1]}")
            return 1
        store.set_role(u["id"], "admin")
        store.set_status(u["id"], "active")
        store.unlock(u["id"])
        print(f"  已把「{u['username']}」设为管理员并解除锁定。")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
