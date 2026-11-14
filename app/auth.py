# -*- coding: utf-8 -*-
"""
账号与认证。零依赖：只用标准库 hashlib / secrets / hmac / sqlite3。

三个设计决定，都不是随手选的：

1. 密码用 PBKDF2-HMAC-SHA256 慢慢算，而不是「sha256(密码+盐)」一次哈希。
   单轮 sha256 对离线爆破毫无抵抗力——一张消费级显卡每秒几十亿次，8 位
   字母数字口令几小时就撞出来。PBKDF2 把单次验证抬到近 200ms，同样的字典
   就要跑几十年。迭代次数写在哈希字符串里，以后调高不影响老账号登录。

2. 令牌只存 sha256，不存原文。库被拖走时拿到一堆哈希，没法直接当 cookie 用。
   这也是「令牌必须存表、不能签 JWT」的原因：只有服务端握有记录，封号和
   改密才能**立刻**让旧令牌失效；JWT 只能等它自己过期。

3. 限流按两个维度分开记。只按账号锁，攻击者换账号继续试；只按 IP 锁，
   攻击者换代理继续试。两个都记，任一触发就拒。

一个刻意的取舍：登录失败对外只说「用户名或密码不正确」，不区分「没这个
人」和「密码错了」。区分开来等于送一个用户名枚举接口。但内部 login_log
记的是真实原因（no_user / bad_password / locked），管理员排查时看得见。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import (LOGIN_FAIL_ACCOUNT, LOGIN_FAIL_IP, LOGIN_IP_WINDOW_MIN,
                     LOGIN_LOCK_MIN, PBKDF2_ITER, PWD_MAX, PWD_MIN,
                     REG_PER_IP_HOUR, TOKEN_TTL_HOURS)
from .dbutil import connect as _db_connect

# 用户名：2~32 位，中文/英文/数字/下划线/点/连字符。
# 下限取 2 而不是 3：「张三」「李四」这种两个字的中文名太常见，按英文习惯
# 定 3 位会直接把一批真实用户挡在门外。代价是「ab」也能注册——但这只是个
# 显示名，重名会被 UNIQUE 拒掉，不构成安全问题。
# 不用 \w 之外的花样，是为了它出现在 URL、日志、CSV 里都不用转义。
_USERNAME_RE = re.compile(r"^[\w.\-]{2,32}$")

_ALGO = "pbkdf2_sha256"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL COLLATE NOCASE UNIQUE,
    pass_hash     TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    REAL NOT NULL,
    created_ip    TEXT NOT NULL DEFAULT '',
    last_login_at REAL,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    ua         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_token_uid ON auth_tokens(user_id);
CREATE TABLE IF NOT EXISTS login_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    user_id  INTEGER,
    ip       TEXT NOT NULL DEFAULT '',
    ok       INTEGER NOT NULL,
    reason   TEXT NOT NULL DEFAULT '',
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_ts ON login_log(ts);
CREATE INDEX IF NOT EXISTS idx_login_ip ON login_log(ip, ts);
CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    admin_name  TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON admin_audit(ts);
-- 个人档案：一个账号一行，跨任务共享。
-- 为什么单独一张表而不是给 users 加列：users 是认证域，任何一次
-- SELECT * 都可能把身份证号带进日志或接口返回值（_pub 只挑白名单，但
-- 别人写查询时未必记得）。档案放在独立表里，要读必须显式来取。
-- onboarding 三态：pending（还没弹过）/ done（填过）/ skipped（选了仅咨询）。
CREATE TABLE IF NOT EXISTS profiles (
    user_id           INTEGER PRIMARY KEY,
    real_name         TEXT NOT NULL DEFAULT '',
    id_card           TEXT NOT NULL DEFAULT '',
    phone             TEXT NOT NULL DEFAULT '',
    address           TEXT NOT NULL DEFAULT '',
    region            TEXT NOT NULL DEFAULT '',
    company_name      TEXT NOT NULL DEFAULT '',
    company_code      TEXT NOT NULL DEFAULT '',
    company_address   TEXT NOT NULL DEFAULT '',
    company_legal_rep TEXT NOT NULL DEFAULT '',
    commission        TEXT NOT NULL DEFAULT '',
    onboarding        TEXT NOT NULL DEFAULT 'pending',
    updated_at        REAL NOT NULL DEFAULT 0
);
"""


class AuthError(Exception):
    """带机器可读 code 的业务错误。server 层按 code 映射 HTTP 状态码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 密码

def hash_password(password: str, *, salt: str | None = None,
                  iterations: int | None = None) -> str:
    """返回 `pbkdf2_sha256$迭代次数$盐$哈希`。

    盐和迭代次数一起放进同一个字符串，而不是分列存储：这个格式自带版本，
    以后换算法或调迭代次数，老账号的原串仍然可验，不用写迁移脚本。
    """
    iterations = iterations or PBKDF2_ITER
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), iterations)
    return f"{_ALGO}${iterations}${salt}${dk.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        algo, it_s, salt, want = stored.split("$", 3)
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(it_s))
    except (ValueError, TypeError):
        return False
    # 恒定时间比较。普通 == 会在第一个不同字节处返回，理论上能按耗时逐字节猜。
    return hmac.compare_digest(dk.hex(), want)


def check_password(password: str) -> None:
    if not isinstance(password, str):
        raise AuthError("weak_password", "密码格式不正确")
    if len(password) < PWD_MIN:
        raise AuthError("weak_password", f"密码至少 {PWD_MIN} 位")
    # 上限不是洁癖：PBKDF2 对超长输入不是免费的，有人拿 1MB 字符串
    # 打登录接口就是一次极低成本的 DoS。
    if len(password) > PWD_MAX:
        raise AuthError("weak_password", f"密码不能超过 {PWD_MAX} 位")
    kinds = (any(c.isalpha() for c in password)
             + any(c.isdigit() for c in password)
             + any(not c.isalnum() for c in password))
    if kinds < 2:
        raise AuthError("weak_password", "密码要同时包含字母和数字（或符号）")


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise AuthError("bad_username",
                        "用户名 2~32 位，可用中英文、数字、下划线、点、连字符")
    # 纯数字用户名没有任何识别价值，而且在列表里容易和用户 id 看混。
    if username.isdigit():
        raise AuthError("bad_username", "用户名不能全是数字")
    return username


def token_key(raw: str) -> str:
    """令牌在库里只以哈希形式存在。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pub(row: sqlite3.Row) -> dict[str, Any]:
    """对外可见的用户字段。不含 pass_hash / fail_count / locked_until ——
    这几个字段一旦进了接口返回值，迟早会被前端展示或被日志打出来。"""
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "status": row["status"], "created_at": row["created_at"],
            "last_login_at": row["last_login_at"]}


# 档案字段：名 → (中文标签, 最大长度)。
# 标签给前端和申请书共用（两处各写一份迟早会不一致）；长度上限防的是
# 「把整段聊天记录粘进姓名栏」，不是严格格式校验——身份证 15 位旧号和
# 18 位新号都得收，为一个人填错格式就拦下整份文书不值得。
PROFILE_FIELDS: dict[str, tuple[str, int]] = {
    "real_name": ("姓名", 30),
    "id_card": ("身份证号", 18),
    "phone": ("联系电话", 24),
    "address": ("住址", 120),
    "region": ("所在地区", 40),
    "company_name": ("单位全称", 80),
    "company_code": ("统一社会信用代码", 18),
    "company_address": ("单位住所", 120),
    "company_legal_rep": ("法定代表人", 30),
    "commission": ("仲裁委名称", 60),
}

PROFILE_ONBOARDING = ("pending", "done", "skipped")


def mask_id(s: str | None) -> str:
    """身份证脱敏：只给前 6 后 4。

    后台审计、日志、任何「不需要真号」的地方都走这个。申请书生成是唯一
    需要全号的场景——那也是当事人自己的机器、自己的文件。
    """
    t = (s or "").strip()
    if not t:
        return ""
    if len(t) <= 10:
        return "*" * len(t)
    return f"{t[:6]}{'*' * (len(t) - 10)}{t[-4:]}"


# ------------------------------------------------------------------ 存储

class AuthStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 建连 + 切 WAL 放在 dbutil 里，并对「文件刚被创建」时的瞬时占用做重试。
        # 交付包解压后第一次跑，撞过一次 `PRAGMA journal_mode=WAL` 报 disk I/O
        # error——文件正被杀毒/索引服务扫，而这条 PRAGMA 是第一个写操作。
        # 详见 app/dbutil.py 顶部。
        self._conn = _db_connect(self.path, timeout=10)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------------- 查用户 ----------------
    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM users WHERE id=?",
                                   (user_id,)).fetchone()
        return _pub(r) if r else None

    def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM users WHERE username=?",
                                   ((username or "").strip(),)).fetchone()
        return _pub(r) if r else None

    # ---------------- 个人档案 ----------------
    def get_profile(self, user_id: int, *, mask: bool = False) -> dict[str, Any]:
        """读档案。没有这一行时返回全空 + onboarding='pending'，
        前端据此弹首次填写——不要用「行不存在」当判据，前端拿不到这个信息。"""
        with self._lock:
            r = self._conn.execute("SELECT * FROM profiles WHERE user_id=?",
                                   (int(user_id),)).fetchone()
        out: dict[str, Any] = {k: "" for k in PROFILE_FIELDS}
        out["onboarding"] = "pending"
        out["updated_at"] = 0.0
        if r:
            for k in PROFILE_FIELDS:
                out[k] = r[k] or ""
            out["onboarding"] = r["onboarding"] or "pending"
            out["updated_at"] = float(r["updated_at"] or 0)
        if mask:
            out["id_card"] = mask_id(out["id_card"])
        # 哪些字段已有值，让前端一眼看出还差什么，不用自己比对空串
        out["filled"] = [k for k in PROFILE_FIELDS if out[k]]
        return out

    def save_profile(self, user_id: int, patch: dict[str, Any],
                     *, onboarding: str = "done") -> dict[str, Any]:
        """按 patch 更新档案；patch 里没出现的键保持原值。

        为什么是「部分更新」而不是整行覆盖：设置页一次只改一栏，整行覆盖
        会把它没渲染的字段清空。但跳过引导时传的是空 patch，那时语义就是
        「只改状态」，也一样成立。
        """
        if onboarding not in PROFILE_ONBOARDING:
            raise AuthError("bad_onboarding", "档案状态不合法")
        clean: dict[str, str] = {}
        for k, v in (patch or {}).items():
            if k not in PROFILE_FIELDS:
                raise AuthError("bad_field", f"未知档案字段：{k}")
            label, limit = PROFILE_FIELDS[k]
            txt = "" if v is None else str(v).strip()
            if len(txt) > limit:
                raise AuthError("too_long", f"{label}最多 {limit} 个字")
            if "\n" in txt or "\r" in txt:
                raise AuthError("bad_newline", f"{label}不能换行")
            clean[k] = txt

        cur = self.get_profile(int(user_id))
        vals = {k: clean.get(k, cur[k]) for k in PROFILE_FIELDS}
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO profiles(user_id, real_name, id_card, phone, address,"
                " region, company_name, company_code, company_address,"
                " company_legal_rep, commission, onboarding, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " real_name=excluded.real_name, id_card=excluded.id_card,"
                " phone=excluded.phone, address=excluded.address,"
                " region=excluded.region, company_name=excluded.company_name,"
                " company_code=excluded.company_code,"
                " company_address=excluded.company_address,"
                " company_legal_rep=excluded.company_legal_rep,"
                " commission=excluded.commission,"
                " onboarding=excluded.onboarding, updated_at=excluded.updated_at",
                (int(user_id), vals["real_name"], vals["id_card"], vals["phone"],
                 vals["address"], vals["region"], vals["company_name"],
                 vals["company_code"], vals["company_address"],
                 vals["company_legal_rep"], vals["commission"], onboarding, now))
            self._conn.commit()
        return self.get_profile(int(user_id))

    def skip_profile(self, user_id: int) -> dict[str, Any]:
        """选了「先跳过，仅咨询」。只改状态，不碰已有内容——
        用户以后从设置里补填时，之前填过的字不该被这次跳过抹掉。"""
        return self.save_profile(int(user_id), {}, onboarding="skipped")

    # ---------------- 建号 ----------------
    def create_user(self, username: str, password: str, role: str = "user",
                    ip: str = "") -> dict[str, Any]:
        username = check_username(username)
        check_password(password)
        if role not in ("user", "admin"):
            raise AuthError("bad_role", "角色只能是 user 或 admin")
        now = time.time()
        with self._lock:
            # 进程内的锁挡不住多进程同时注册，最终靠 UNIQUE 约束。
            dup = self._conn.execute("SELECT 1 FROM users WHERE username=?",
                                     (username,)).fetchone()
            if dup:
                raise AuthError("user_exists", "这个用户名已经被注册了")
            cur = self._conn.execute(
                "INSERT INTO users(username, pass_hash, role, status,"
                " created_at, created_ip) VALUES(?,?,?,'active',?,?)",
                (username, hash_password(password), role, now, (ip or "")[:64]))
            self._conn.commit()
            uid = int(cur.lastrowid)
        return self.get_user(uid) or {}

    def count_users(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, status, COUNT(*) c FROM users GROUP BY role, status"
            ).fetchall()
        out: dict[str, int] = {"total": 0, "admin": 0, "user": 0,
                               "active": 0, "banned": 0}
        for r in rows:
            c = int(r["c"])
            out["total"] += c
            out[r["role"]] = out.get(r["role"], 0) + c
            out[r["status"]] = out.get(r["status"], 0) + c
        return out

    def list_users(self, q: str = "", role: str = "", status: str = "",
                   page: int = 1, size: int = 20) -> dict[str, Any]:
        where, args = [], []
        if q:
            where.append("username LIKE ?")
            args.append(f"%{q}%")
        if role in ("user", "admin"):
            where.append("role=?")
            args.append(role)
        if status in ("active", "banned"):
            where.append("status=?")
            args.append(status)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page or 1))
        size = min(100, max(1, int(size or 20)))
        with self._lock:
            total = int(self._conn.execute(
                f"SELECT COUNT(*) c FROM users {w}", args).fetchone()["c"])
            rows = self._conn.execute(
                f"SELECT * FROM users {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size]).fetchall()
        items = []
        for r in rows:
            d = _pub(r)
            # 锁定状态给管理员看，但不给别的普通用户看 —— 这个接口只有 admin 能进，
            # 所以放在这里安全。
            d["locked_until"] = r["locked_until"]
            d["fail_count"] = r["fail_count"]
            items.append(d)
        return {"total": total, "page": page, "size": size, "items": items}

    def count_registrations(self, ip: str, hours: int = 1) -> int:
        """同 IP 在窗口内建了几个号。开放注册必然招灌水，一张表灌满之后
        管理员面板翻页都翻不动，所以宁可一开始就卡住。"""
        if not ip:
            return 0
        since = time.time() - hours * 3600
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) c FROM users WHERE created_ip=? AND created_at>=?",
                (ip, since)).fetchone()
        return int(r["c"])

    # ---------------- 登录 ----------------
    def _ip_fails(self, ip: str, now: float) -> int:
        if not ip:
            return 0
        since = now - LOGIN_IP_WINDOW_MIN * 60
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) c FROM login_log WHERE ip=? AND ok=0 AND ts>=?",
                (ip, since)).fetchone()
        return int(r["c"])

    def _log(self, username: str, user_id: int | None, ip: str,
             ok: bool, reason: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO login_log(username, user_id, ip, ok, reason, ts)"
                " VALUES(?,?,?,?,?,?)",
                (username[:64], user_id, (ip or "")[:64], 1 if ok else 0,
                 reason, now))
            # 日志表只留 30 天。不限的话它会长成库里最大的一张表，
            # 而管理员真正会看的永远是最近几天。
            if secrets.randbelow(50) == 0:
                self._conn.execute("DELETE FROM login_log WHERE ts < ?",
                                   (now - 30 * 86400,))
            self._conn.commit()

    def login(self, username: str, password: str, ip: str = "",
              ua: str = "") -> tuple[dict[str, Any], str]:
        """成功返回 (用户, 明文令牌)。失败抛 AuthError。"""
        username = (username or "").strip()[:64]
        now = time.time()

        # IP 维度先拦。撞库的特征是「很多账号、少量密码」，账号维度根本看不出来。
        if self._ip_fails(ip, now) >= LOGIN_FAIL_IP:
            self._log(username, None, ip, False, "ip_limited")
            raise AuthError("ip_limited",
                            f"尝试过于频繁，请 {LOGIN_IP_WINDOW_MIN} 分钟后再试")

        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username=?",
                                     (username,)).fetchone()
        if row is None:
            self._log(username, None, ip, False, "no_user")
            raise AuthError("bad_credentials", "用户名或密码不正确")

        uid = int(row["id"])
        if row["status"] != "active":
            self._log(username, uid, ip, False, "banned")
            raise AuthError("banned", "这个账号已被停用，请联系管理员")

        if row["locked_until"] > now:
            left = int((row["locked_until"] - now) / 60) + 1
            self._log(username, uid, ip, False, "locked")
            raise AuthError("locked", f"密码错误次数过多，请 {left} 分钟后再试")

        if not verify_password(row["pass_hash"], password):
            fails = int(row["fail_count"]) + 1
            locked = (now + LOGIN_LOCK_MIN * 60) if fails >= LOGIN_FAIL_ACCOUNT else 0.0
            with self._lock:
                self._conn.execute(
                    "UPDATE users SET fail_count=?, locked_until=? WHERE id=?",
                    (0 if locked else fails, locked, uid))
                self._conn.commit()
            self._log(username, uid, ip, False,
                      "locked" if locked else "bad_password")
            if locked:
                raise AuthError("locked", f"密码错误 {LOGIN_FAIL_ACCOUNT} 次，"
                                          f"账号锁定 {LOGIN_LOCK_MIN} 分钟")
            raise AuthError("bad_credentials", "用户名或密码不正确")

        with self._lock:
            self._conn.execute(
                "UPDATE users SET fail_count=0, locked_until=0, last_login_at=?"
                " WHERE id=?", (now, uid))
            self._conn.commit()
        self._log(username, uid, ip, True, "")
        return self.get_user(uid) or {}, self.issue_token(uid, ip, ua)

    # ---------------- 令牌 ----------------
    def issue_token(self, user_id: int, ip: str = "", ua: str = "") -> str:
        raw = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_tokens(token_hash, user_id, created_at,"
                " expires_at, ip, ua) VALUES(?,?,?,?,?,?)",
                (token_key(raw), user_id, now, now + TOKEN_TTL_HOURS * 3600,
                 (ip or "")[:64], (ua or "")[:200]))
            # 顺手清过期令牌，避免这张表只增不减。放在签发时做，比开定时任务省事。
            self._conn.execute("DELETE FROM auth_tokens WHERE expires_at < ?",
                               (now,))
            self._conn.commit()
        return raw

    def resolve_token(self, raw: str | None) -> dict[str, Any] | None:
        """校验令牌 → 用户。任何一个环节不过都返回 None，调用方一律当未登录。"""
        if not raw:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT t.expires_at, u.* FROM auth_tokens t"
                " JOIN users u ON u.id=t.user_id WHERE t.token_hash=?",
                (token_key(raw),)).fetchone()
        if r is None:
            return None
        if float(r["expires_at"]) < time.time():
            return None
        # 状态在每次请求时重新读库，而不是信任签发时的快照 —— 这样封号
        # 对**下一个**请求就生效，不用等令牌过期。
        if r["status"] != "active":
            return None
        return _pub(r)

    def revoke_token(self, raw: str | None) -> None:
        if not raw:
            return
        with self._lock:
            self._conn.execute("DELETE FROM auth_tokens WHERE token_hash=?",
                               (token_key(raw),))
            self._conn.commit()

    def revoke_user_tokens(self, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM auth_tokens WHERE user_id=?",
                                     (user_id,))
            self._conn.commit()
        return int(cur.rowcount or 0)

    # ---------------- 改密 ----------------
    def change_password(self, user_id: int, old: str, new: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT pass_hash FROM users WHERE id=?",
                                     (user_id,)).fetchone()
        if row is None:
            raise AuthError("no_user", "用户不存在")
        if not verify_password(row["pass_hash"], old):
            raise AuthError("bad_old_password", "原密码不正确")
        check_password(new)
        if old == new:
            raise AuthError("same_password", "新密码不能和原密码一样")
        with self._lock:
            self._conn.execute("UPDATE users SET pass_hash=? WHERE id=?",
                               (hash_password(new), user_id))
            self._conn.commit()
        # 改完本人所有令牌作废，包括当前这个。宁可让用户重登一次，
        # 也不留下「密码改了但旧会话还活着」的窗口。
        self.revoke_user_tokens(user_id)

    def admin_reset_password(self, user_id: int, new: str) -> None:
        check_password(new)
        with self._lock:
            cur = self._conn.execute("UPDATE users SET pass_hash=?, fail_count=0,"
                                     " locked_until=0 WHERE id=?",
                                     (hash_password(new), user_id))
            if cur.rowcount == 0:
                raise AuthError("no_user", "用户不存在")
            self._conn.commit()
        self.revoke_user_tokens(user_id)

    # ---------------- 管理动作 ----------------
    def _active_admins(self, exclude_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM users WHERE role='admin' AND status='active'"
        args: list[Any] = []
        if exclude_id is not None:
            sql += " AND id<>?"
            args.append(exclude_id)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()["c"])

    def _guard_last_admin(self, user_id: int) -> None:
        """最后一个可用管理员既不能停用也不能降级。

        没有这道闸，一次误点就能把整个后台锁死——而且没有任何界面能救回来，
        只能登服务器改库。这类「不可逆的误操作」必须由代码兜住，不能靠
        按钮上写个「确定吗」。
        """
        u = self.get_user(user_id)
        if u and u["role"] == "admin" and self._active_admins(exclude_id=user_id) == 0:
            raise AuthError("last_admin", "这是最后一个管理员，不能停用或降级")

    def set_status(self, user_id: int, status: str) -> None:
        if status not in ("active", "banned"):
            raise AuthError("bad_status", "状态只能是 active 或 banned")
        if status != "active":
            self._guard_last_admin(user_id)
        with self._lock:
            cur = self._conn.execute("UPDATE users SET status=? WHERE id=?",
                                     (status, user_id))
            if cur.rowcount == 0:
                raise AuthError("no_user", "用户不存在")
            self._conn.commit()
        if status != "active":
            # 封号必须立刻踢下线。只改状态不销令牌的话，用户手上的 cookie
            # 还能用 —— resolve_token 虽然会拦，但那条路依赖每次请求都查库，
            # 万一某处漏了校验就是越权。两道一起上。
            self.revoke_user_tokens(user_id)

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ("user", "admin"):
            raise AuthError("bad_role", "角色只能是 user 或 admin")
        if role == "user":
            self._guard_last_admin(user_id)
        with self._lock:
            cur = self._conn.execute("UPDATE users SET role=? WHERE id=?",
                                     (role, user_id))
            if cur.rowcount == 0:
                raise AuthError("no_user", "用户不存在")
            self._conn.commit()

    def unlock(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET fail_count=0, locked_until=0 WHERE id=?",
                (user_id,))
            self._conn.commit()

    # ---------------- 首个管理员 ----------------
    def ensure_admin(self, username: str | None = None,
                     password: str | None = None
                     ) -> tuple[dict[str, Any] | None, str | None]:
        """库里没有可用管理员时建一个。

        返回 (用户, 一次性明文密码)。第二个值只在「密码是系统生成的」时有内容，
        调用方负责打印一次；不落盘、不写日志——写进日志等于把密码永久留档。
        """
        if self._active_admins() > 0:
            return None, None
        username = check_username(username or "admin")
        generated: str | None = None
        if not password:
            # 末尾固定补 "K7#"，保证一定同时含字母/数字/符号，
            # 免得随机串恰好全是字母被 check_password 拒掉。
            password = secrets.token_urlsafe(9) + "K7#"
            generated = password
        if self.get_user_by_name(username):
            # 公开注册先被抢注了 admin 这个名字。不能把那个账号直接提权
            # ——那等于把后台送给起名最快的人。换个名字建。
            username = f"{username}_root"
        user = self.create_user(username, password, role="admin")
        return user, generated

    # ---------------- 日志 ----------------
    def recent_logins(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, user_id, ip, ok, reason, ts FROM login_log"
                " ORDER BY id DESC LIMIT ?", (min(500, int(limit)),)).fetchall()
        return [dict(r) for r in rows]

    def log_admin(self, admin: dict[str, Any], action: str,
                  target_type: str = "", target_id: str | int = "",
                  detail: str = "", ip: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO admin_audit(admin_id, admin_name, action,"
                " target_type, target_id, detail, ip, ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (admin.get("id", 0), admin.get("username", ""), action,
                 target_type, str(target_id), detail[:500], (ip or "")[:64],
                 time.time()))
            self._conn.commit()

    def recent_audits(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
                (min(500, int(limit)),)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
