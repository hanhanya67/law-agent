# -*- coding: utf-8 -*-
"""把「历史咨询」桶里被并在一起的会话拆成独立任务（一次性迁移）。

背景（v123）：`/api/chat` 现在会给每一次新咨询自动建任务，但**存量**数据里，
所有无主会话曾经被 `assign_loose_to_task()` 扫进同一个「历史咨询」任务。于是
先问食安、再问劳动法，在任务页上只是同一个任务里的两条会话，任务卡的徽章还
跟着最后一条会话变色——看起来就是「上一个任务被覆盖了」。

本脚本不动会话内容、不动消息，只做一件事：给每条有提问的会话建一个自己的
任务，把它挂过去。任务标题取该会话的第一句提问，与 v123 的自动命名同一口径
（`app.server._auto_task_title`），所以拆出来的名字和「现在新问一句」长得一样。

拆完的空桶默认删掉（`--keep-empty` 留着）。空会话（一条提问都没有的）留在
原桶里不动——给它们各建一个任务只会多出一堆空卡片。

默认**只预演，不写库**。真的写要加 `--apply`，写之前自动备份整个数据库。

用法：
    python scripts/split_loose_tasks.py                 # 预演（只读）
    python scripts/split_loose_tasks.py --apply         # 执行（先备份）
    python scripts/split_loose_tasks.py --db D:\\x\\law.db --apply
    python scripts/split_loose_tasks.py --keep-empty    # 保留拆空的桶
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH                     # noqa: E402
from app.memory import MemoryStore                 # noqa: E402

BUCKET = "历史咨询"


def _title_fn():
    """拿到 v123 的自动命名函数。

    为什么是延迟导入 `app.server`：那个模块在 import 时就把 FastAPI 应用、
    MemoryStore、AuthStore 都建起来了（它是 Web 入口，本来就这么设计的）。
    放在函数里，一是只在这一处用得到，二是万一 import 失败能给出人话，
    而不是让脚本在顶上一行 traceback 就死掉。
    """
    try:
        from app.server import _auto_task_title
    except Exception as e:                        # noqa: BLE001
        print(f"拿不到自动命名函数（app.server 导入失败：{e}）\n"
              f"退化成只显示、不改名的模式——任务名直接取会话的第一句提问。")
        return None
    return _auto_task_title


def _backup(db: Path) -> Path:
    """用 sqlite 的 backup API 备份，不直接拷文件。

    库是 WAL 模式：只拷 .db 会漏掉还没 checkpoint 的 -wal 内容，
    备份看着成功、恢复时少一截——这类失误在出事那天才会被发现。
    """
    dst = db.with_name(db.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect(str(db))
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="按会话拆开「历史咨询」桶")
    ap.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预演）")
    ap.add_argument("--keep-empty", action="store_true", help="保留拆空的桶")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"找不到数据库：{db}")
        return 1

    store = MemoryStore(db)
    try:
        rows = store._conn.execute(
            "SELECT task_id, user_id, title FROM tasks WHERE title LIKE ?"
            " ORDER BY user_id, created_at", (BUCKET + "%",)).fetchall()
    except sqlite3.Error as e:
        print(f"读不了 tasks 表：{e}")
        return 1

    if not rows:
        print(f"库里没有以「{BUCKET}」开头的任务——没有要拆的东西。")
        return 0

    names = {}
    try:
        from app.auth import AuthStore
        for u in AuthStore(db).list_users(size=200)["items"]:
            names[int(u["id"])] = u["username"]
    except Exception:                             # noqa: BLE001
        pass

    naming = _title_fn()
    plan = []          # [(bucket_tid, uid, sid, pid, old_title, new_title)]

    print(f"库：{db}")
    print("=" * 68)
    for r in rows:
        tid, uid = r["task_id"], int(r["user_id"])
        sess = store.task_sessions(tid, limit=200)
        live = [s for s in sess if (s.get("n_turns") or 0) > 0]
        empty = len(sess) - len(live)
        print(f"\n桶「{r['title']}」 {tid} · 用户 {names.get(uid, uid)}"
              f" · {len(live)} 条有提问的会话"
              + (f"（另有 {empty} 条空会话，留在原桶）" if empty else ""))
        if not live:
            print("    没有可拆的会话，跳过")
            continue
        for i, s in enumerate(live, 1):
            sid = s["session_id"]
            pid = store.session_profile(sid)
            pname = getattr(_profiles_resolve(pid), "name", pid)
            first = (s.get("title") or "").strip()
            new = naming(first, pname) if naming else (first[:20] or "新咨询")
            print(f"    {i}. {pname:<12} {first[:34]:<36} "
                  f"{s['n_turns']} 轮 → 新任务「{new}」")
            plan.append((tid, uid, sid, pid, first, new))

    if not plan:
        print("\n没有需要拆的会话。")
        return 0

    buckets = {p[0] for p in plan}
    print("\n" + "=" * 68)
    print(f"预演结果：新建 {len(plan)} 个任务、搬 {len(plan)} 条会话、"
          f"涉及 {len(buckets)} 个桶。")
    if not args.apply:
        print("这一步**没有写库**。确认无误后加 --apply 执行（执行前会自动备份）。")
        return 0

    bak = _backup(db)
    print(f"已备份：{bak}")
    made = 0
    for tid, uid, sid, pid, _first, new in plan:
        t = store.create_task(uid, new, profile_id=pid)
        # 直接改 sessions.task_id：ensure_session() 带 COALESCE，只会给
        # 「还没有任务」的会话补归属，搬不动已经在桶里的那些。这里要的正是
        # 「搬走」，所以只能直写。条件是 task_id 仍等于原桶，避免两次运行
        # 之间有人手动挪过。
        with store._lock:
            cur = store._conn.execute(
                "UPDATE sessions SET task_id=? WHERE session_id=? AND task_id=?",
                (t["task_id"], sid, tid))
            store._conn.commit()
        if cur.rowcount:
            made += 1
        else:
            store.delete_task(t["task_id"], uid)   # 没搬成就把空任务收回去
            print(f"  跳过（会话已被挪走）：{sid}")

    dropped = 0
    if not args.keep_empty:
        for tid in buckets:
            left = store._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE task_id=?", (tid,)).fetchone()[0]
            if left == 0:
                owner = store.task_owner(tid)
                if owner is not None and store.delete_task(tid, owner):
                    dropped += 1
    print(f"完成：新建 {made} 个任务"
          + (f"，删除 {dropped} 个拆空的桶" if dropped else "")
          + "。打开任务页就能看到它们各占一张卡。")
    return 0


def _profiles_resolve(pid: str):
    from app.profiles import resolve
    return resolve(pid)


if __name__ == "__main__":
    sys.exit(main())
