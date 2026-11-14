# -*- coding: utf-8 -*-
"""自主学习与个人主页（v139）—— 按 (user_id, pid) 全隔离。

一条总原则，和问答引擎一致：**模型管「怎么说」，事实只认库**。
  · 题目依附的原文、条号、引用全部由服务端从 `LawIndex.by_id` 写入，
    模型只负责把题面与解析写成中文。它输出的 cites 一律不采信——
    让模型自己报条号，等于把 v40「幻觉引用」那个洞重新挖一遍。
  · 判答：客观题规则判（100% 确定）；主观题才交给模型，且带原文去判。
  · **回放包（无 key）必须照常能学**：离线兜底只出四选一，规则判答，
    不出需要模型判的主观题。否则交付包里这一整块是死的。

进度口径：不用「条文数」当分母（民法典 1688 条 vs 道交法 4 条摘录，
进度条没法比），用 `章节覆盖率 × 0.7 + 章节内答对率 × 0.3`，
五个法典各自 0–100%，横向可比。章节 = (law_short, chapter_path)。

为什么单独一个 StudyStore 而不塞进 MemoryStore：学习数据和会话/任务
没有任何外键关系，混进 memory.py 会让「删任务级联清」多一层顾虑。

v140 三件大事，都只加不改：
  ① **卷子**：一次出 N 题（5/10/15/20 或自填 1–30）攒成一张卷，交卷时
     一次性汇总成评估报告。原来的「出 5 题逐题提交」**语义不动**
     （`new_quiz` 签名与返回不变），卷子只是它上面的一层壳，老断言照旧。
  ② **三种题型**：choice（四选一）/ judge（判断）/ blank（填空）。
     三种都能离线出：判断题 = 把 A 型题的「出自哪条」改成「是不是第 X 条」，
     填空题从原文里挖一个**孤立的词**当空，答案与位置全由服务端算，
     模型一个字节都不参与。填空判答走宽容匹配，见 `_blank_correct`。
  ③ **遗忘曲线**：错题带 next_review，错 1 次隔 1 天、2 次隔 3 天、
     3 次及以上隔 7 天；连续答对 2 次才自动标记掌握。界面上只有
     「今天到期」的才算待复习，不把攒了半年的旧错题一股脑推给用户。
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
from datetime import date, timedelta
from typing import Any

from . import profiles as _profiles
from .dbutil import connect as _db_connect
from .library import profile_for
from .llm import _loads_obj

_NO_CHAPTER = "（未分章）"
_LETTERS = "ABCD"
# 一次最多出 10 题（`new_quiz` 的老口径，不动）；卷子走 MAX_PAPER。
MAX_QUIZ = 10
# 一张卷最多 30 题。离线题不花 token，30 道也就几毫秒；在线题按每题
# 一次调用算，30 题在 DeepSeek 上约 4–6 分钟，前端用真实进度条告知。
MAX_PAPER = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_quizzes (
    quiz_id    TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    pid        TEXT NOT NULL,
    article_id TEXT NOT NULL,
    law_short  TEXT NOT NULL,
    chapter    TEXT NOT NULL,
    qtype      TEXT NOT NULL,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL DEFAULT '[]',
    answer     TEXT NOT NULL,
    explain    TEXT NOT NULL DEFAULT '',
    cites      TEXT NOT NULL DEFAULT '[]',
    source     TEXT NOT NULL DEFAULT 'offline',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quiz_up ON study_quizzes(user_id, pid);
CREATE TABLE IF NOT EXISTS study_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    pid        TEXT NOT NULL,
    quiz_id    TEXT NOT NULL,
    answer     TEXT NOT NULL,
    correct    INTEGER NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    feedback   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_up ON study_attempts(user_id, pid, created_at);
CREATE TABLE IF NOT EXISTS study_progress (
    user_id    INTEGER NOT NULL,
    pid        TEXT NOT NULL,
    article_id TEXT NOT NULL,
    chapter    TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0,
    correct    INTEGER NOT NULL DEFAULT 0,
    wrong      INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, pid, article_id)
);
CREATE TABLE IF NOT EXISTS study_wrongbook (
    user_id     INTEGER NOT NULL,
    pid         TEXT NOT NULL,
    quiz_id     TEXT NOT NULL,
    wrong_count INTEGER NOT NULL DEFAULT 0,
    mastered    INTEGER NOT NULL DEFAULT 0,
    last_wrong  REAL NOT NULL,
    PRIMARY KEY (user_id, pid, quiz_id)
);
CREATE TABLE IF NOT EXISTS study_papers (
    paper_id   TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    pid        TEXT NOT NULL,
    law_short  TEXT NOT NULL DEFAULT '',
    chapter    TEXT NOT NULL DEFAULT '',
    n          INTEGER NOT NULL,
    quiz_ids   TEXT NOT NULL DEFAULT '[]',
    started_at REAL NOT NULL,
    submit_at  REAL NOT NULL DEFAULT 0,
    score      INTEGER NOT NULL DEFAULT 0,
    right_n    INTEGER NOT NULL DEFAULT 0,
    report     TEXT NOT NULL DEFAULT '{}',
    kind       TEXT NOT NULL DEFAULT 'quiz'
);
CREATE INDEX IF NOT EXISTS idx_paper_up ON study_papers(user_id, pid, started_at);
"""

# 老库升级：v139 已经建过 study_wrongbook / study_quizzes，这里只补列。
# 用 PRAGMA 查过再 ALTER —— SQLite 没有 ADD COLUMN IF NOT EXISTS，
# 重复执行会抛 duplicate column name，而 executescript 一抛就整段回滚。
_ADD_COLS = {
    "study_wrongbook": (
        ("next_review", "REAL NOT NULL DEFAULT 0"),
        ("right_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("last_answer", "TEXT NOT NULL DEFAULT ''"),
    ),
    "study_quizzes": (
        ("paper_id", "TEXT NOT NULL DEFAULT ''"),
        ("blank_at", "TEXT NOT NULL DEFAULT '[]'"),
    ),
}


def chapter_key(a: dict) -> str:
    """章节键。用 || 拼法律简称与章节路径，避免不同法的同名章混在一起。"""
    return f"{a.get('law_short', '')}||{str(a.get('chapter_path') or '').strip() or _NO_CHAPTER}"


# 遗忘曲线的三个间隔（天）。错得越多隔得越久：第一次错明天再看见，
# 错了三次的隔一周——这类题多半是概念性误解，天天刷只会记住选项位置。
_REVIEW_GAP = (1, 3, 7)


def _review_gap(wrong_count: int) -> int:
    return _REVIEW_GAP[min(max(int(wrong_count or 1) - 1, 0), len(_REVIEW_GAP) - 1)]


def _ago(ts: float) -> str:
    """「3 天前」这种说法。前端不该拿到裸时间戳再自己算。"""
    d = max(0, int((time.time() - float(ts or 0)) // 86400))
    if d == 0:
        return "今天"
    if d == 1:
        return "昨天"
    return f"{d} 天前"


def _as_options(v: Any) -> list:
    """options 在内存题里是 list、在库里是 JSON 字符串，两种都要吃。

    冒烟时踩过：new_quiz 直接把内存里的 q 交给 public_quiz，list 被
    json.loads 抛 TypeError 后吞成 []，前端收到 0 个选项但判答照常能过
    ——这类「不报错但页面是空的」最难从现象反推。
    """
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


class StudyStore:
    """四张表全在 law.db 里，不用新文件——备份脚本不用改。"""

    def __init__(self, db_path: str) -> None:
        self.conn = _db_connect(db_path)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()
        self.lock = threading.Lock()

    def _migrate(self) -> None:
        """补列。逐列 PRAGMA 查，缺才 ALTER——老库（v139）升上来不丢数据。"""
        for table, cols in _ADD_COLS.items():
            have = {r["name"] for r in
                    self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------ 题
    def save_quiz(self, uid: int, pid: str, q: dict) -> str:
        qid = str(q.get("quiz_id") or uuid.uuid4().hex[:16])
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO study_quizzes (quiz_id,user_id,pid,"
                "article_id,law_short,chapter,qtype,question,options,answer,"
                "explain,cites,source,created_at,paper_id,blank_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (qid, uid, pid, q["article_id"], q["law_short"], q["chapter"],
                 q["qtype"], q["question"], json.dumps(q.get("options") or []),
                 q["answer"], q.get("explain", ""),
                 json.dumps(q.get("cites") or [q["article_id"]]),
                 q.get("source", "offline"), time.time(),
                 str(q.get("paper_id") or ""),
                 json.dumps(q.get("blank_at") or [])))
            self.conn.commit()
        return qid

    def get_quiz(self, uid: int, pid: str, qid: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM study_quizzes WHERE quiz_id=? AND user_id=? AND pid=?",
                (str(qid), uid, pid)).fetchone()
        return dict(r) if r else None

    def public_quiz(self, row: dict) -> dict:
        """给前端的题目：**不含答案与解析**，防止 F12 直接看答案。"""
        return {"quiz_id": row["quiz_id"], "pid": row["pid"],
                "qtype": row["qtype"], "question": row["question"],
                "options": _as_options(row.get("options")), "law_short": row["law_short"],
                "chapter": row["chapter"], "source": row["source"]}

    # ------------------------------------------------------------ 答
    def record(self, uid: int, pid: str, q: dict, answer: str,
               correct: bool, feedback: str) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO study_attempts (user_id,pid,quiz_id,answer,correct,"
                "score,feedback,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, pid, q["quiz_id"], str(answer)[:2000], 1 if correct else 0,
                 100.0 if correct else 0.0, str(feedback)[:800], now))
            # progress：seen/correct/wrong 只累加，不覆盖。
            self.conn.execute(
                "INSERT INTO study_progress (user_id,pid,article_id,chapter,"
                "seen,correct,wrong,updated_at) VALUES (?,?,?,?,1,?,?,?) "
                "ON CONFLICT(user_id,pid,article_id) DO UPDATE SET "
                " seen=seen+1, correct=correct+excluded.correct,"
                " wrong=wrong+excluded.wrong, updated_at=excluded.updated_at",
                (uid, pid, q["article_id"], q["chapter"],
                 1 if correct else 0, 0 if correct else 1, now))
            if correct:
                # 答对一次不自动移出错题集（v139 的老语义保留），但把
                # mastered 置 1。v140 额外记 right_streak：连续答对两次的
                # 才叫真掌握，界面上从「待复习」挪走靠的是这个，不是一次运气。
                self.conn.execute(
                    "UPDATE study_wrongbook SET mastered=1, "
                    " right_streak=right_streak+1, last_answer=?, "
                    " next_review=? "
                    "WHERE user_id=? AND pid=? AND quiz_id=?",
                    (str(answer)[:200], now + _REVIEW_GAP[0] * 86400,
                     uid, pid, q["quiz_id"]))
            else:
                # 间隔按「这次错完一共错了几次」算，所以先查再写。
                # 在 lock 里多一次 SELECT 无所谓：SQLite 同连接串行，成本是微秒级。
                prev = self.conn.execute(
                    "SELECT wrong_count FROM study_wrongbook "
                    "WHERE user_id=? AND pid=? AND quiz_id=?",
                    (uid, pid, q["quiz_id"])).fetchone()
                wc = int(prev["wrong_count"]) + 1 if prev else 1
                self.conn.execute(
                    "INSERT INTO study_wrongbook (user_id,pid,quiz_id,wrong_count,"
                    "mastered,last_wrong,next_review,right_streak,last_answer) "
                    "VALUES (?,?,?,1,0,?,?,0,?) "
                    "ON CONFLICT(user_id,pid,quiz_id) DO UPDATE SET "
                    " wrong_count=wrong_count+1, mastered=0, "
                    " right_streak=0, "
                    " last_wrong=excluded.last_wrong, "
                    " next_review=excluded.next_review, "
                    " last_answer=excluded.last_answer",
                    (uid, pid, q["quiz_id"], now,
                     now + _review_gap(wc) * 86400, str(answer)[:200]))
            self.conn.commit()

    def progress(self, uid: int, pid: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM study_progress WHERE user_id=? AND pid=?",
                (uid, pid)).fetchall()
        return [dict(r) for r in rows]

    def wrongbook(self, uid: int, pid: str | None = None,
                  mastered: int | None = 0) -> list[dict]:
        sql = ("SELECT w.*, q.question, q.options, q.answer, q.explain, "
               "q.law_short, q.chapter, q.qtype, q.article_id, q.cites "
               "FROM study_wrongbook w "
               "JOIN study_quizzes q ON q.quiz_id=w.quiz_id "
               "WHERE w.user_id=?")
        args: list[Any] = [uid]
        if pid:
            sql += " AND w.pid=?"
            args.append(pid)
        if mastered is not None:
            sql += " AND w.mastered=?"
            args.append(mastered)
        sql += " ORDER BY w.last_wrong DESC LIMIT 200"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        out: list[dict] = []
        now = time.time()
        for r in rows:
            d = dict(r)
            d["options"] = _as_options(d.get("options"))
            # 不把时间戳原样发给前端：前端要用「3 天前错 2 次」这种话。
            nr = float(d.get("next_review") or 0)
            d["due"] = 1 if (not d.get("mastered")) and nr and nr <= now else 0
            d["due_in_days"] = (0 if d["due"] else
                                (int(round((nr - now) / 86400)) if nr else 0))
            d["last_wrong_txt"] = _ago(float(d.get("last_wrong") or now))
            out.append(d)
        return out

    def mark_mastered(self, uid: int, pid: str, qid: str, on: bool) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE study_wrongbook SET mastered=? "
                "WHERE user_id=? AND pid=? AND quiz_id=?",
                (1 if on else 0, uid, pid, str(qid)))
            self.conn.commit()
        return cur.rowcount > 0

    def attempts_since(self, uid: int, since: float) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT pid,correct,created_at FROM study_attempts "
                "WHERE user_id=? AND created_at>=? ORDER BY created_at",
                (uid, since)).fetchall()
        return [dict(r) for r in rows]

    def total_attempts(self, uid: int) -> int:
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*) AS n FROM study_attempts WHERE user_id=?",
                (uid,)).fetchone()
        return int(r["n"])

    # ------------------------------------------------------------ 卷子
    def save_paper(self, uid: int, pid: str, paper: dict) -> str:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO study_papers (paper_id,user_id,pid,"
                "law_short,chapter,n,quiz_ids,started_at,submit_at,score,"
                "right_n,report,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (paper["paper_id"], uid, pid, paper.get("law_short", ""),
                 paper.get("chapter", ""), int(paper.get("n") or 0),
                 json.dumps(paper.get("quiz_ids") or []),
                 float(paper.get("started_at") or time.time()),
                 float(paper.get("submit_at") or 0),
                 int(paper.get("score") or 0), int(paper.get("right_n") or 0),
                 json.dumps(paper.get("report") or {}, ensure_ascii=False),
                 paper.get("kind") or "quiz"))
            self.conn.commit()
        return paper["paper_id"]

    def get_paper(self, uid: int, paper_id: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM study_papers WHERE paper_id=? AND user_id=?",
                (str(paper_id), uid)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["quiz_ids"] = json.loads(d.get("quiz_ids") or "[]")
        d["report"] = json.loads(d.get("report") or "{}")
        return d

    def papers(self, uid: int, pid: str | None = None, limit: int = 10) -> list[dict]:
        """交过卷的卷子，最近的在前。只给「已交卷」的：草稿不进趋势图。"""
        sql = ("SELECT paper_id,pid,law_short,chapter,n,score,right_n,"
               "started_at,submit_at,kind FROM study_papers "
               "WHERE user_id=? AND submit_at>0")
        args: list[Any] = [uid]
        if pid:
            sql += " AND pid=?"
            args.append(pid)
        sql += " ORDER BY submit_at DESC LIMIT ?"
        args.append(max(1, min(int(limit or 10), 50)))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def quiz_of_paper(self, uid: int, pid: str, paper_id: str) -> list[dict]:
        """卷子里全部题目 + 本题的作答结果。报告页逐题回顾用。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM study_quizzes WHERE user_id=? AND pid=? "
                "AND paper_id=? ORDER BY created_at", (uid, pid, paper_id)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                a = self.conn.execute(
                    "SELECT answer,correct,feedback FROM study_attempts "
                    "WHERE user_id=? AND pid=? AND quiz_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (uid, pid, d["quiz_id"])).fetchone()
                d["options"] = _as_options(d.get("options"))
                d["cites"] = json.loads(d.get("cites") or "[]")
                d["blank_at"] = json.loads(d.get("blank_at") or "[]")
                d["my_answer"] = (a["answer"] if a else "")
                d["correct_flag"] = (int(a["correct"]) if a else -1)
                out.append(d)
        return out


# ---------------------------------------------------------------- 出题

def _pick_articles(pid: str, law_short: str, chapter: str, n: int,
                   rng: random.Random | None = None) -> list[dict]:
    """从该内核索引里挑条文。只挑正文够长的——十几字的条文出不出题。

    rng 只在「每日一题」传：那边要求同一天同一用户同一法典不换题，
    候选池也必须可复现，光固定最后选第几题是不够的。
    """
    _, idx = profile_for(pid)
    pool = list(idx.articles)
    if law_short:
        pool = [a for a in pool if a.get("law_short") == law_short]
    if chapter:
        pool = [a for a in pool if chapter_key(a) == chapter]
    pool = [a for a in pool if len(str(a.get("content") or "")) >= 60]
    if not pool:
        return []
    pick = rng or random
    return pick.sample(pool, min(n, len(pool)))


def _snippet(a: dict, limit: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(a.get("content") or "")).strip()
    return s[:limit] + ("……" if len(s) > limit else "")


def _judge_options() -> list[str]:
    """判断题的选项固定两项。**不用「正确/错误」这种词做真值判断**，
    答案仍是 A/B，判答走的是同一个 `_choice_correct`，不另开一条路。"""
    return ["A. 对", "B. 错"]


def _offline_question(a: dict, idx, rng: random.Random,
                      qtype: str = "choice") -> dict | None:
    """离线题：零模型。`qtype` 给 choice / judge / blank 三者之一。

    三种都只依赖**真实条文**：
      choice  A 型 给节选选出自哪条 / B 型 给条号选哪段是它的内容
      judge   给节选 + 一个条号，问是不是这一条（一半为真）
      blank   从原文挖一个词，
    干扰项一律来自**同一部法的其他真条文**，不编造条文，也不编条号。
    挖不到空时回落 choice——但那已经是最后一道兜底：正常条文总能挖出一个
    4 字片段，用户要 2 道填空就该拿到 2 道。
    """
    if qtype == "judge":
        return _offline_judge(a, idx, rng)
    if qtype == "blank":
        q = _offline_blank(a, idx, rng)
        if q:
            return q
        qtype = "choice"
    law = a.get("law_short") or ""
    no_cn = a.get("article_no_cn") or f"第{a.get('article_no')}条"
    same = [x for x in idx.by_law.get(law, []) if x["id"] != a["id"]
            and str(x.get("content") or "").strip()]
    rng.shuffle(same)
    distract = same[:3]
    kind = "A" if rng.random() < 0.5 else "B"
    if kind == "A":
        opts = [f"《{law}》{x.get('article_no_cn') or x['article_no']}"
                for x in distract]
        right = f"《{law}》{no_cn}"
        question = f"以下条文节选出自哪一条？\n\n“{_snippet(a)}”"
        explain = (f"该节选出自《{a.get('law_name') or law}》{no_cn}。"
                   f"原文：{_snippet(a, 200)}")
    else:
        opts = [_snippet(x, 100) for x in distract]
        right = _snippet(a, 100)
        question = f"《{law}》{no_cn}主要规定了下列哪一项？"
        explain = (f"《{a.get('law_name') or law}》{no_cn}原文："
                   f"{_snippet(a, 200)}")
    while len(opts) < 3:      # 该法条文不足时补足，避免选项数飘
        opts.append(f"以上都不是（{law}其他条款）")
    all_opts = opts[:3] + [right]
    rng.shuffle(all_opts)
    answer = _LETTERS[all_opts.index(right)]
    return {
        "article_id": a["id"], "law_short": law, "chapter": chapter_key(a),
        "qtype": "choice", "question": question,
        "options": [f"{_LETTERS[i]}. {o}" for i, o in enumerate(all_opts)],
        "answer": answer, "explain": explain,
        "cites": [a["id"]], "source": "offline",
    }


_QUIZ_SYS = (
    "你在为一个法律学习平台出题。只输出 JSON，不要解释、不要 Markdown 代码块。"
    "题目必须严格依据给定的法条原文，不得引入原文之外的条号、案例、金额或结论；"
    "不得预测胜诉率，不得建议是否起诉或仲裁。"
)

# 填空题挖哪个词：优先「数字 + 单位」这种一眼能判对错的，其次接一个常见
# 法律名词。**只挖原文里的原样片段**，答案就是那段原文，不换说法、不改
# 条号——否则离线也没法自动判对错。
_BLANK_UNIT = (r"(?:\d+|[一二三四五六七八九十百千两]+)\s*(?:个|日|天|个月|月|年|"
               r"小时|工作日|倍|元|%|以上|以下|以内)")
_BLANK_WORD = ("消费者", "经营者", "用人单位", "劳动者", "合同", "书面", "赔偿",
               "退货", "工资", "社会保险", "欺诈", "责任", "解除", "告知",
               # v142：只有上面这串时，《劳动合同法》里「用人单位应当依法
               # 建立和完善规章制度」这类**整条没有一个数字、也没踩中上面
               # 任何一个词**的条文就挖不出空，于是「2 道填空」被悄悄换成
               # 2 道单选——实测 10 次出卷里有 2 次，测试跟着偶发红。
               "规章", "制度", "协商", "变更", "终止", "报酬", "试用期",
               "服务期", "保密", "竞业限制", "工会", "职工", "休息休假",
               "劳动保护", "经济补偿", "赔偿金", "违约金", "通知", "证明")
_RE_CN_NUM = r"[零一二三四五六七八九十百千两]+"


def _find_blank(text: str, rng: random.Random) -> tuple[str, list[int]] | None:
    """从原文里找一个可以挖空的连续片段，返回 (答案, [起, 止])。

    只挑「独立出现」的片段：长度 2–8 个汉字宽，前后不是同一个词的一部分。
    找不到就返回 None，调用方**换一条条文**再挖（见 `create_paper` 的
    `spare`），而不是硬挖一个 4 字片段——「＿＿＿＿赁物影响承租人使用的」
    这种半截词比换题型难看得多。
    """
    cands: list[tuple[str, int, int]] = []
    for m in re.finditer(_BLANK_UNIT, text):
        s, e = m.span()
        if s > 0 and text[s - 1].isdigit():
            continue
        cands.append((m.group(0), s, e))
    for w in _BLANK_WORD:
        for m in re.finditer(re.escape(w), text):
            cands.append((w, m.start(), m.end()))
    cands = [c for c in cands if 2 <= len(c[0]) <= 8]
    if not cands:
        return None
    rng.shuffle(cands)
    ans, s, e = cands[0]
    return ans, [s, e]


def _offline_judge(a: dict, idx, rng: random.Random) -> dict:
    """判断题：给一段条文节选 + 一个条号，问是不是这一条。

    一半为真、一半为假。为假时条号取自**同法其他真条文**，绝不编条号。
    这也顺带把「条文与条号对不上」这个最常见误记考出来了。
    """
    law = a.get("law_short") or ""
    no_cn = a.get("article_no_cn") or f"第{a.get('article_no')}条"
    same = [x for x in idx.by_law.get(law, []) if x["id"] != a["id"]
            and str(x.get("content") or "").strip()]
    truth = not same or rng.random() < 0.5
    if truth:
        claim_no = no_cn
        right = "对"
    else:
        other = rng.choice(same)
        claim_no = other.get("article_no_cn") or f"第{other.get('article_no')}条"
        right = "错"
    question = (f"判断：《{law}》{claim_no}的内容是——\n\n"
                f"“{_snippet(a)}”")
    explain = (f"该节选出自《{a.get('law_name') or law}》{no_cn}。"
               if truth else
               f"该节选出自《{a.get('law_name') or law}》{no_cn}，不是 {claim_no}。")
    explain += f"原文：{_snippet(a, 200)}"
    return {
        "article_id": a["id"], "law_short": law, "chapter": chapter_key(a),
        "qtype": "judge", "question": question,
        "options": ["A. 对", "B. 错"],
        "answer": "A" if right == "对" else "B", "explain": explain,
        "cites": [a["id"]], "source": "offline",
    }


def _offline_blank(a: dict, idx, rng: random.Random) -> dict | None:
    """填空题：从原文挖一个词，答案就是那个词，位置存进 blank_at。

    题面用 ＿＿＿＿ 占位（项目里「金额留空」是同一个记号，用户认得）。
    """
    law = a.get("law_short") or ""
    no_cn = a.get("article_no_cn") or f"第{a.get('article_no')}条"
    content = re.sub(r"\s+", " ", str(a.get("content") or "")).strip()
    if len(content) < 40:
        return None
    hit = _find_blank(content, rng)
    if not hit:
        return None
    ans, (s, e) = hit
    # 只取包含空的那一句，题面不至于一屏都是字。
    cut = content.rfind("。", 0, s)
    start = 0 if cut < 0 else cut + 1
    tail = content.find("。", e)
    end = len(content) if tail < 0 else tail + 1
    seg = content[start:end]
    s2, e2 = s - start, e - start
    shown = seg[:s2] + "＿＿＿＿" + seg[e2:]
    return {
        "article_id": a["id"], "law_short": law, "chapter": chapter_key(a),
        "qtype": "blank", "question": f"填空（《{law}》{no_cn}）：\n\n{shown}",
        "options": [], "answer": ans,
        "explain": f"《{a.get('law_name') or law}》{no_cn}原文：{_snippet(a, 200)}",
        "cites": [a["id"]], "source": "offline",
        "blank_at": [[s2, e2]],
    }


def _llm_question(a: dict, llm) -> dict | None:
    """在线题：模型只写题面/选项/解析，条号与引用由服务端回填。

    模型输出里若带条号文字，我们不采信也不保留——`cites` 恒为 [a['id']]，
    前端点「看原文」直接按这个 id 回库取，绝无幻觉引用的可能。
    """
    no_cn = a.get("article_no_cn") or f"第{a.get('article_no')}条"
    user = (
        f"法条原文（《{a.get('law_name') or a.get('law_short')}》{no_cn}）：\n"
        f"{str(a.get('content') or '')[:900]}\n\n"
        "请出一道四选一单选题，输出 JSON："
        '{"question":"题面","options":["选项1","选项2","选项3","选项4"],'
        '"answer":"A/B/C/D 中正确项字母","explain":"解析，须引用上述原文要点"}\n'
        "要求：四个选项长度接近、只有一个正确答案；不要把条号写进选项；"
        "解析不超过 120 字。")
    try:
        r = llm._chat(_QUIZ_SYS, user, json_mode=True, max_tokens=900,
                      allow_think=False)
        data = _loads_obj(r.choices[0].message.content)
    except Exception as e:                # noqa: BLE001
        if hasattr(llm, "_downgrade"):
            llm._downgrade(e)
        return None
    opts = data.get("options")
    ans = str(data.get("answer") or "").strip().upper()[:1]
    q = str(data.get("question") or "").strip()
    if not isinstance(opts, list) or len(opts) != 4 or ans not in _LETTERS or not q:
        return None
    return {
        "article_id": a["id"], "law_short": a.get("law_short") or "",
        "chapter": chapter_key(a), "qtype": "choice",
        "question": q, "options": [f"{_LETTERS[i]}. {str(o)[:200]}"
                                   for i, o in enumerate(opts)],
        "answer": ans, "explain": str(data.get("explain") or "")[:600],
        "cites": [a["id"]], "source": "llm",
    }


def new_quiz(store: StudyStore, llm, uid: int, pid: str, *,
             law_short: str = "", chapter: str = "", n: int = 5) -> dict:
    """出一组题。在线优先用模型，失败/无 key 自动回落离线题，绝不空手返回。"""
    p, idx = profile_for(pid)
    n = max(1, min(int(n or 5), MAX_QUIZ))
    arts = _pick_articles(p.id, law_short, chapter, n)
    if not arts:
        return {"pid": p.id, "items": [], "source": "offline",
                "note": "该范围内没有足够长的条文可出题"}
    rng = random.Random()
    online = bool(llm is not None and getattr(llm, "online", False))
    out, used_llm = [], 0
    for a in arts:
        q = None
        if online:
            q = _llm_question(a, llm)
        if q is None:
            q = _offline_question(a, idx, rng)
        else:
            used_llm += 1
        q["pid"] = p.id
        q["quiz_id"] = store.save_quiz(uid, p.id, q)
        out.append(store.public_quiz(q))
    return {"pid": p.id, "items": out,
            "source": "llm" if used_llm == len(out) else (
                "mixed" if used_llm else "offline"),
            "note": ("题目依据本内核法条原文，答案在提交后给出"
                     if online else "当前为回放模式，题目由本地法条原文生成，可正常作答")}


# ---------------------------------------------------------------- 卷子（v140）

_QTYPE_CN = {"choice": "单选", "judge": "判断", "blank": "填空"}
_DEFAULT_MIX = {"choice": 6, "judge": 2, "blank": 2}


def _mix_for(qtypes: Any, n: int) -> list[str]:
    """按配比摊出 n 个题型的顺序数组。配比给空就用 6:2:2 的默认。

    摊法用「最大余数法」：n=7 且配比 6:2:2 时得到 5 单选 1 判断 1 填空，
    而不是先给满单选再给判断。这样小卷子也能三种题型都见到。
    """
    if isinstance(qtypes, list) and qtypes:
        want = {k: max(0, int(v)) for k, v in
                ((str(x), 1) for x in qtypes if str(x) in _QTYPE_CN)}
        if not want:
            want = dict(_DEFAULT_MIX)
    elif isinstance(qtypes, dict) and qtypes:
        want = {str(k): max(0, int(v)) for k, v in qtypes.items()
                if str(k) in _QTYPE_CN}
        if not want:
            want = dict(_DEFAULT_MIX)
    else:
        want = dict(_DEFAULT_MIX)
    total = sum(want.values()) or 1
    raw = {k: n * v / total for k, v in want.items()}
    base = {k: int(v) for k, v in raw.items()}
    left = n - sum(base.values())
    for k in sorted(raw, key=lambda x: (-(raw[x] - base[x]), x))[:max(0, left)]:
        base[k] += 1
    seq: list[str] = []
    # 交错排：不要让前 6 题全是单选、最后 2 题才判断题，做题节奏很怪。
    pools = {k: [k] * v for k, v in base.items()}
    order = ["choice", "judge", "blank"]
    while any(pools.values()):
        for k in order:
            if pools.get(k):
                seq.append(pools[k].pop())
    return seq or ["choice"] * n


def create_paper(store: StudyStore, llm, uid: int, pid: str, *,
                 law_short: str = "", chapter: str = "", n: int = 10,
                 qtypes: Any = None, kind: str = "quiz",
                 only_quiz_ids: list[str] | None = None) -> dict:
    """生成一张卷子：挑条文 → 按题型配比出题 → 存成一卷。

    **只存题、不判题**：作答结果在交卷时逐题 grade，然后 `submit_paper`
    汇总。这样「边做边判」和「做完一起判」共用同一套判答逻辑，
    进度/错题/遗忘曲线的写入路径只有一条，不会两套口径打架。

    `only_quiz_ids` 给「错题重练卷」用：不重新出题，直接把指定的旧题
    装进一张新卷。旧题原文不变，判答走的还是同一条路。
    """
    p, idx = profile_for(pid)
    n = max(1, min(int(n or 10), MAX_PAPER))
    rng = random.Random()
    paper_id = "p" + uuid.uuid4().hex[:14]
    online = bool(llm is not None and getattr(llm, "online", False))
    items: list[dict] = []

    if only_quiz_ids:
        for qid in only_quiz_ids[:n]:
            row = store.get_quiz(uid, p.id, str(qid))
            if not row:
                continue
            row["paper_id"] = paper_id
            store.save_quiz(uid, p.id, row)
            items.append(dict(store.public_quiz(row),
                              paper_id=paper_id))
        if not items:
            return {"pid": p.id, "paper_id": "", "items": [],
                    "note": "选中的错题已不在错题集里，刷新后再试"}
        store.save_paper(uid, p.id, {
            "paper_id": paper_id, "law_short": law_short, "chapter": chapter,
            "n": len(items), "quiz_ids": [i["quiz_id"] for i in items],
            "started_at": time.time(), "kind": "review"})
        return {"pid": p.id, "paper_id": paper_id, "items": items,
                "source": "offline", "kind": "review", "n": len(items),
                "note": "错题重练卷：题目与你之前答错的一模一样，答案不会变"}

    # 多抽 6 条当备用：要填空的那几道挖不出空时，换一条还能挖的顶上。
    # 不这么做就只能回落成单选——用户点了「2 道填空」却拿到 0 道，
    # 交卷报告里的题型统计跟他点的不一样（实测 10 次出卷里有 2 次）。
    # **只抽一次再切**：分两次抽会有同一条文进两张题面的可能。
    picked = _pick_articles(p.id, law_short, chapter, n + 6, rng)
    if not picked:
        return {"pid": p.id, "paper_id": "", "items": [],
                "note": "该范围内没有足够长的条文可出题"}
    arts, spare = picked[:n], picked[n:]
    seq = _mix_for(qtypes, len(arts))
    used_llm = 0
    for i, a in enumerate(arts):
        want = seq[i] if i < len(seq) else "choice"
        q = None
        # 在线时只有 choice 交给模型：judge/blank 的真值和挖空位置是
        # 服务端算出来的，交给模型等于把「答案唯一」这件事押在它身上。
        if online and want == "choice":
            q = _llm_question(a, llm)
        if q is None:
            q = _offline_question(a, idx, rng, want)
        else:
            used_llm += 1
        # 要填空却只拿到单选题 = 这条挖不出空。换备用条文，别换题型。
        if want == "blank" and q and q.get("qtype") != "blank":
            while spare:
                alt = spare.pop(0)
                q2 = _offline_blank(alt, idx, rng)
                if q2:
                    q = q2
                    break
        q["pid"] = p.id
        q["paper_id"] = paper_id
        q["quiz_id"] = store.save_quiz(uid, p.id, q)
        items.append(dict(store.public_quiz(q), paper_id=paper_id))
    store.save_paper(uid, p.id, {
        "paper_id": paper_id, "law_short": law_short, "chapter": chapter,
        "n": len(items), "quiz_ids": [i["quiz_id"] for i in items],
        "started_at": time.time(), "kind": kind})
    mix = {k: sum(1 for i in items if i["qtype"] == k) for k in _QTYPE_CN}
    return {"pid": p.id, "paper_id": paper_id, "items": items,
            "n": len(items), "mix": mix,
            "source": "llm" if used_llm == len(items) else (
                "mixed" if used_llm else "offline"),
            "note": ("题目依据本内核法条原文，交卷后统一判答"
                     if online else "回放模式：题目由本地法条原文生成，可正常作答")}


def _chapter_rows(items: list[dict]) -> list[dict]:
    """按章节汇总本次作答。报告页的雷达图与「下一步」都读它。"""
    agg: dict[str, dict] = {}
    for it in items:
        law, _, ch = str(it.get("chapter") or "").partition("||")
        key = f"{law}||{ch}"
        d = agg.setdefault(key, {"law": law, "chapter": ch,
                                 "total": 0, "right": 0})
        d["total"] += 1
        if it.get("correct_flag") == 1:
            d["right"] += 1
    out = []
    for d in agg.values():
        d["rate"] = int(round(100 * d["right"] / d["total"])) if d["total"] else 0
        out.append(d)
    out.sort(key=lambda x: (x["rate"], -x["total"]))
    return out


def submit_paper(store: StudyStore, uid: int, pid: str, paper_id: str,
                 elapsed: int = 0) -> dict | None:
    """交卷：汇总本次作答，写回 study_papers，生成评估报告。

    **不重复判答**：题目在作答时已经逐题 grade 过（客户端每题一次请求），
    这里只读结果、算统计。所以哪怕客户端卡在最后一题，前面答过的也不算丢。
    """
    p, _ = profile_for(pid)
    paper = store.get_paper(uid, paper_id)
    if not paper or paper["pid"] != p.id:
        return None
    items = store.quiz_of_paper(uid, p.id, paper_id)
    total = len(items)
    right = sum(1 for it in items if it["correct_flag"] == 1)
    answered = sum(1 for it in items if it["correct_flag"] >= 0)
    score = int(round(100 * right / total)) if total else 0

    by_type: dict[str, dict] = {}
    for it in items:
        t = it.get("qtype") or "choice"
        d = by_type.setdefault(t, {"type": t, "name": _QTYPE_CN.get(t, t),
                                   "total": 0, "right": 0, "wrong": 0})
        d["total"] += 1
        if it["correct_flag"] == 1:
            d["right"] += 1
        elif it["correct_flag"] == 0:
            d["wrong"] += 1

    chapters = _chapter_rows(items)
    # 趋势：与最近 10 张已交卷比较，位置放最后一位。
    hist = store.papers(uid, p.id, 10)
    trend = [{"paper_id": h["paper_id"], "score": int(h["score"]),
              "at": h["submit_at"]} for h in reversed(hist)]
    prev = hist[0]["score"] if hist else None
    delta = (score - int(prev)) if prev is not None else None

    # 「下一步」必须能拍板：直接点最弱那一章，而不是说「请加强复习」。
    weakest = chapters[0] if chapters else None
    nxt = ""
    if total == 0:
        nxt = "这张卷子还没有作答记录，先答完再交卷。"
    elif weakest and weakest["rate"] < 60:
        nxt = (f"先练《{weakest['law']}》{weakest['chapter'] or '未分章'}："
               f"本次 {weakest['total']} 题只对 {weakest['right']} 题。")
    elif score >= 90:
        nxt = "这张卷子已经过关，可以换一章或换一部法继续。"
    elif score >= 70:
        nxt = "整体过关，错的那几题进错题集了，过两天重练一遍。"
    else:
        nxt = "正确率不到七成，建议按错题集的重练卷再过一遍再往下走。"

    report = {
        "paper_id": paper_id, "pid": p.id,
        "score": score, "total": total, "right": right,
        "wrong": answered - right, "unanswered": total - answered,
        "elapsed": max(0, int(elapsed or 0)),
        "by_type": list(by_type.values()),
        "chapters": chapters, "trend": trend, "delta": delta,
        "next": nxt,
    }
    store.save_paper(uid, p.id, {
        "paper_id": paper_id, "law_short": paper["law_short"],
        "chapter": paper["chapter"], "n": total,
        "quiz_ids": paper["quiz_ids"], "started_at": paper["started_at"],
        "submit_at": time.time(), "score": score, "right_n": right,
        "report": report, "kind": paper.get("kind") or "quiz"})
    return report


def paper_detail(store: StudyStore, uid: int, pid: str,
                 paper_id: str) -> dict | None:
    """报告页：汇总 + 逐题回顾（含你的答案、正确答案、解析、引用条号）。"""
    p, _ = profile_for(pid)
    paper = store.get_paper(uid, paper_id)
    if not paper or paper["pid"] != p.id:
        return None
    if not paper.get("submit_at"):
        return {"paper_id": paper_id, "submitted": False,
                "report": {}, "items": [],
                "note": "这张卷子还没交，答完后点「交卷」才有报告。"}
    items = store.quiz_of_paper(uid, p.id, paper_id)
    return {"paper_id": paper_id, "submitted": True,
            "report": paper.get("report") or {}, "pid": p.id,
            "beijing_note": "报告按本次作答实时汇总",
            "items": [{
                "quiz_id": it["quiz_id"], "qtype": it["qtype"],
                "question": it["question"], "options": it["options"],
                "answer": it["answer"], "explain": it.get("explain") or "",
                "law_short": it["law_short"], "chapter": it["chapter"],
                "article_id": it["article_id"],
                "my_answer": it["my_answer"],
                "correct_flag": it["correct_flag"],
                "cites": it["cites"],
            } for it in items]}


# ---------------------------------------------------------------- 判答

def _norm(s: Any) -> str:
    # 外层用单引号，字符类里的中文双/单引号一律走 \u 转义，避免
    # Python 把相邻字面量隐式拼接、悄悄吃掉一个引号字符。
    return re.sub(r'[\s，。、；：、,.;:（）()\u201c\u201d\u2018\u2019「」『』]',
                  "", str(s or "")).strip().upper()


def _choice_correct(row: dict, answer: str) -> bool:
    """客观题：接受字母或选项全文，两种写法都算。"""
    a = _norm(answer)
    if not a:
        return False
    right = _norm(row.get("answer"))
    if a == right:
        return True
    if len(a) == 1 and a in _LETTERS:
        return a == right
    opts = _as_options(row.get("options"))
    want = next((o for o in opts if str(o).strip().upper().startswith(right + ".")), "")
    return bool(want) and _norm(want) == a


# 填空的宽容匹配：用户要打「三十日」还是「30 日」不该决定对错。
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2num(s: str) -> int | None:
    """把「三十」「十五」「一百二十」转成数字；「三十日」先被剥成「三十」。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in _CN_NUM:
            cur = _CN_NUM[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            return None
    return total + cur


def _blank_correct(row: dict, answer: str) -> bool:
    """填空：宽容匹配。三层都过不了才算错。

    ① 归一后完全相同；
    ② 数字换算法相同（「三十日」=「30日」=「30 天」）；
    ③ 用户答的是答案的一部分且长度 ≥2（答「三十」对「三十日」算对，
       但只答「三」不算——一个字太容易蒙）。
    """
    a = _norm(answer)
    right = _norm(row.get("answer"))
    if not a:
        return False
    if a == right:
        return True
    # 只比数字部分：答案里带单位时，把两边的单位剥掉再比数。
    m_r = re.match(rf"^(.+?)(个|日|天|个月|月|年|小时|工作日|倍|元|以上|以下|以内)$", right)
    m_a = re.match(rf"^(.+?)(个|日|天|个月|月|年|小时|工作日|倍|元|以上|以下|以内)$", a)
    if m_r and m_a and m_r.group(2) == m_a.group(2):
        n1, n2 = _cn2num(m_r.group(1)), _cn2num(m_a.group(1))
        if n1 is not None and n1 == n2:
            return True
    if len(a) >= 2 and a in right:
        return True
    return False


def _is_correct(row: dict, answer: str) -> bool:
    return (_blank_correct if row.get("qtype") == "blank"
            else _choice_correct)(row, answer)


def grade_answer(store: StudyStore, llm, uid: int, pid: str,
                 quiz_id: str, answer: str) -> dict | None:
    """判一道题。题目必须属于 (uid, pid)，跨用户/跨内核一律拒。"""
    p, _ = profile_for(pid)
    row = store.get_quiz(uid, p.id, quiz_id)
    if not row:
        return None
    correct = _is_correct(row, answer)
    if row.get("qtype") == "blank":
        right_txt = row["answer"]
    elif row.get("qtype") == "judge":
        right_txt = "对" if row["answer"] == "A" else "错"
    else:
        right_txt = row["answer"]
    feedback = "回答正确。" if correct else f"正确答案是 {right_txt}。"
    explain = row.get("explain") or ""
    if explain:
        feedback += " " + explain
    store.record(uid, p.id, row, answer, correct, feedback)
    return {"quiz_id": quiz_id, "pid": p.id, "correct": correct,
            "answer": row["answer"], "answer_txt": right_txt,
            "qtype": row.get("qtype"),
            "feedback": feedback,
            "cites": json.loads(row.get("cites") or "[]"),
            "source": row.get("source")}


# ---------------------------------------------------------------- 个人主页

def _chapters_of(pid: str) -> int:
    """该内核语料里有多少个章节。分母只数「有正文的条文」所在章。"""
    _, idx = profile_for(pid)
    return len({chapter_key(a) for a in idx.articles
                if len(str(a.get("content") or "")) >= 60})


def _pct(coverage: float, accuracy: float) -> int:
    return int(round(100 * (0.7 * coverage + 0.3 * accuracy)))


def _cal_cell(count: int, right: int) -> int:
    """日历格子的等级 0–4。按**当天题量**分档，和 GitHub 贡献图同一套直觉：
    0 没练、1 零到 4 题、2 五到九、3 十到十九、4 二十题以上。
    """
    if count <= 0:
        return 0
    if count < 5:
        return 1
    if count < 10:
        return 2
    if count < 20:
        return 3
    return 4


def calendar_of(store: StudyStore, uid: int, month: str = "") -> dict:
    """打卡日历。给一个月（YYYY-MM）或「近 12 周」。

    为什么不能只看最近七天：`dashboard.heat` 那七根条是 v139 的简化版，
    看不出「上个月是不是断了一整周」。日历要按自然月排网格，前端才能
    画成一格一天、周一到周日对齐的日历。
    """
    today = date.today()
    if month:
        try:
            y, m = (int(x) for x in month.split("-")[:2])
            m = min(12, max(1, m))
        except (ValueError, TypeError):
            y, m = today.year, today.month
    else:
        y, m = today.year, today.month
    first = date(y, m, 1)
    nxt = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    days = (nxt - first).days
    span = store.attempts_since(uid, time.mktime(first.timetuple()) - 86400 * 2)
    agg: dict[str, dict] = {}
    for a in span:
        d = date.fromtimestamp(a["created_at"])
        if d < first or d >= nxt:
            continue
        k = d.isoformat()
        c = agg.setdefault(k, {"count": 0, "right": 0})
        c["count"] += 1
        c["right"] += 1 if a["correct"] else 0
    cells = []
    for i in range(days):
        d = first + timedelta(days=i)
        c = agg.get(d.isoformat(), {"count": 0, "right": 0})
        cells.append({"date": d.isoformat(), "d": d.day,
                      # 周一起算：Python 的 weekday() 周一=0，日历表头也是周一打头
                      "w": d.weekday(),
                      "count": c["count"], "right": c["right"],
                      "level": _cal_cell(c["count"], c["right"]),
                      "future": 1 if d > today else 0})
    active = sum(1 for c in cells if c["count"] > 0)
    total_q = sum(c["count"] for c in cells)
    # 连续天数从今天（或最近有记录的一天）往回数，断了就停。
    streak = 0
    cur = today
    if not agg.get(today.isoformat()):
        cur = today - timedelta(days=1)
    while agg.get(cur.isoformat()) and cur.year == y and cur.month == m:
        streak += 1
        cur -= timedelta(days=1)
    return {"month": f"{y:04d}-{m:02d}", "label": f"{y} 年 {m} 月",
            "first_w": first.weekday(), "days": days,
            "cells": cells, "active_days": active, "total": total_q,
            "streak": streak,
            "prev": (date(y - 1, 12, 1) if m == 1 else date(y, m - 1, 1)).strftime("%Y-%m"),
            "next": (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)).strftime("%Y-%m"),
            "can_next": f"{y:04d}-{m:02d}" < today.strftime("%Y-%m")}


def _daily_series(store: StudyStore, uid: int, days: int = 30) -> list[dict]:
    """近 N 天每日答题数与正确率，折线图用。没答题的日子补 0 而不是跳过——
    跳过会把「断了三天」画成一条平滑的斜线，正好掩盖了要暴露的信息。
    """
    since = time.time() - days * 86400
    rows = store.attempts_since(uid, since)
    agg: dict[str, dict] = {}
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        agg[d] = {"date": d, "count": 0, "right": 0}
    for a in rows:
        k = date.fromtimestamp(a["created_at"]).isoformat()
        if k in agg:
            agg[k]["count"] += 1
            agg[k]["right"] += 1 if a["correct"] else 0
    out = []
    for d in sorted(agg):
        c = agg[d]
        c["rate"] = int(round(100 * c["right"] / c["count"])) if c["count"] else 0
        out.append(c)
    return out


def dashboard(store: StudyStore, uid: int) -> dict:
    """五个法典的进度、知识树、错题、打卡、薄弱项、徽章。"""
    pids = list(_profiles.ids())
    total_att = store.total_attempts(uid)
    since = time.time() - 7 * 86400
    recent = store.attempts_since(uid, since)
    books_out, tree_out = [], []
    for pid in pids:
        p, _ = profile_for(pid)
        rows = store.progress(uid, pid)
        total_ch = max(1, _chapters_of(pid))
        seen_ch = {r["chapter"] for r in rows if r["seen"]}
        ok = sum(int(r["correct"]) for r in rows)
        bad = sum(int(r["wrong"]) for r in rows)
        acc = ok / (ok + bad) if (ok + bad) else 0.0
        cov = min(1.0, len(seen_ch) / total_ch)
        books_out.append({
            "pid": pid, "name": p.name, "short": p.short, "seal": p.seal,
            "percent": _pct(cov, acc), "chapters_seen": len(seen_ch),
            "chapters_total": total_ch, "articles_seen": len(rows),
            "correct": ok, "wrong": bad,
            "accuracy": int(round(acc * 100)),
        })
        # 知识树：按法律 → 章 → 掌握度。每个法典一棵，互不相干。
        laws: dict[str, list] = {}
        for r in rows:
            law, _, ch = r["chapter"].partition("||")
            n = int(r["correct"]) + int(r["wrong"])
            laws.setdefault(law, []).append({
                "chapter": ch, "seen": int(r["seen"]),
                "correct": int(r["correct"]), "wrong": int(r["wrong"]),
                "mastery": int(round(int(r["correct"]) / n * 100)) if n else 0,
            })
        tree_out.append({"pid": pid, "name": p.name, "laws": [
            {"law": k, "chapters": sorted(v, key=lambda x: x["chapter"])}
            for k, v in sorted(laws.items())]})
    # 打卡：最近七天，按本地日期。
    today = date.today()
    heat = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        heat.append({"date": d.isoformat(), "count": 0})
    bucket = {h["date"]: h for h in heat}
    for a in recent:
        k = date.fromtimestamp(a["created_at"]).isoformat()
        if k in bucket:
            bucket[k]["count"] += 1
    # 薄弱章节：按错题次数排，跨法典取前三，但标出属于哪部法。
    weak = []
    for pid in pids:
        for r in store.progress(uid, pid):
            if int(r["wrong"]) > 0:
                law, _, ch = r["chapter"].partition("||")
                weak.append({"pid": pid, "law": law, "chapter": ch,
                             "wrong": int(r["wrong"])})
    weak.sort(key=lambda x: -x["wrong"])
    wrong = store.wrongbook(uid, None, 0)
    due = [w for w in wrong if w.get("due")]
    # 徽章要历史口径：`heat` 只有七天、`recent` 也是七天，画不出「连续三十
    # 天」。这里拉近一年的作答记录，最长连续段与累计打卡天数都从它算。
    year_ats = store.attempts_since(uid, time.time() - 366 * 86400)
    mastered_n = sum(1 for w in wrong if w.get("mastered"))
    badges = _badges(books_out, total_att, heat, wrong, {
        "attempts": year_ats,
        "papers": store.papers(uid, None, 50),
        "streak_long": _longest_streak(year_ats),
        "mastered": mastered_n,
    })
    # v140：知识树的「根」需要每部法有多少章才能算出「还剩多少没长出来」。
    # 之前 tree 只列答过的章，画出来的树只有几片叶子，看不出全貌。
    for t in tree_out:
        total_ch = _chapters_of(t["pid"])
        seen = sum(len(l["chapters"]) for l in t["laws"])
        t["chapters_total"] = total_ch
        t["chapters_seen"] = seen
        t["percent"] = int(round(100 * min(1.0, seen / total_ch))) if total_ch else 0
    papers = store.papers(uid, None, 10)
    return {
        "profiles": books_out, "tree": tree_out, "heat": heat,
        "weak": weak[:3], "wrong_count": len(wrong), "wrong": wrong[:20],
        "total_attempts": total_att, "badges": badges,
        "due_count": len(due), "due": due[:10],
        "calendar": calendar_of(store, uid, ""),
        "daily": _daily_series(store, uid, 30),
        "papers": [{"paper_id": p["paper_id"], "pid": p["pid"],
                    "score": int(p["score"]), "n": int(p["n"]),
                    "at": p["submit_at"],
                    "label": date.fromtimestamp(p["submit_at"]).strftime("%m-%d")}
                   for p in reversed(papers)],
        "note": "进度 = 章节覆盖率 × 70% + 答题正确率 × 30%；五个法典各自独立计算。",
    }


def _badge(name: str, desc: str, cur: int, goal: int) -> dict:
    return {"name": name, "desc": desc, "cur": int(cur), "goal": int(goal),
            "got": 1 if cur >= goal else 0,
            "pct": min(100, int(round(100 * cur / goal))) if goal else 0}


def _longest_streak(ats: list[dict]) -> int:
    """历史最长连续打卡天数。

    三档「不辍」徽章（三日 / 七日 / 月满）都在比这个数。**不能拿
    `dashboard.heat` 那七天的 `days` 冒充**：那只数「最近七天里有几天练过」，
    断在中间也算数，而且永远到不了 30。这里按近一年真实作答日期集合
    数最长连续段。
    """
    days = {date.fromtimestamp(a["created_at"]) for a in ats}
    if not days:
        return 0
    best = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if (prev and (d - prev).days == 1) else 1
        best = max(best, run)
        prev = d
    return best


def _badges(books: list[dict], total: int, heat: list[dict],
            wrong: list[dict], ctx: dict | None = None) -> list[dict]:
    """徽章：**全部列出**，未解锁的带进度。

    v139 只返回已获得的，页面上看不到「下一个目标是什么」。现在每枚都带
    `got` / `cur` / `goal`，前端能画成灰色剪影 + 进度条；未获得的不写
    长句子，只留一句条件。

    v141 扩到 21 枚、六组（`kind`）：累计 / 坚持 / 精通 / 卷子 / 广度 /
    错题。组别只用于前端分栏，**不影响获得条件**。
    """
    ctx = ctx or {}
    ats: list[dict] = ctx.get("attempts") or []
    papers: list[dict] = ctx.get("papers") or []
    days = sum(1 for h in heat if h["count"] > 0)
    best = max([b["percent"] for b in books] or [0])
    streak_long = int(ctx.get("streak_long") or 0)
    right = sum(int(a["correct"] or 0) for a in ats)
    # 正确率只有在样本够厚时才算数，否则第一题答对就是 100%。
    acc = int(round(100.0 * right / len(ats))) if ats else 0
    scored = [p for p in papers if int(p.get("n") or 0) > 0]
    full = [p for p in scored
            if int(p.get("score") or 0) >= 100 and int(p.get("right_n") or 0)
            >= int(p.get("n") or 0)]
    passed = [p for p in scored if int(p.get("score") or 0) >= 80]
    pids_hit = {p["pid"] for p in ats}
    laws_hit = 0
    for b in books:
        if int(b.get("chapters_seen") or 0) > 0:
            laws_hit += 1
    chapters_total = sum(int(b.get("chapters_total") or 0) for b in books)
    chapters_seen = sum(int(b.get("chapters_seen") or 0) for b in books)
    mastered_wrong = int(ctx.get("mastered") or 0)
    # 「百日之勤」数的是**历史**打卡天数，不是 heat 那七天：heat 是近七天
    # 的桶，永远是 0–7，拿它跟 60 比等于永不解锁。口径从近一年作答里取。
    day_set = {date.fromtimestamp(a["created_at"]) for a in ats}

    got: list[dict] = []

    def add(name, desc, cur, goal, kind):
        b = _badge(name, desc, cur, goal)
        b["kind"] = kind
        got.append(b)

    # ---- 累计：答了多少 ----
    add("开篇", "完成第一道练习题", total, 1, "累计")
    add("十日功", "累计作答 10 题", total, 10, "累计")
    add("百题", "累计作答 100 题", total, 100, "累计")
    add("千题", "累计作答 1000 题", total, 1000, "累计")
    # ---- 坚持：连了多少天 ----
    add("三日不辍", "连续三天都有练习", streak_long, 3, "坚持")
    add("七日不辍", "连续七天都有练习", streak_long, 7, "坚持")
    add("月满", "连续三十天都有练习", streak_long, 30, "坚持")
    add("百日之勤", "累计打卡 60 天", len(day_set), 60, "坚持")
    # ---- 精通：某一部法练到什么程度 ----
    add("过半", "某部法进度超过 50%", min(best, 50), 50, "精通")
    add("通读", "某部法进度达到 100%", min(best, 100), 100, "精通")
    # ---- 卷子：整卷表现 ----
    add("交卷", "完整交一张卷子", len(scored), 1, "卷子")
    add("八十分", "有一张卷子拿到 80 分以上", len(passed), 1, "卷子")
    add("满分卷", "有一张卷子全对", len(full), 1, "卷子")
    add("十卷", "完整交十张卷子", len(scored), 10, "卷子")
    # ---- 广度：跨了多少部法、多少章 ----
    add("跨界", "在两部以上的法里练过", len(pids_hit), 2, "广度")
    add("五法通练", "五部法都练过至少一章", laws_hit, 5, "广度")
    add("百章", "练过的章节累计 100 章", min(chapters_seen, 100), 100, "广度")
    add("半壁", "练过的章节超过全库一半", min(chapters_seen,
                                             chapters_total // 2),
        max(1, chapters_total // 2), "广度")
    # ---- 错题：清得掉、也啃得动 ----
    # 这两枚都要求做过题，否则空账号一进来就是「错题清零」。
    zero = 0 if wrong else (1 if total >= 10 else 0)
    add("错题清零", "作答十题以上且错题集为空", zero, 1, "错题")
    add("啃硬骨", "把 5 道错题标记为已掌握", mastered_wrong, 5, "错题")
    # 正确率要看样本：至少 30 题、七成以上。
    # 分两段算，进度条才读得懂：先攒到 30 题（下半段），再谈 70% 正确率
    # （上半段）。直接给 `acc` 的话，答 2 题全对就是 100%，进度条满格却
    # 还没拿到徽章，看着像坏了。
    if len(ats) < 30:
        steady = min(30, len(ats))
    else:
        steady = 30 + int(round(40.0 * min(100, acc) / 100))
    add("稳", "累计作答 30 题且正确率 70% 以上", steady, 70, "错题")
    return got


def daily_question(store: StudyStore, llm, uid: int, pid: str) -> dict:
    """每日一题：按 (用户, 法典, 日期) 确定性选题，同一天刷新不换题。"""
    p, idx = profile_for(pid)
    rng = random.Random(f"{uid}|{p.id}|{date.today().isoformat()}")
    arts = _pick_articles(p.id, "", "", 8, rng)
    if not arts:
        return {"pid": p.id, "items": [], "note": "本内核暂无可出题的条文"}
    a = arts[rng.randrange(len(arts))]
    q = None
    if llm is not None and getattr(llm, "online", False):
        q = _llm_question(a, llm)
    if q is None:
        q = _offline_question(a, idx, rng)
    q["pid"] = p.id
    q["quiz_id"] = store.save_quiz(uid, p.id, q)
    return {"pid": p.id, "date": date.today().isoformat(),
            "items": [store.public_quiz(q)], "source": q["source"]}


def report_text(store: StudyStore, uid: int,
                paper_id: str | None = None, pid: str | None = None) -> str:
    """导出纯文本学习报告。项目一贯用 .txt，不引 PDF 依赖。

    `paper_id` 给了就导**单张卷子的评估报告**（逐题回顾 + 题型得分 +
    章节分布 + 下一步），否则导账号总览。两条路径共用 .txt 一个出口，
    前端不需要为「导出卷子报告」再写一套下载逻辑。
    """
    if paper_id:
        return _paper_report_text(store, uid, str(pid or ""), paper_id)
    d = dashboard(store, uid)
    cal = d.get("calendar") or {}
    lines = ["法律知识学习报告", "=" * 40,
             f"生成日期：{date.today().isoformat()}",
             f"累计作答：{d['total_attempts']} 题",
             f"待复习错题：{d['wrong_count']} 题"
             f"（其中今天到期 {d.get('due_count', 0)} 题）",
             f"本月打卡：{cal.get('active_days', 0)} 天 / "
             f"本月作答 {cal.get('total', 0)} 题", "",
             "一、各法典学习进度", "-" * 40]
    for b in d["profiles"]:
        lines.append(f"  {b['name']}：{b['percent']}%  "
                     f"（章节 {b['chapters_seen']}/{b['chapters_total']}，"
                     f"正确率 {b['accuracy']}%）")
    if d["weak"]:
        lines += ["", "二、薄弱章节（按错题数）", "-" * 40]
        for w in d["weak"]:
            lines.append(f"  {w['law']} · {w['chapter']}：错 {w['wrong']} 次")
    if d.get("papers"):
        lines += ["", "三、最近卷子（近 10 张）", "-" * 40]
        for p in d["papers"]:
            lines.append(f"  {p['label']}  {p['score']} 分  "
                         f"（{p['n']} 题）")
    got = [b for b in d["badges"] if b["got"]]
    if got:
        lines += ["", "四、已获得徽章", "-" * 40]
        for b in got:
            lines.append(f"  {b['name']} —— {b['desc']}")
    lines += ["", "仅供参考，不构成法律意见。"]
    return "\n".join(lines) + "\n"


def _paper_report_text(store: StudyStore, uid: int, pid: str,
                       paper_id: str) -> str:
    """单张卷子的评估报告（.txt）。判分口径与页面完全一致。"""
    p, _ = profile_for(pid)
    detail = paper_detail(store, uid, p.id, paper_id)
    if not detail or not detail.get("submitted"):
        return "这张卷子还没有交卷，暂时没有报告。\n"
    r = detail["report"]
    mm, ss = divmod(int(r.get("elapsed") or 0), 60)
    lines = ["自主学习 · 卷面评估报告", "=" * 40,
             f"生成日期：{date.today().isoformat()}",
             f"法典：{p.name}",
             f"卷面得分：{r.get('score')} 分"
             f"（答对 {r.get('right')}/{r.get('total')} 题）",
             f"用时：{mm} 分 {ss} 秒",
             f"未作答：{r.get('unanswered', 0)} 题", "",
             "一、题型得分", "-" * 40]
    for t in r.get("by_type") or []:
        lines.append(f"  {t['name']}：{t['right']}/{t['total']} 题")
    if r.get("chapters"):
        lines += ["", "二、章节分布", "-" * 40]
        for c in r["chapters"]:
            lines.append(f"  《{c['law']}》{c['chapter'] or '未分章'}："
                         f"{c['right']}/{c['total']}（正确率 {c['rate']}%）")
    lines += ["", "三、逐题回顾", "-" * 40]
    _, idx = profile_for(p.id)          # 条号只从库里取，不从 id 猜
    for i, it in enumerate(detail["items"], 1):
        flag = {1: "✔", 0: "✘"}.get(it["correct_flag"], "—")
        mine = it.get("my_answer") or "（未作答）"
        right = it.get("answer") or ""
        if it.get("qtype") == "judge":
            right = "对" if right == "A" else "错"
        art = idx.by_id.get(str(it.get("article_id") or "")) or {}
        no = art.get("article_no_cn") or ""
        lines.append(f"  {i}. {flag} {str(it['question']).splitlines()[0][:50]}")
        lines.append(f"     你的答案：{mine}    正确答案：{right}")
        lines.append(f"     依据：《{it['law_short']}》{no or it.get('article_id')}")
    if r.get("next"):
        lines += ["", "四、下一步", "-" * 40, f"  {r['next']}"]
    lines += ["", "仅供参考，不构成法律意见。"]
    return "\n".join(lines) + "\n"
