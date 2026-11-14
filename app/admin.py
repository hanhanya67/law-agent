# -*- coding: utf-8 -*-
"""
管理端只读查询。跟认证逻辑分开，理由很实际：

  auth.py 管「谁能进来、能做什么」（写操作与权限判断，错了就是安全漏洞）
  admin.py 管「后台要看什么」（跨表聚合，错了只是数字难看）

混在一起的话，改一个图表要动鉴权代码，风险不对等。

为什么自己开一个连接而不复用 MemoryStore / AuthStore 的：
  概览和会话列表要 join users × sessions × turns 三张表，而 users 归
  AuthStore、后两张归 MemoryStore。借其中一个的私有连接做 join，等于让
  admin 依赖两个类的内部实现。WAL 模式下多开一个只读连接代价极小，
  换取的是「这个文件只和表结构耦合，不和那两个类的实现耦合」。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
import time
from typing import Any

from .auth import AuthStore
from .memory import MemoryStore


def _today_start() -> float:
    """本地时区的今天零点。用 UTC 会让「今日提问」在晚上八点后错位。"""
    d = _dt.date.today()
    return _dt.datetime.combine(d, _dt.time.min).timestamp()


# 告警阈值。写死在这儿而不是 config：它们不是部署参数，是「什么算异常」
# 的判断，改动应该伴随一次讨论，不该被某个环境变量悄悄调掉。
ALERT_VIEW_SESSIONS = 8     # 1 小时内查看多少个不同会话算批量翻
ALERT_FAIL_IP = 6           # 10 分钟内同一 IP 失败几次算撞库


class AdminService:
    def __init__(self, auth: AuthStore, mem: MemoryStore):
        self.auth = auth
        self.mem = mem
        self._conn = sqlite3.connect(str(mem.path), check_same_thread=False,
                                     timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 告警
    def alerts(self) -> list[dict[str, Any]]:
        """三条只读规则，全部是「已经发生、值得立刻知道」的事。

        为什么规则写在这里而不是定时任务里：
          这个项目没有常驻调度器，也不想为一个演示站引入一个。每次打开
          概览页现算，代价是三次带索引的聚合查询（毫秒级），换来的是
          「不需要后台进程也能看见异常」。
        """
        now = time.time()
        out: list[dict[str, Any]] = []

        with self._lock:
            # 1) 批量翻会话：单人在 1 小时内看了很多个不同会话的正文
            row = self._conn.execute(
                "SELECT admin_name, COUNT(DISTINCT target_id) AS c "
                "FROM admin_audit WHERE action='view_session' AND ts>=? "
                "GROUP BY admin_id ORDER BY c DESC LIMIT 1",
                (now - 3600,)).fetchone()
            if row and int(row["c"]) >= ALERT_VIEW_SESSIONS:
                out.append({
                    "level": "warn",
                    "title": "短时间批量查看会话",
                    "detail": f"{row['admin_name']} 在过去 1 小时打开了 "
                              f"{row['c']} 个会话正文",
                    "count": int(row["c"]),
                })

            # 2) 撞库：同一 IP 10 分钟内失败多次
            row = self._conn.execute(
                "SELECT ip, COUNT(*) AS c FROM login_log "
                "WHERE ok=0 AND ts>=? AND ip<>'' GROUP BY ip "
                "HAVING c>=? ORDER BY c DESC LIMIT 1",
                (now - 600, ALERT_FAIL_IP)).fetchone()
            if row:
                out.append({
                    "level": "bad",
                    "title": "同一 IP 连续登录失败",
                    "detail": f"{row['ip']} 在 10 分钟内失败 {row['c']} 次",
                    "count": int(row["c"]),
                })

            # 3) 正在锁定中的账号（密码连错触发的 15 分钟锁）
            rows = self._conn.execute(
                "SELECT username, locked_until FROM users "
                "WHERE locked_until > ? ORDER BY locked_until DESC",
                (now,)).fetchall()
            if rows:
                names = "、".join(r["username"] for r in rows[:5])
                out.append({
                    "level": "info",
                    "title": "有账号处于登录锁定",
                    "detail": f"{len(rows)} 个：{names}"
                              + ("…" if len(rows) > 5 else ""),
                    "count": len(rows),
                })

        return out

    # ------------------------------------------------------------ 概览
    def overview(self) -> dict[str, Any]:
        day0 = _today_start()
        users = self.auth.count_users()
        today = self.mem.turn_stats(day0)
        week = self.mem.turn_stats(day0 - 6 * 86400)
        total = self.mem.turn_stats(0)

        with self._lock:
            s_total = int(self._conn.execute(
                "SELECT COUNT(*) c FROM sessions").fetchone()["c"])
            s_today = int(self._conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE created_at>=?",
                (day0,)).fetchone()["c"])
            ownerless = int(self._conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE user_id IS NULL"
            ).fetchone()["c"])

        return {
            "users": users,
            "sessions": {"total": s_total, "today": s_today,
                         "ownerless": ownerless},
            "today": today,
            "week": week,
            "total": total,
            "by_day": self.mem.questions_by_day(7),
            # 告警跟概览一起下发：别人看后台的第一眼就是概览，
            # 让他再多点一次才看到异常，等于没做告警。
            "alerts": self.alerts(),
            # 反馈同理。用近 7 天窗口：全量会把三个月前的差评和今天的混在
            # 一起，看不出「最近是不是变差了」。
            "feedback": self.mem.feedback_stats(day0 - 6 * 86400, recent=6),
        }

    # ------------------------------------------------------------ 会话
    def sessions(self, q: str = "", user_id: int | None = None,
                 page: int = 1, size: int = 20,
                 owner: str = "any") -> dict[str, Any]:
        """owner: any 全部 / owned 有归属 / orphan 无主。

        无主会话是 v20 加账号体系之前留下的（那时候 sessions 没有 user_id）。
        它们不属于任何人，谁的历史里都看不到，但管理员分页的第一页会被它们
        占满。

        接口不做默认过滤（默认 any，后端不替调用方决定）；前端默认选「仅已归属」，
        同时在计数里把无主的数量摆出来。**隐藏一部分数据的前提是那部分的数量
        必须可见**，否则就成了悄悄丢东西。
        """
        where = ["1=1"]
        args: list[Any] = []
        if user_id is not None:
            where.append("s.user_id=?")
            args.append(int(user_id))
        if owner == "owned":
            where.append("s.user_id IS NOT NULL")
        elif owner == "orphan":
            where.append("s.user_id IS NULL")
        if q:
            # 搜「会话第一句提问」而不是搜全部消息：管理员能记住的是用户
            # 一开始问的那件事，不是第三轮的补充。
            where.append(
                "(COALESCE(u.username,'') LIKE ? OR s.session_id LIKE ?"
                " OR EXISTS(SELECT 1 FROM messages m WHERE m.session_id=s.session_id"
                " AND m.role='user' AND m.content LIKE ?))")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        w = " AND ".join(where)
        page = max(1, int(page or 1))
        size = min(100, max(1, int(size or 20)))
        base = ("FROM sessions s LEFT JOIN users u ON u.id=s.user_id"
                f" WHERE {w}")
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) c {base}", args).fetchone()["c"])
            # 三个总量（不受 owner 筛选影响，只跟搜索词有关），用来在界面上
            # 显示「你看到 2 个，另有 84 个在别的筛选里」
            counts = self._session_counts(q)
            rows = self._conn.execute(
                "SELECT s.session_id, s.created_at, s.updated_at, s.user_id,"
                " COALESCE(u.username,'（未归属）') AS username,"
                " (SELECT content FROM messages m WHERE m.session_id=s.session_id"
                "  AND m.role='user' ORDER BY m.id LIMIT 1) AS title,"
                " (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id"
                "  AND m.role='user') AS n_turns,"
                " (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id)"
                "  AS n_logged"
                f" {base} ORDER BY s.updated_at DESC LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size]).fetchall()
        return {"total": total, "page": page, "size": size,
                "counts": counts, "items": [dict(r) for r in rows]}

    def _session_counts(self, q: str = "") -> dict[str, int]:
        """三个归属类型的总量。只受搜索词影响，不受 owner 筛选影响。

        单独查三次而不是一次 GROUP BY：这个表的行数是百级，三次 COUNT
        走索引都是常数时间；写成一条带 CASE 的聚合反而更难读。
        """
        base = ["FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE 1=1"]
        args: list[Any] = []
        if q:
            base.append("AND (COALESCE(u.username,'') LIKE ? OR s.session_id LIKE ?"
                        " OR EXISTS(SELECT 1 FROM messages m"
                        " WHERE m.session_id=s.session_id AND m.role='user'"
                        " AND m.content LIKE ?))")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        head = " ".join(base)
        out = {}
        for key, extra in (("any", ""),
                           ("owned", " AND s.user_id IS NOT NULL"),
                           ("orphan", " AND s.user_id IS NULL")):
            out[key] = int(self._conn.execute(
                f"SELECT COUNT(*) c {head}{extra}", args).fetchone()["c"])
        return out

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._conn.execute(
                "SELECT s.*, COALESCE(u.username,'（未归属）') AS username"
                " FROM sessions s LEFT JOIN users u ON u.id=s.user_id"
                " WHERE s.session_id=?", (session_id,)).fetchone()
        if s is None:
            return None
        with self._lock:
            turns = [dict(r) for r in self._conn.execute(
                "SELECT question, status, steps, elapsed_ms, n_cites,"
                " n_unverified, ts FROM turns WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()]
        return {
            "session_id": s["session_id"],
            "user_id": s["user_id"],
            "username": s["username"],
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
            "messages": self.mem.session_messages(session_id, limit=400),
            "turns": turns,
            "slots": self.mem.load_slots(session_id).known(),
            "traces": self.mem.traces(session_id, limit=60),
        }

    # ------------------------------------------------------------ 用户
    def user_detail(self, user_id: int) -> dict[str, Any] | None:
        u = self.auth.get_user(user_id)
        if not u:
            return None
        with self._lock:
            n_sessions = int(self._conn.execute(
                "SELECT COUNT(*) c FROM sessions WHERE user_id=?",
                (user_id,)).fetchone()["c"])
            n_turns = int(self._conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE user_id=?",
                (user_id,)).fetchone()["c"])
            last = self._conn.execute(
                "SELECT question, status, ts FROM turns WHERE user_id=?"
                " ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        u = dict(u)
        u["n_sessions"] = n_sessions
        u["n_turns"] = n_turns
        u["last_question"] = dict(last) if last else None
        u["sessions"] = self.mem.user_sessions(user_id, limit=30)
        # 个人档案：**一律脱敏**（mask_id 只给前 6 后 4）。
        # 后台是审计视角，看「这人填了没有、单位写的谁」就够了，
        # 完整身份证号只有一个合法去处——用户自己在设置里看。
        # 这里多一个全号字段，迟早会被某个导出脚本顺手打进 CSV。
        u["profile"] = self.auth.get_profile(user_id, mask=True)
        u["n_tasks"] = int(self._conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND status='active'",
            (user_id,)).fetchone()["c"])
        return u

    # ------------------------------------------------------------ 近期问答
    def recent_turns(self, status: str = "", limit: int = 30) -> list[dict[str, Any]]:
        where, args = "1=1", []
        if status in ("answered", "refused", "failed"):
            where = "t.status=?"
            args.append(status)
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.*, COALESCE(u.username,'（未归属）') AS username"
                " FROM turns t LEFT JOIN users u ON u.id=t.user_id"
                f" WHERE {where} ORDER BY t.id DESC LIMIT ?",
                args + [min(200, int(limit))]).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
