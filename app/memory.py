# -*- coding: utf-8 -*-
"""
记忆层。基础版用 SQLite 单文件，零服务、零配置。

三层记忆的落地：
  工作记忆  在 AgentState.scratchpad 里，只活在一次请求内
  会话记忆  messages + case_slots 两张表，可跨天续聊
  长期记忆  user_facts 表，跨会话（这一版还没用上，留好了位置）

关于线程：Web 模式下 AgentRunner 跑在 worker 线程里，而连接是在主线程
创建的。sqlite3 默认禁止跨线程使用，所以这里 check_same_thread=False
再加一把锁串行化访问。数据量小，锁的代价可以忽略；等换 PostgreSQL
时把锁去掉即可。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .dbutil import connect as _db_connect
from .schemas import CaseSlots, Step

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    profile_id TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_sid ON messages(session_id, id);
-- 任务 = 一个案子。为什么要有这一层：一个劳动争议往往要问十几轮，
-- 中途关掉页面再回来，如果每次都是新会话，案情要素和前面说过的事实
-- 全断。任务把多次会话绑在一起，同时保证不同案子之间不串。
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_uid ON tasks(user_id, updated_at);
-- 任务级案情要素：同一任务下多个会话合并后的结果。
-- 为什么不直接改 case_slots 的主键：「要素按会话存」是已有数据的形状，
-- 改主键要迁移全部历史；保留它、再叠一层任务级合并，旧数据零风险。
CREATE TABLE IF NOT EXISTS task_slots (
    task_id    TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS case_slots (
    session_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- v138：接入内核的案情要素（消费/合同/道交/婚家各一套字段）。
-- 为什么不加进 case_slots：那张表存的是 `CaseSlots` 的 JSON，读回来要过
-- pydantic 校验，字段名不在那八项里就会被静默丢掉——「静默丢」正是这个项目
-- 里最难查的一类故障。分开一张表，两套字段互不干扰，老数据零迁移。
-- 主键带 profile_id：同一个会话理论上不会换内核（服务端不可变），但把内核
-- 写进主键之后，「万一换了」也只是各存各的，不会把消费的「商品」读成道交的。
CREATE TABLE IF NOT EXISTS domain_slots (
    session_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, profile_id)
);
-- 一个案子里查过的条文。为什么值得单独存：
--   同一件事往往要问三四轮（先问标准、再问金额、最后问时效），每轮都从
--   零检索一遍，等于每轮都重新踩一次「这次能不能命中」的运气。存下来之后
--   下一轮直接带着上一轮的证据开工，模型看到「这些已经查过」，就不会重复查。
-- 只存 id 和正文，不存打分——分数是「那一次提问」的产物，换个问题就没意义了。
CREATE TABLE IF NOT EXISTS task_evidence (
    task_id    TEXT NOT NULL,
    article_id TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, article_id)
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    summary TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    error TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_sid ON traces(session_id, id);
-- 每次问答的结果元数据。为什么不在 messages 上打标记而要单独一张表：
-- messages 是「对话内容」，一条问一条答，拒答和追问都只有一条 assistant
-- 记录，从里面反推「这轮是答了还是拒了、引了几条、自检过没过」要去数
-- traces 猜。管理端的概览和审计要的是**一问一行**，独立成表才不用猜。
CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    user_id       INTEGER,
    question      TEXT NOT NULL,
    status        TEXT NOT NULL,
    steps         INTEGER NOT NULL DEFAULT 0,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0,
    n_cites       INTEGER NOT NULL DEFAULT 0,
    n_unverified  INTEGER NOT NULL DEFAULT 0,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turn_uid ON turns(user_id, ts);
-- 用户对答案的显式评价。为什么值得单开一张表：
--   到目前为止所有评测都是自己造的题集，没有一条**真实用户信号**。
--   「这答案有没有用」是唯一能自动积累 badcase 的入口。
-- 唯一键用 (session_id, question) 而不是 turn_id：历史会话重放出来的答案
-- 没有 turn_id（那是流式过程中才产生的），但它在界面上和刚答完的完全一样，
-- 用户照样会点赞/点踩。turn_id 只作为「有就记下」的补充信息。
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question   TEXT NOT NULL,
    user_id    INTEGER,
    turn_id    INTEGER,
    value      TEXT NOT NULL,
    answer     TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    UNIQUE(session_id, question)
);
CREATE INDEX IF NOT EXISTS idx_fb_value ON feedback(value, ts);

-- v117 长期记忆：用户**显式**选过的立场与表达偏好。
-- 只存用户点过的，绝不存从语气推断出来的东西 —— 语气识别只是建议，
-- 不是关于这个人的事实；一旦落库，下次会话就会被当成「已知身份」。
-- 这张表在 v117 之前只在模块 docstring 里留了个位置，DDL 里并没有。
CREATE TABLE IF NOT EXISTS user_facts (
    user_id     INTEGER PRIMARY KEY,
    stance      TEXT,
    tone_mode   TEXT,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

-- v137 用户说过的事实（城市 / 职业 / 单位性质）。
-- 与 user_facts 分开：那张表存的是**用户选的**立场与表达偏好（显式动作），
-- 这张表存的是**用户说的话里明确出现过**的事实（可删）。混在一张表里，
-- 以后分不清哪条是用户选的、哪条是系统记的，删除语义就乱了。
-- source 存原句片段：任何一条都要能指回用户自己说的话，指不回的不记。
CREATE TABLE IF NOT EXISTS user_profile_facts (
    user_id     INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- v124 案件级附件开关：一个任务对应一份程序时间表、一份文书骨架。
-- 只存「用户有没有打开」，不存内容 —— 内容随案情与语料实时重算，
-- 存下来就会过期。两个开关默认 0（关），用户自己决定要不要展开。
-- 必须在 delete_task 里一起清掉，否则删任务会留下孤儿行。
CREATE TABLE IF NOT EXISTS task_attachments (
    task_id     TEXT PRIMARY KEY,
    procedure   INTEGER NOT NULL DEFAULT 0,
    document    INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);
"""


def _snippet(text: str, needle: str, pad: int = 28) -> str:
    """截一段带上下文的文字。会话搜索的结果里只给「命中的那几个字」，
    用户看不出上下文，等于让他自己再点进去找一遍。"""
    t = (text or "").replace("\n", " ").strip()
    low = t.lower()
    i = low.find(needle.lower())
    if i < 0:
        return t[:pad * 2] + ("…" if len(t) > pad * 2 else "")
    a = max(0, i - pad)
    b = min(len(t), i + len(needle) + pad)
    return ("…" if a else "") + t[a:b] + ("…" if b < len(t) else "")


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL：这个库现在有两个连接（MemoryStore 和 AuthStore）在写，
        # 默认的回滚日志模式下并发写会直接 database is locked。
        # 建连与切换都在 dbutil 里，含「文件刚创建时被杀毒扫描」的短暂重试。
        self._conn = _db_connect(self.path, timeout=10)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # sessions.user_id 是后加的。加账号体系之前的会话没有归属，
            # 保持 NULL 表示「未归属」——不做「默认归给第一个用户」这种猜测，
            # 那等于把别人的咨询记录塞给某个人。
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(sessions)")}
            if "user_id" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sess_uid ON sessions(user_id)")
            # 任务列同理：加任务功能之前产生的会话 task_id 为 NULL，
            # 前端把它们显示成「未分类」，不强行塞进某个新任务——
            # 把历史对话归到用户没建过的任务里，比看到「未分类」更让人困惑。
            if "task_id" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN task_id TEXT")
            # v119 内核列。老库里的会话 profile_id 为 NULL，读的时候回落
            # 劳动法——历史咨询本来就是劳动法，不「猜」一个新内核。
            if "profile_id" not in cols:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN profile_id TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sess_task ON sessions(task_id)")
            # v122 任务的内核列。任务是「一件纠纷」，纠纷本身有法域归属；
            # 老任务没有这一列，读的时候按它最近一次会话回落（_TASK_SQL 里
            # 的 COALESCE），再兜底劳动法——和 session_profile 同一口径。
            tcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(tasks)")}
            if "profile_id" not in tcols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN profile_id TEXT")
            # v137 任务级记忆三列：目标是「用户要干什么」的一句话，摘要是
            # 滚动压缩的案情，upto 记摘要覆盖到第几条 message（增量更新用）。
            # 老任务三列全空，读的时候当「还没生成」，不做任何猜测性回填。
            if "goal" not in tcols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN goal TEXT")
            if "summary" not in tcols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN summary TEXT")
            if "summary_upto" not in tcols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN summary_upto INTEGER")
            self._conn.commit()

    # ---------- 会话 ----------
    def ensure_session(self, session_id: str, user_id: int | None = None,
                       task_id: str | None = None,
                       profile_id: str | None = None) -> None:
        now = time.time()
        with self._lock:
            # 三件事同时做：刷新 updated_at、认领无主的老会话、挂到任务上。
            # COALESCE 的方向很关键——只在**原值为 NULL**时写入，已有归属
            # 绝不覆盖。否则任何人拿到一个旧 session_id 就能把它改成自己的。
            # task_id / profile_id 同理：会话一旦属于某个任务或内核，
            # 后续请求带别的值也不会把它挪走（前端状态错乱时这点能救数据）。
            self._conn.execute(
                "INSERT INTO sessions(session_id, created_at, updated_at,"
                " user_id, task_id, profile_id) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at, "
                "user_id=COALESCE(sessions.user_id, excluded.user_id), "
                "task_id=COALESCE(sessions.task_id, excluded.task_id), "
                "profile_id=COALESCE(sessions.profile_id, excluded.profile_id)",
                (session_id, now, now, user_id, task_id, profile_id))
            if task_id:
                # 有新消息就刷新任务的活跃时间，任务列表才能按「最近在办」排
                self._conn.execute("UPDATE tasks SET updated_at=? WHERE task_id=?",
                                   (now, task_id))
                # 任务第一次拿到会话时，把这个会话的内核记成任务的归属。
                # 只在任务自己还没内核时写，之后不问「这次是不是同一个法」——
                # 任务的法域是它被办起来那一刻定的，不该被一次误点改掉。
                if profile_id:
                    self._conn.execute(
                        "UPDATE tasks SET profile_id=? WHERE task_id=? "
                        "AND (profile_id IS NULL OR profile_id='')",
                        (profile_id, task_id))
            self._conn.commit()

    def session_profile(self, session_id: str) -> str:
        """会话绑定的内核 id。NULL / 拼错一律回落劳动法，不猜。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT profile_id FROM sessions WHERE session_id=?",
                (session_id,)).fetchone()
        from . import profiles as _profiles
        return _profiles.resolve(r["profile_id"] if r else None).id

    def set_session_profile(self, session_id: str, profile_id: str) -> None:
        """给新会话绑定内核。已有内核的会话不许改（防止中途串库）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET profile_id=? WHERE session_id=? "
                "AND (profile_id IS NULL OR profile_id='')",
                (profile_id, session_id))
            self._conn.commit()

    def session_owner(self, session_id: str) -> int | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT user_id FROM sessions WHERE session_id=?",
                (session_id,)).fetchone()
        if not r or r["user_id"] is None:
            return None
        return int(r["user_id"])

    def user_sessions(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """我的历史。标题取该会话的第一句提问——比「会话 3f2a」有用得多。

        task_id 一起带出来：前端要按任务分组显示，让它为每条再发一次请求
        既慢又容易在并发时对不上号。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.session_id, s.created_at, s.updated_at, s.task_id,"
                "  s.profile_id,"
                "  (SELECT content FROM messages m WHERE m.session_id=s.session_id"
                "   AND m.role='user' ORDER BY m.id LIMIT 1) AS title,"
                "  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id"
                "   AND m.role='user') AS n_turns"
                " FROM sessions s WHERE s.user_id=?"
                " ORDER BY s.updated_at DESC LIMIT ?",
                (user_id, min(200, int(limit)))).fetchall()
        return [{"session_id": r["session_id"], "title": r["title"] or "（空会话）",
                 "created_at": r["created_at"], "updated_at": r["updated_at"],
                 "task_id": r["task_id"] or None,
                 # v120：历史列表按内核打标。老会话 profile_id 为 NULL，
                 # 回落 labor 与 session_profile() 同一口径，不猜别的。
                 "profile_id": r["profile_id"] or "labor",
                 "n_turns": int(r["n_turns"])} for r in rows]

    def session_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, ts FROM messages WHERE session_id=?"
                " ORDER BY id LIMIT ?", (session_id, min(1000, int(limit)))).fetchall()
        return [dict(r) for r in rows]

    # ---------- 任务 ----------
    # 一条 SQL 一次算出「任务 + 会话数 + 轮数 + 最后活跃」。
    # 为什么不在 Python 里循环查：任务列表是首屏，N 个任务 N 次查询会
    # 随着使用天数变慢，而这种慢很难在开发机上复现。
    _TASK_SQL = (
        "SELECT t.task_id, t.title, t.status, t.created_at, t.updated_at,"
        " t.goal, t.summary, t.summary_upto,"
        # 任务的法域：任务自己的列优先，没有就取它最近一次会话的内核。
        # 为什么不用 t.profile_id 一个字段就完事：老库里的任务这一列是
        # 迁移出来的 NULL，而它们底下的会话早就记着内核了——按会话回填
        # 比让用户看到一排「劳动法」标签更诚实。
        " COALESCE(t.profile_id,"
        "   (SELECT s4.profile_id FROM sessions s4"
        "     WHERE s4.task_id=t.task_id AND s4.profile_id IS NOT NULL"
        "       AND s4.profile_id<>''"
        "     ORDER BY s4.updated_at DESC LIMIT 1)) AS profile_id,"
        " (SELECT COUNT(*) FROM sessions s WHERE s.task_id=t.task_id) AS n_sessions,"
        " (SELECT COUNT(*) FROM messages m"
        "   JOIN sessions s2 ON s2.session_id=m.session_id"
        "  WHERE s2.task_id=t.task_id AND m.role='user') AS n_turns,"
        " (SELECT MAX(m.ts) FROM messages m"
        "   JOIN sessions s3 ON s3.session_id=m.session_id"
        "  WHERE s3.task_id=t.task_id) AS last_msg"
        " FROM tasks t")

    @staticmethod
    def _task_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {"task_id": r["task_id"], "title": r["title"],
                "status": r["status"], "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                # 老任务无会话也无内核 → 劳动法，和历史列表同一口径。
                "profile_id": r["profile_id"] or "labor",
                "n_sessions": int(r["n_sessions"] or 0),
                "n_turns": int(r["n_turns"] or 0),
                "last_msg": r["last_msg"] or 0,
                # v137 任务记忆：三个都可能为空（老任务 / 还没生成）。
                "goal": r["goal"] or "",
                "summary": r["summary"] or "",
                "summary_upto": int(r["summary_upto"] or 0)}

    def create_task(self, user_id: int, title: str = "",
                    profile_id: str | None = None) -> dict[str, Any]:
        tid = uuid.uuid4().hex[:12]
        now = time.time()
        t = (title or "").strip()[:60] or "新任务"
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id, user_id, title, status,"
                " created_at, updated_at, profile_id)"
                " VALUES(?,?,?,'active',?,?,?)",
                (tid, int(user_id), t, now, now, profile_id))
            self._conn.commit()
        return self.get_task(tid, int(user_id)) or {}

    def get_task(self, task_id: str,
                 user_id: int | None = None) -> dict[str, Any] | None:
        sql = self._TASK_SQL + " WHERE t.task_id=?"
        args: list[Any] = [task_id]
        if user_id is not None:
            sql += " AND t.user_id=?"
            args.append(int(user_id))
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
        return self._task_dict(r) if r else None

    def list_tasks(self, user_id: int,
                   include_archived: bool = False) -> list[dict[str, Any]]:
        sql = self._TASK_SQL + " WHERE t.user_id=?"
        if not include_archived:
            sql += " AND t.status='active'"
        # 办理中的排在已结案前面。归档会刷新 updated_at，只按时间排的话
        # 刚归档的案子会跳到第一行——文书下拉默认选中的就是它，等于
        # 「打开文书，默认给你一个结案的案子」。
        sql += " ORDER BY (t.status='archived'), t.updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, (int(user_id),)).fetchall()
        return [self._task_dict(r) for r in rows]

    def task_owner(self, task_id: str) -> int | None:
        with self._lock:
            r = self._conn.execute("SELECT user_id FROM tasks WHERE task_id=?",
                                   (task_id,)).fetchone()
        return int(r["user_id"]) if r and r["user_id"] is not None else None

    def update_task(self, task_id: str, user_id: int, *,
                    title: str | None = None,
                    status: str | None = None) -> bool:
        sets: list[str] = []
        args: list[Any] = []
        if title is not None:
            sets.append("title=?")
            args.append((title or "").strip()[:60] or "新任务")
        if status is not None:
            # 只允许两态。不做硬删除：已经有对话和文书的案子被删掉，
            # 用户找不回来，而磁盘上那几十 KB 根本不值得省。
            if status not in ("active", "archived"):
                return False
            sets.append("status=?")
            args.append(status)
        if not sets:
            return False
        sets.append("updated_at=?")
        args.append(time.time())
        args += [task_id, int(user_id)]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=? AND user_id=?",
                args)
            self._conn.commit()
        return cur.rowcount > 0

    def task_sessions(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.session_id, s.created_at, s.updated_at,"
                "  (SELECT content FROM messages m WHERE m.session_id=s.session_id"
                "   AND m.role='user' ORDER BY m.id LIMIT 1) AS title,"
                "  (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id"
                "   AND m.role='user') AS n_turns"
                " FROM sessions s WHERE s.task_id=?"
                " ORDER BY s.updated_at DESC LIMIT ?",
                (task_id, min(200, int(limit)))).fetchall()
        return [{"session_id": r["session_id"], "title": r["title"] or "（空会话）",
                 "created_at": r["created_at"], "updated_at": r["updated_at"],
                 "n_turns": int(r["n_turns"])} for r in rows]

    def session_task(self, session_id: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT task_id FROM sessions WHERE session_id=?",
                                   (session_id,)).fetchone()
        return (r["task_id"] or None) if r else None

    # ---------- v137 任务级记忆 ----------
    def task_memory(self, task_id: str) -> dict[str, Any]:
        """读任务记忆。读不到就是三个空值，不抛异常——调用方都在答案链路
        的尾巴上，这里一旦抛错就会把已经生成好的答案弄丢。"""
        if not task_id:
            return {"goal": "", "summary": "", "summary_upto": 0}
        with self._lock:
            r = self._conn.execute(
                "SELECT goal, summary, summary_upto FROM tasks WHERE task_id=?",
                (task_id,)).fetchone()
        if not r:
            return {"goal": "", "summary": "", "summary_upto": 0}
        return {"goal": r["goal"] or "", "summary": r["summary"] or "",
                "summary_upto": int(r["summary_upto"] or 0)}

    def set_task_memory(self, task_id: str, *,
                        goal: str | None = None,
                        summary: str | None = None,
                        summary_upto: int | None = None) -> None:
        """增量更新：只写传进来的字段，None 表示「这次不动」。

        为什么不做成 upsert 整行：goal 只在第一次生成，摘要每 4 轮才动，
        两个更新时机不同；整行覆盖会让先写的那一个被空值打回去。
        """
        if not task_id:
            return
        sets: list[str] = []
        args: list[Any] = []
        if goal is not None:
            sets.append("goal=?")
            args.append(str(goal)[:200])
        if summary is not None:
            sets.append("summary=?")
            args.append(str(summary)[:1200])
        if summary_upto is not None:
            sets.append("summary_upto=?")
            args.append(int(summary_upto))
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(time.time())
        args.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", args)
            self._conn.commit()

    def recent_messages(self, session_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """最近的原文消息（旧的在前），给任务摘要与上下文注入用。"""
        return self.recent(session_id, limit)

    def delete_session(self, session_id: str, user_id: int | None = None) -> bool:
        """级联删除一个会话（含全部子表），不留孤儿行。
        user_id 给定时校验归属，防止越权删别人的会话。"""
        if user_id is not None and self.session_owner(session_id) != int(user_id):
            return False
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM traces WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM feedback WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM case_slots WHERE session_id=?", (session_id,))
            # v138：领域要素也按会话存，漏这一条删完会话会留下读不到的行。
            self._conn.execute("DELETE FROM domain_slots WHERE session_id=?",
                               (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            self._conn.commit()
        return True

    def delete_task(self, task_id: str, user_id: int) -> bool:
        """删除一个任务及其下所有会话（级联）。校验归属。"""
        if self.task_owner(task_id) != int(user_id):
            return False
        with self._lock:
            sids = [r[0] for r in self._conn.execute(
                "SELECT session_id FROM sessions WHERE task_id=?", (task_id,)).fetchall()]
            for sid in sids:
                self._conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM traces WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM feedback WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM case_slots WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM domain_slots WHERE session_id=?", (sid,))
            self._conn.execute("DELETE FROM sessions WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM task_slots WHERE task_id=?", (task_id,))
            self._conn.execute("DELETE FROM task_evidence WHERE task_id=?", (task_id,))
            # v124：附件开关也挂在任务上，漏这条会留孤儿行。
            self._conn.execute("DELETE FROM task_attachments WHERE task_id=?",
                               (task_id,))
            self._conn.execute(
                "DELETE FROM tasks WHERE task_id=? AND user_id=?",
                (task_id, int(user_id)))
            self._conn.commit()
        return True

    # ---------------------------------------------------------- v124 附件开关
    def get_attachment_flags(self, task_id: str) -> dict[str, bool]:
        """读某任务的两个开关。没写过就是全关 —— 不在读的时候偷偷建行。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT procedure, document FROM task_attachments WHERE task_id=?",
                (task_id,)).fetchone()
        return {"procedure": bool(r["procedure"]) if r else False,
                "document": bool(r["document"]) if r else False}

    def set_attachment_flags(self, task_id: str, user_id: int,
                             patch: dict[str, Any]) -> dict[str, bool] | None:
        """只改传进来的那一个开关，另一个保持原值。校验归属（越权返回 None）。"""
        if self.task_owner(task_id) != int(user_id):
            return None
        cur = self.get_attachment_flags(task_id)
        proc = bool(patch.get("procedure", cur["procedure"]))
        doc = bool(patch.get("document", cur["document"]))
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_attachments(task_id, procedure, document, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "procedure=excluded.procedure, document=excluded.document, "
                "updated_at=excluded.updated_at",
                (task_id, int(proc), int(doc), time.time()))
            self._conn.commit()
        return {"procedure": proc, "document": doc}

    def assign_loose_to_task(self, user_id: int,
                             title: str = "历史咨询") -> dict[str, Any] | None:
        """把该用户所有未归属（task_id 为空）的会话**按内核分桶**收进任务。

        v123 起 /api/chat 会给每次新咨询自动建任务，正常路径上不再产生无主
        会话，这里只剩老库的存量数据。分桶必须按内核：原来全塞进同一个
        「历史咨询」，先问食安再问劳动法就变成同一个任务，而任务的徽章还会
        跟着最后一条会话变色——用户看到的就是「上一个任务被覆盖了」。

        劳动法那条仍叫「历史咨询」（老库里绝大多数都是它，也和既有文档、
        既有截图对得上），其余内核叫「历史咨询 · 内核名」，一眼能看出是哪部法。

        已归属的会话不会被这次写入改走（UPDATE 带 task_id IS NULL 条件）。
        """
        uid = int(user_id)
        from . import profiles as _profiles
        last: str | None = None
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, profile_id FROM sessions"
                " WHERE user_id=? AND (task_id IS NULL OR task_id='')"
                " ORDER BY updated_at", (uid,)).fetchall()
            if not rows:
                return None
            groups: dict[str, list[str]] = {}
            for r in rows:
                pid = _profiles.resolve(r["profile_id"]).id
                groups.setdefault(pid, []).append(r["session_id"])
            for pid, sids in groups.items():
                name = (title if pid == "labor"
                        else f"{title} · {_profiles.resolve(pid).name}")
                r2 = self._conn.execute(
                    "SELECT task_id FROM tasks WHERE user_id=? AND title=?",
                    (uid, name)).fetchone()
                tid = r2["task_id"] if r2 else uuid.uuid4().hex[:12]
                if not r2:
                    now = time.time()
                    # 桶自己的法域写死在任务上：不写的话 _TASK_SQL 会按
                    # 「任务下最近一次会话」回落，桶的徽章每来一个新内核的
                    # 会话就换个颜色。
                    self._conn.execute(
                        "INSERT INTO tasks(task_id, user_id, title, status,"
                        " created_at, updated_at, profile_id)"
                        " VALUES(?,?,?,'active',?,?,?)",
                        (tid, uid, name, now, now, pid))
                for sid in sids:
                    self._conn.execute(
                        "UPDATE sessions SET task_id=? WHERE session_id=? "
                        "AND (task_id IS NULL OR task_id='')", (tid, sid))
                last = tid
            self._conn.commit()
        return self.get_task(last, uid) if last else None

    def task_messages(self, task_id: str,
                      limit: int = 400) -> list[dict[str, Any]]:
        """该任务下所有会话的消息，按时间正序。生成申请书时用。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.role, m.content, m.ts FROM messages m"
                " JOIN sessions s ON s.session_id=m.session_id"
                " WHERE s.task_id=? ORDER BY m.ts, m.id LIMIT ?",
                (task_id, min(2000, int(limit)))).fetchall()
        return [dict(r) for r in rows]

    def load_task_slots(self, task_id: str) -> CaseSlots:
        with self._lock:
            r = self._conn.execute("SELECT data FROM task_slots WHERE task_id=?",
                                   (task_id,)).fetchone()
        if not r:
            return CaseSlots()
        try:
            return CaseSlots(**json.loads(r["data"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return CaseSlots()

    def merge_task_slots(self, task_id: str, slots: CaseSlots) -> list[str]:
        """把会话级槽位合并进任务级：非空覆盖空，冲突时新值胜。

        为什么新值胜：用户后面纠正过的信息（「记错了，是 8 月入职」）
        必须能盖掉前面说错的。反过来（旧值优先）会让纠正在界面上
        「不生效」，用户会以为系统坏了。清空某字段不会传播——那是
        merge 的语义，要真删得去案情档案里手工改。
        """
        cur = self.load_task_slots(task_id)
        changed = cur.merge(slots.known())
        if changed:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO task_slots(task_id, data, updated_at)"
                    " VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET"
                    " data=excluded.data, updated_at=excluded.updated_at",
                    (task_id,
                     json.dumps(cur.model_dump(), ensure_ascii=False),
                     time.time()))
                self._conn.commit()
        return changed

    # ---------- 任务级证据 ----------
    def save_task_evidence(self, task_id: str, items: list[dict[str, Any]]) -> int:
        """把这一轮用到的条文记到任务上。items 是 Evidence.model_dump()。

        只增不改：同一条文再查到一次就刷新正文与时间，不新增行。
        不存 score —— 分数是「那一次提问」的产物，换个问题就没有可比性。
        """
        if not task_id or not items:
            return 0
        now = time.time()
        n = 0
        with self._lock:
            for it in items:
                aid = (it or {}).get("article_id")
                if not aid:
                    continue
                self._conn.execute(
                    "INSERT INTO task_evidence(task_id, article_id, data, updated_at)"
                    " VALUES(?,?,?,?) ON CONFLICT(task_id, article_id) DO UPDATE SET"
                    " data=excluded.data, updated_at=excluded.updated_at",
                    (task_id, aid, json.dumps(it, ensure_ascii=False), now))
                n += 1
            self._conn.commit()
        return n

    def load_task_evidence(self, task_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """取这个案子上次用过的条文，最近用过的优先。

        limit 卡在 8：这是「上一轮查过什么」的提示，不是本次的证据池。
        把整张表搬进来会顶到 _MAX_EVIDENCE，本次该查的新条文就没机会查了。
        """
        if not task_id:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM task_evidence WHERE task_id=?"
                " ORDER BY updated_at DESC LIMIT ?",
                (task_id, max(1, int(limit)))).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    # ---------- 问答元数据 ----------
    def save_turn(self, session_id: str, user_id: int | None, question: str,
                  status: str, steps: int = 0, elapsed_ms: int = 0,
                  n_cites: int = 0, n_unverified: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO turns(session_id, user_id, question, status, steps,"
                " elapsed_ms, n_cites, n_unverified, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, user_id, (question or "")[:1000], status,
                 int(steps), int(elapsed_ms), int(n_cites), int(n_unverified),
                 time.time()))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def turn_stats(self, since: float = 0.0) -> dict[str, Any]:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) n,"
                " COALESCE(SUM(status='answered'),0) answered,"
                " COALESCE(SUM(status='refused'),0) refused,"
                " COALESCE(SUM(status='failed'),0) failed,"
                " COALESCE(AVG(elapsed_ms),0) avg_ms,"
                " COALESCE(AVG(steps),0) avg_steps,"
                " COALESCE(SUM(n_unverified>0),0) unverified,"
                " COALESCE(AVG(n_cites),0) avg_cites"
                " FROM turns WHERE ts>=?", (since,)).fetchone()
        n = int(r["n"])
        return {"turns": n, "answered": int(r["answered"]),
                "refused": int(r["refused"]), "failed": int(r["failed"]),
                "avg_ms": int(r["avg_ms"]), "avg_steps": round(float(r["avg_steps"]), 2),
                "unverified_turns": int(r["unverified"]),
                "avg_cites": round(float(r["avg_cites"]), 2),
                "refuse_rate": round(int(r["refused"]) / n, 4) if n else 0.0,
                "unverified_rate": round(int(r["unverified"]) / n, 4) if n else 0.0}

    # ---------- 会话搜索 ----------
    def search_sessions(self, user_id: int, q: str,
                        limit: int = 20) -> list[dict[str, Any]]:
        """在**自己的**会话里搜正文。

        为什么不上 FTS5：86 个会话、几万条消息，LIKE 全表扫一次是毫秒级。
        引入 FTS5 要建虚表 + 触发器同步，改错一次就静默丢索引，代价远大于收益。
        等真到几十万条再说，那时也应当换存储而不是加索引。

        **必须按 user_id 过滤**：这是搜索，不是越权入口。少这一句，
        用户就能靠关键词把别人的咨询记录一条条试出来。
        """
        q = (q or "").strip()[:80]
        if not q:
            return []
        # 用户输入里的 % 和 _ 是通配符，不转义的话搜「100%」会变成搜「100 任意」
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.session_id, m.role, m.content, m.ts FROM messages m"
                " JOIN sessions s ON s.session_id = m.session_id"
                " WHERE s.user_id = ? AND m.content LIKE ? ESCAPE '\\'"
                " ORDER BY m.ts DESC LIMIT 400",
                (user_id, like)).fetchall()
            hits: dict[str, dict[str, Any]] = {}
            for r in rows:
                sid = r["session_id"]
                if sid in hits:
                    hits[sid]["hits"] += 1
                    continue
                hits[sid] = {"session_id": sid,
                             "snippet": _snippet(r["content"], q),
                             "role": r["role"], "ts": r["ts"], "hits": 1}
            sids = list(hits)[:max(1, int(limit))]
            titles: dict[str, str] = {}
            if sids:
                ph = ",".join("?" * len(sids))
                for t in self._conn.execute(
                        f"SELECT session_id, content FROM messages WHERE"
                        f" session_id IN ({ph}) AND role='user' AND id IN"
                        f" (SELECT MIN(id) FROM messages WHERE session_id IN ({ph})"
                        "  AND role='user' GROUP BY session_id)",
                        sids + sids).fetchall():
                    titles[t["session_id"]] = t["content"]
        out = []
        for sid in sids:
            h = dict(hits[sid])
            h["title"] = titles.get(sid, "")
            out.append(h)
        return out

    # ---------- 用户反馈 ----------
    def save_feedback(self, session_id: str, question: str,
                      user_id: int | None, turn_id: int | None,
                      value: str, answer: str = "") -> None:
        """value 传空串表示撤销。一个人对同一问只能有一个态度。"""
        q = (question or "")[:1000]
        with self._lock:
            if not value:
                self._conn.execute(
                    "DELETE FROM feedback WHERE session_id=? AND question=?",
                    (session_id, q))
            else:
                # 答案截断存：badcase 池要能直接看「他嫌哪句不对」，
                # 但整篇存进去会让这张表迅速膨胀，而它只需要定位问题。
                self._conn.execute(
                    "INSERT INTO feedback(session_id, question, user_id,"
                    " turn_id, value, answer, ts) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(session_id, question) DO UPDATE SET"
                    " value=excluded.value, answer=excluded.answer,"
                    " ts=excluded.ts,"
                    " turn_id=COALESCE(excluded.turn_id, feedback.turn_id)",
                    (session_id, q, user_id, turn_id, value,
                     (answer or "")[:4000], time.time()))
            self._conn.commit()

    def session_votes(self, session_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT question, value FROM feedback WHERE session_id=?",
                (session_id,)).fetchall()
        return {r["question"]: r["value"] for r in rows}

    def feedback_stats(self, since: float = 0.0, recent: int = 6) -> dict[str, Any]:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(value='up'),0) up,"
                " COALESCE(SUM(value='down'),0) down FROM feedback"
                " WHERE ts>=?", (since,)).fetchone()
            bad = self._conn.execute(
                "SELECT session_id, question, answer, ts FROM feedback"
                " WHERE value='down' AND ts>=? ORDER BY ts DESC LIMIT ?",
                (since, int(recent))).fetchall()
        n, up, down = int(r["n"]), int(r["up"]), int(r["down"])
        return {"n": n, "up": up, "down": down,
                "up_rate": round(up / n, 4) if n else 0.0,
                "recent_bad": [dict(x) for x in bad]}

    def questions_by_day(self, days: int = 7) -> list[dict[str, Any]]:
        days = max(1, min(90, int(days)))
        since = time.time() - days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d,"
                " COUNT(*) n, COALESCE(SUM(status='refused'),0) refused"
                " FROM turns WHERE ts>=? GROUP BY d ORDER BY d",
                (since,)).fetchall()
        return [{"day": r["d"], "n": int(r["n"]), "refused": int(r["refused"])}
                for r in rows]

    def save_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(session_id, role, content, ts) VALUES(?,?,?,?)",
                (session_id, role, content, time.time()))
            self._conn.commit()

    # ---------- 长期记忆：显式画像（v117）----------
    def get_user_facts(self, user_id: int | None) -> dict[str, Any]:
        """读用户点过的立场与表达偏好；没有就返回空 dict。

        命令行 / 评测脚本传 user_id=None，不走账号体系，直接返回空。
        """
        if user_id is None:
            return {}
        with self._lock:
            r = self._conn.execute(
                "SELECT stance, tone_mode, confirmed FROM user_facts "
                "WHERE user_id=?", (int(user_id),)).fetchone()
        return dict(r) if r else {}

    def save_user_facts(self, user_id: int, *, stance: str | None = None,
                        tone_mode: str | None = None,
                        confirmed: bool | None = None) -> dict[str, Any]:
        """只接受白名单值。非法值不报错、也不落库 —— 前端传错时
        宁可保持原样，也不要把「瞎编的立场」写进长期记忆。"""
        from .persona import normalize_stance
        st = normalize_stance(stance)
        tone = tone_mode if tone_mode in ("plain", "standard") else None
        with self._lock:
            old = self.get_user_facts(user_id)
            self._conn.execute(
                "INSERT INTO user_facts(user_id, stance, tone_mode, confirmed, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "stance=COALESCE(excluded.stance, user_facts.stance), "
                "tone_mode=COALESCE(excluded.tone_mode, user_facts.tone_mode), "
                "confirmed=excluded.confirmed, updated_at=excluded.updated_at",
                (int(user_id), st, tone,
                 int(bool(confirmed if confirmed is not None else old.get("confirmed"))),
                 time.time()))
            self._conn.commit()
        return self.get_user_facts(user_id)

    # ---------- v137 用户说过的事实（可删）----------
    def list_profile_facts(self, user_id: int | None) -> list[dict[str, str]]:
        """用户自己说过的事实。没登录就当没有，不去猜。"""
        if user_id is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, source FROM user_profile_facts "
                "WHERE user_id=? ORDER BY key", (int(user_id),)).fetchall()
        return [{"key": r["key"], "value": r["value"],
                 "source": r["source"] or ""} for r in rows]

    def add_profile_facts(self, user_id: int | None,
                          patch: dict[str, dict[str, str]]) -> list[str]:
        """只增不覆盖：用户后来说的同一项，若与旧值不同就覆盖——
        因为「我换工作了」是事实变更，不是矛盾。返回真正变动的 key。

        空的 value 视为用户主动清除（档案页的删除按钮走这里）。
        """
        if user_id is None or not patch:
            return []
        changed: list[str] = []
        with self._lock:
            for k, v in patch.items():
                val = str(v.get("value") or "").strip()[:40]
                src = str(v.get("source") or "").strip()[:80]
                if not val:
                    self._conn.execute(
                        "DELETE FROM user_profile_facts WHERE user_id=? AND key=?",
                        (int(user_id), k))
                    changed.append(k)
                    continue
                r = self._conn.execute(
                    "SELECT value FROM user_profile_facts WHERE user_id=? AND key=?",
                    (int(user_id), k)).fetchone()
                if r and r["value"] == val:
                    continue
                self._conn.execute(
                    "INSERT INTO user_profile_facts(user_id, key, value, source, updated_at)"
                    " VALUES(?,?,?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET"
                    " value=excluded.value, source=excluded.source,"
                    " updated_at=excluded.updated_at",
                    (int(user_id), k, val, src, time.time()))
                changed.append(k)
            self._conn.commit()
        return changed

    def recent(self, session_id: str, limit: int = 6) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def count_messages(self, session_id: str) -> int:
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) c FROM messages WHERE session_id=?",
                                   (session_id,)).fetchone()
        return int(r["c"])

    # ---------- 槽位 ----------
    def load_slots(self, session_id: str) -> CaseSlots:
        with self._lock:
            r = self._conn.execute("SELECT data FROM case_slots WHERE session_id=?",
                                   (session_id,)).fetchone()
        if not r:
            return CaseSlots()
        try:
            return CaseSlots(**json.loads(r["data"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return CaseSlots()

    def save_slots(self, session_id: str, slots: CaseSlots) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO case_slots(session_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, "
                "updated_at=excluded.updated_at",
                (session_id,
                 json.dumps(slots.model_dump(), ensure_ascii=False), time.time()))
            self._conn.commit()
        # 会话挂在任务上时，顺手把要素往任务级合并一次。
        # 放在这里而不是每次读取时现算：任务级要能被「生成申请书」
        # 直接读到，读取路径不该承担合并逻辑。
        tid = self.session_task(session_id)
        if tid:
            self.merge_task_slots(tid, slots)

    # ---------- 接入内核的案情要素（v138）----------
    def load_domain_slots(self, session_id: str, profile_id: str) -> dict[str, str]:
        """读本会话在本内核下记下的要素。

        读失败一律返回空 dict：这只是「多记得一点」，读不出来就当没记过，
        绝不能因为它让一次问答崩掉。脏数据（不是 dict、值不是标量）也丢掉。
        """
        with self._lock:
            r = self._conn.execute(
                "SELECT data FROM domain_slots WHERE session_id=? AND profile_id=?",
                (session_id, profile_id)).fetchone()
        if not r:
            return {}
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def save_domain_slots(self, session_id: str, profile_id: str,
                          data: dict[str, str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO domain_slots(session_id, profile_id, data, updated_at)"
                " VALUES(?,?,?,?) ON CONFLICT(session_id, profile_id) DO UPDATE SET"
                " data=excluded.data, updated_at=excluded.updated_at",
                (session_id, profile_id,
                 json.dumps(data, ensure_ascii=False), time.time()))
            self._conn.commit()

    def clear_domain_slots(self, session_id: str, profile_id: str) -> None:
        """删掉一个会话记下的领域要素。删会话时调一次，不然会留下孤儿行。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM domain_slots WHERE session_id=? AND profile_id=?",
                (session_id, profile_id))
            self._conn.commit()

    # ---------- 轨迹 ----------
    def save_trace(self, session_id: str, turn: int, step: Step) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO traces(session_id, turn, step_index, tool, args, summary,"
                " elapsed_ms, error, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, turn, step.index, step.tool,
                 json.dumps(step.args, ensure_ascii=False), step.summary,
                 step.elapsed_ms, step.error, time.time()))
            self._conn.commit()

    def traces(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM traces WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
