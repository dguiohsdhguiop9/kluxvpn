"""База данных KlyxVPN (SQLite).

На Railway база должна лежать на Volume (DB_PATH=/data/klyx.db),
иначе при каждом перезапуске она будет стираться.
"""
import os
import sqlite3
import time

DB_PATH = os.getenv("DB_PATH", "/data/klyx.db")
if os.path.dirname(DB_PATH) and not os.path.isdir(os.path.dirname(DB_PATH)):
    # Volume не подключён — работаем в текущей папке (данные НЕ сохранятся при перезапуске)
    DB_PATH = "klyx.db"

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY,
        username    TEXT,
        name        TEXT,
        created_at  INTEGER,
        referrer    INTEGER,
        uuid        TEXT,
        expires_at  INTEGER DEFAULT 0,
        trial_used  INTEGER DEFAULT 0,
        bonus_days  INTEGER DEFAULT 0,
        ref_paid    INTEGER DEFAULT 0,
        notified    INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS payments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER,
        plan        TEXT,
        amount      INTEGER,
        currency    TEXT,
        charge_id   TEXT,
        created_at  INTEGER,
        refunded    INTEGER DEFAULT 0
    );
    """
)
_conn.commit()


def now() -> int:
    return int(time.time())


def get_user(uid: int):
    return _conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def upsert_user(uid: int, username: str | None, name: str | None, referrer: int | None = None) -> bool:
    """Создаёт пользователя, если его нет. Возвращает True, если пользователь новый."""
    if get_user(uid):
        _conn.execute("UPDATE users SET username=?, name=? WHERE id=?", (username, name, uid))
        _conn.commit()
        return False
    if referrer == uid or (referrer and not get_user(referrer)):
        referrer = None
    _conn.execute(
        "INSERT INTO users (id, username, name, created_at, referrer) VALUES (?,?,?,?,?)",
        (uid, username, name, now(), referrer),
    )
    _conn.commit()
    return True


def set_uuid(uid: int, uuid: str):
    _conn.execute("UPDATE users SET uuid=? WHERE id=?", (uuid, uid))
    _conn.commit()


def set_expires(uid: int, expires_at: int):
    _conn.execute("UPDATE users SET expires_at=?, notified=0 WHERE id=?", (expires_at, uid))
    _conn.commit()


def mark(uid: int, field: str, value=1):
    assert field in {"trial_used", "ref_paid", "notified"}
    _conn.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
    _conn.commit()


def add_bonus_days(uid: int, days: int):
    _conn.execute("UPDATE users SET bonus_days=bonus_days+? WHERE id=?", (days, uid))
    _conn.commit()


def count_invited(uid: int) -> int:
    return _conn.execute("SELECT COUNT(*) FROM users WHERE referrer=?", (uid,)).fetchone()[0]


def add_payment(uid: int, plan: str, amount: int, currency: str, charge_id: str):
    _conn.execute(
        "INSERT INTO payments (user_id, plan, amount, currency, charge_id, created_at) VALUES (?,?,?,?,?,?)",
        (uid, plan, amount, currency, charge_id, now()),
    )
    _conn.commit()


def payment_exists(charge_id: str) -> bool:
    return _conn.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone() is not None


def mark_refunded(charge_id: str):
    _conn.execute("UPDATE payments SET refunded=1 WHERE charge_id=?", (charge_id,))
    _conn.commit()


def paid_count(uid: int) -> int:
    return _conn.execute("SELECT COUNT(*) FROM payments WHERE user_id=? AND refunded=0", (uid,)).fetchone()[0]


def stats() -> dict:
    t = now()
    day = t - 86400
    q = lambda sql, *a: _conn.execute(sql, a).fetchone()[0] or 0
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "users_today": q("SELECT COUNT(*) FROM users WHERE created_at>?", day),
        "active": q("SELECT COUNT(*) FROM users WHERE expires_at>?", t),
        "payments": q("SELECT COUNT(*) FROM payments WHERE refunded=0"),
        "stars": q("SELECT SUM(amount) FROM payments WHERE refunded=0 AND currency='XTR'"),
        "stars_today": q("SELECT SUM(amount) FROM payments WHERE refunded=0 AND currency='XTR' AND created_at>?", day),
        "trials": q("SELECT COUNT(*) FROM users WHERE trial_used=1"),
    }


def last_payments(limit: int = 10):
    return _conn.execute(
        "SELECT p.*, u.username, u.name FROM payments p LEFT JOIN users u ON u.id=p.user_id "
        "ORDER BY p.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def all_user_ids():
    return [r[0] for r in _conn.execute("SELECT id FROM users").fetchall()]


def expiring_soon(within_sec: int):
    t = now()
    return _conn.execute(
        "SELECT * FROM users WHERE expires_at>? AND expires_at<? AND notified=0",
        (t, t + within_sec),
    ).fetchall()


def is_persistent() -> bool:
    return DB_PATH != "klyx.db"
