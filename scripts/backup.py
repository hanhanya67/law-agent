# -*- coding: utf-8 -*-
"""
数据库备份。**不要用 cp 复制 law.db**，这是本脚本存在的唯一理由。

为什么 cp 会丢数据：
  库跑在 WAL 模式（`PRAGMA journal_mode=WAL`）。WAL 的意义就是「先写
  law.db-wal，攒够了再合并回主库」。所以任一时刻真实数据 = 主库 + WAL。
  实测这台机器上主库 331KB、WAL 947KB —— 直接 cp 主库，等于把最近
  绝大部分写入扔掉，而拷出来的文件还能正常打开、还能查，**看不出少了什么**。

正确做法只有两个：
  1. `VACUUM INTO 'x.db'` —— 由 SQLite 自己把主库 + WAL 拍成一份干净副本，
     顺带做了碎片整理。推荐。
  2. sqlite3 的 backup API（`src.backup(dst)`）—— 逐页复制，可增量。

本脚本用 1，然后**校验**：打开备份跑 `PRAGMA integrity_check`，
再把关键表行数和原库逐一对一遍。备份不校验等于没备份。

跑法：
    ./.venv/Scripts/python.exe scripts/backup.py
    ./.venv/Scripts/python.exe scripts/backup.py --keep 10
    ./.venv/Scripts/python.exe scripts/backup.py --out D:\\somewhere

服务运行中也能跑：WAL 下 VACUUM INTO 是读操作，不挡写入。
"""
from __future__ import annotations

import argparse
import datetime as dt
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

# 备份要核对的表。少一张就等于少一类证据。
# 法条不在库里（在 data/statutes.jsonl），所以没有 articles 表。
TABLES = ["users", "sessions", "messages", "traces", "turns",
          "login_log", "admin_audit", "auth_tokens"]


def count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7, help="保留最近几份（默认 7）")
    ap.add_argument("--out", default="", help="输出目录，默认 data/backups")
    args = ap.parse_args()

    src_path = Path(DB_PATH)
    if not src_path.exists():
        print(f"库不存在：{src_path}")
        return 1

    out_dir = Path(args.out) if args.out else ROOT / "data" / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = out_dir / f"law_{stamp}.db"
    if dst.exists():
        print(f"目标已存在：{dst}")
        return 1

    # 先算一份「备份前」的行数，拿同一把尺子去量备份
    with sqlite3.connect(str(src_path)) as src:
        src_counts = {t: count(src, t) for t in TABLES}
        wal = src_path.with_name(src_path.name + "-wal")
        wal_kb = wal.stat().st_size // 1024 if wal.exists() else 0
        src.execute("VACUUM INTO ?", (str(dst),))

    print(f"源库  ：{src_path}")
    print(f"WAL   ：{wal_kb} KB {'（已并入备份）' if wal_kb else ''}")
    print(f"备份至：{dst}  ({dst.stat().st_size // 1024} KB)")

    # 校验：能不能打开、结构坏没坏、行数对不对
    ok = True
    with sqlite3.connect(str(dst)) as chk:
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print(f"  [FAIL] integrity_check：{integrity}")
            ok = False
        else:
            print("  [PASS] integrity_check")
        for t in TABLES:
            a, b = src_counts[t], count(chk, t)
            if a < 0 and b < 0:
                continue          # 这个库没这张表，不算失败
            same = (a == b)
            ok = ok and same
            flag = "PASS" if same else "FAIL"
            print(f"  [{flag}] {t:<12} {a:>6} 条" + ("" if same else f" → 备份里 {b}"))

    if not ok:
        print("\n备份不完整，已保留文件供排查，不要拿它覆盖原库。")
        return 1

    # 轮转：只留最近 N 份
    olds = sorted(out_dir.glob("law_*.db"))
    for p in olds[:-args.keep] if len(olds) > args.keep else []:
        p.unlink()
        print(f"  清理旧备份：{p.name}")

    print(f"\n完成，当前保留 {min(len(olds), args.keep)} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
