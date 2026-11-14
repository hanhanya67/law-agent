# -*- coding: utf-8 -*-
"""
无主会话的导出与清理。

什么是无主会话：
  v20 加账号体系之前，sessions 表没有 user_id 这一列。那时候建的会话在迁移
  后 user_id 为 NULL。它们有三个特点：
    · 谁的历史列表里都不会出现（历史只查 current_user 自己的）
    · 管理员在「会话审计」里能看到一堆匿名记录，第一句提问还是当时的调试内容
    · 一旦误点「查看正文」，还会在审计里留下一条「看了某个人的会话」

为什么默认不删：
  这些数据本身没错，删是不可逆的。所以默认只统计；要删必须**先导出**，
  导出文件校验通过才允许删。顺序错了就是丢数据。

跑法：
    ./.venv/Scripts/python.exe scripts/archive_orphans.py                # 只看
    ./.venv/Scripts/python.exe scripts/archive_orphans.py --export       # 导出到 data/archives/
    ./.venv/Scripts/python.exe scripts/archive_orphans.py --export --purge   # 导出并删除
    ./.venv/Scripts/python.exe scripts/archive_orphans.py --guest 演示账号   # 改成归属到某个账号
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

from app.config import DB_PATH     # noqa: E402


def _title(conn: sqlite3.Connection, sid: str) -> str:
    row = conn.execute(
        "SELECT content FROM messages WHERE session_id=? AND role='user' "
        "ORDER BY id LIMIT 1", (sid,)).fetchone()
    return (row[0][:60] if row and row[0] else "（无提问）")


def export(conn: sqlite3.Connection, sids: list[str], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for sid in sids:
            s = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                             (sid,)).fetchone()
            msgs = conn.execute(
                "SELECT role, content, ts FROM messages WHERE session_id=? "
                "ORDER BY id", (sid,)).fetchall()
            trs = conn.execute(
                "SELECT turn, step_index, tool, args, summary, elapsed_ms, error, ts"
                " FROM traces WHERE session_id=? ORDER BY id", (sid,)).fetchall()
            f.write(json.dumps({
                "session_id": sid,
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
                "title": _title(conn, sid),
                "messages": [dict(m) for m in msgs],
                "traces": [dict(t) for t in trs],
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def verify_export(path: Path, sids: list[str]) -> tuple[bool, str]:
    """导出必须能读回来、行数对得上、session_id 一个不少。

    只检查「文件存在且非空」是不够的：写到一半被打断也会得到一个非空文件。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False, "导出文件不存在或为空"
    got = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                got.add(json.loads(line)["session_id"])
    except Exception as e:            # noqa: BLE001
        return False, f"导出文件解析失败：{e}"
    missing = set(sids) - got
    if missing:
        return False, f"导出缺 {len(missing)} 个会话"
    return True, f"{len(got)} 条可读回"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="导出为 jsonl")
    ap.add_argument("--out", default="", help="导出路径（默认 data/archives/orphans_*.jsonl）")
    ap.add_argument("--purge", action="store_true", help="删除（必须同时 --export）")
    ap.add_argument("--guest", default="", help="改为归属到这个用户名，而不是删除")
    ap.add_argument("--limit-print", type=int, default=8, help="打印几条样例")
    args = ap.parse_args()

    db = Path(DB_PATH)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    sids = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE user_id IS NULL "
        "ORDER BY created_at")]
    if not sids:
        print("没有无主会话，不需要处理。")
        return 0

    def cnt(sql: str, args_: tuple = ()) -> int:
        return int(conn.execute(sql, args_).fetchone()[0])

    ph = ",".join("?" * len(sids))
    n_msg = cnt(f"SELECT COUNT(*) FROM messages WHERE session_id IN ({ph})", sids)
    n_tr = cnt(f"SELECT COUNT(*) FROM traces WHERE session_id IN ({ph})", sids)
    n_turn = cnt(f"SELECT COUNT(*) FROM turns WHERE session_id IN ({ph})", sids)
    t0, t1 = conn.execute(
        "SELECT MIN(created_at), MAX(created_at) FROM sessions "
        "WHERE user_id IS NULL").fetchone()

    def fmt(t: float | None) -> str:
        return dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M") if t else "—"

    print(f"库：{db}")
    print(f"无主会话 {len(sids)} 个｜消息 {n_msg} 条｜轨迹 {n_tr} 行｜轮次 {n_turn} 条")
    print(f"时间范围：{fmt(t0)} → {fmt(t1)}")
    print(f"\n前 {min(args.limit_print, len(sids))} 条样例：")
    for sid in sids[:args.limit_print]:
        print(f"  {sid:<20} {_title(conn, sid)}")
    if len(sids) > args.limit_print:
        print(f"  …… 另 {len(sids) - args.limit_print} 个")

    if not (args.export or args.purge or args.guest):
        print("\n（只读模式，没有改动任何数据）")
        print("  导出：--export        清理：--export --purge")
        return 0

    # ---- 归属到某个账号：不删数据，把孤儿认领给一个真实用户
    if args.guest:
        row = conn.execute("SELECT id, username FROM users WHERE username=?",
                           (args.guest,)).fetchone()
        if not row:
            print(f"\n没有这个账号：{args.guest}")
            return 1
        conn.executemany("UPDATE sessions SET user_id=? WHERE session_id=?",
                         [(row["id"], sid) for sid in sids])
        conn.commit()
        print(f"\n已把这 {len(sids)} 个会话归属到「{row['username']}」（数据未删）")
        return 0

    # ---- 导出
    out = Path(args.out) if args.out else (
        ROOT / "data" / "archives" /
        f"orphans_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    n = export(conn, sids, out)
    ok, msg = verify_export(out, sids)
    print(f"\n导出 {n} 条 → {out}  ({out.stat().st_size // 1024} KB)")
    print(f"  校验：{'PASS' if ok else 'FAIL'} — {msg}")
    if not ok:
        print("导出不可用，已中止，没有删除任何数据。")
        return 1

    if not args.purge:
        print("\n已导出。要真正删除，加 --purge 再跑一次。")
        return 0

    # ---- 删除。顺序：先子表后主表，避免留下孤儿行
    conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", sids)
    conn.execute(f"DELETE FROM traces WHERE session_id IN ({ph})", sids)
    conn.execute(f"DELETE FROM turns WHERE session_id IN ({ph})", sids)
    conn.execute(f"DELETE FROM sessions WHERE session_id IN ({ph})", sids)
    conn.commit()

    left = cnt("SELECT COUNT(*) FROM sessions WHERE user_id IS NULL")
    print(f"\n已删除 {len(sids)} 个无主会话（消息 {n_msg}、轨迹 {n_tr}、轮次 {n_turn}）")
    print(f"剩余无主会话：{left}")
    print(f"归档文件保留在：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
