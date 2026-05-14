"""
State - SQLite persistence for bets and daily stats.
"""
import sqlite3
from datetime import date, datetime, timezone
from typing import Optional

DB_PATH = "adam.db"


def _conn(path: str = DB_PATH):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def init(path: str = DB_PATH):
    with _conn(path) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id   TEXT NOT NULL,
                question       TEXT NOT NULL,
                direction      TEXT NOT NULL,
                market_price   REAL NOT NULL,
                estimated_prob REAL NOT NULL,
                reason         TEXT NOT NULL DEFAULT '',
                bet_size       REAL NOT NULL DEFAULT 1.0,
                limit_price    REAL NOT NULL,
                order_id       TEXT NOT NULL DEFAULT '',
                token_id       TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'pending',
                fill_price     REAL,
                pnl            REAL,
                end_date       TEXT NOT NULL DEFAULT '',
                market_url     TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                date           TEXT PRIMARY KEY,
                spent          REAL NOT NULL DEFAULT 0,
                realized_pnl   REAL NOT NULL DEFAULT 0,
                bets_placed    INTEGER NOT NULL DEFAULT 0,
                bets_won       INTEGER NOT NULL DEFAULT 0,
                bets_lost      INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                level   TEXT NOT NULL,
                message TEXT NOT NULL
            );
        """)


def today() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── bets ─────────────────────────────────────────────────────────────

def insert_bet(b: dict, path: str = DB_PATH) -> int:
    with _conn(path) as c:
        cur = c.execute("""
            INSERT INTO bets (condition_id,question,direction,market_price,estimated_prob,
                reason,bet_size,limit_price,order_id,token_id,status,end_date,market_url,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            b["condition_id"], b["question"], b["direction"],
            b["market_price"], b["estimated_prob"], b.get("reason",""),
            b.get("bet_size",1.0), b["limit_price"],
            b.get("order_id",""), b.get("token_id",""),
            b.get("status","pending"),
            b.get("end_date",""), b.get("market_url",""),
            now_iso(),
        ))
        return cur.lastrowid


def update_bet(bet_id: int, fields: dict, path: str = DB_PATH):
    parts = ", ".join(f"{k}=?" for k in fields)
    with _conn(path) as c:
        c.execute(f"UPDATE bets SET {parts} WHERE id=?", list(fields.values()) + [bet_id])


def get_pending(path: str = DB_PATH) -> list[dict]:
    with _conn(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM bets WHERE status IN ('pending','filled')")]


def already_bet(condition_id: str, path: str = DB_PATH) -> bool:
    with _conn(path) as c:
        return c.execute(
            "SELECT id FROM bets WHERE condition_id=? AND status IN ('pending','filled')",
            (condition_id,)).fetchone() is not None


def all_bets(limit=100, path: str = DB_PATH) -> list[dict]:
    with _conn(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM bets ORDER BY created_at DESC LIMIT ?", (limit,))]


# ── daily stats ───────────────────────────────────────────────────────

def get_daily(day: Optional[str] = None, path: str = DB_PATH) -> dict:
    day = day or today()
    with _conn(path) as c:
        r = c.execute("SELECT * FROM daily_stats WHERE date=?", (day,)).fetchone()
        return dict(r) if r else {"date": day, "spent": 0, "realized_pnl": 0,
                                   "bets_placed": 0, "bets_won": 0, "bets_lost": 0}


def add_daily(day: str, field: str, delta: float, path: str = DB_PATH):
    s = get_daily(day, path)
    s[field] = s.get(field, 0) + delta
    with _conn(path) as c:
        c.execute("""INSERT INTO daily_stats VALUES (:date,:spent,:realized_pnl,
            :bets_placed,:bets_won,:bets_lost)
            ON CONFLICT(date) DO UPDATE SET spent=excluded.spent,
            realized_pnl=excluded.realized_pnl, bets_placed=excluded.bets_placed,
            bets_won=excluded.bets_won, bets_lost=excluded.bets_lost""", s)


def all_time(path: str = DB_PATH) -> dict:
    with _conn(path) as c:
        r = c.execute("""SELECT COALESCE(SUM(spent),0) total_spent,
            COALESCE(SUM(realized_pnl),0) total_pnl,
            COALESCE(SUM(bets_placed),0) total_bets,
            COALESCE(SUM(bets_won),0) total_won,
            COALESCE(SUM(bets_lost),0) total_lost
            FROM daily_stats""").fetchone()
        return dict(r) if r else {}


# ── log ───────────────────────────────────────────────────────────────

def log(level: str, message: str, path: str = DB_PATH):
    with _conn(path) as c:
        c.execute("INSERT INTO log (ts,level,message) VALUES (?,?,?)",
                  (now_iso(), level, message))
        c.execute("DELETE FROM log WHERE id NOT IN (SELECT id FROM log ORDER BY id DESC LIMIT 300)")


def get_log(limit=50, path: str = DB_PATH) -> list[dict]:
    with _conn(path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,))]
