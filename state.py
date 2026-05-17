"""
State - persistent storage for bets and daily stats.
Uses PostgreSQL on Railway (DATABASE_URL), SQLite locally.
"""
from datetime import date, datetime, timezone
from typing import Optional
import db

DB_PATH = "adam.db"   # kept for backward compat - db.py reads DB_PATH env


def init(path: str = DB_PATH):
    db.init_tables([
        """CREATE TABLE IF NOT EXISTS bets (
            id             SERIAL PRIMARY KEY,
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
        )""" if db.USE_PG else
        """CREATE TABLE IF NOT EXISTS bets (
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
        )""",
        """CREATE TABLE IF NOT EXISTS daily_stats (
            date           TEXT PRIMARY KEY,
            spent          REAL NOT NULL DEFAULT 0,
            realized_pnl   REAL NOT NULL DEFAULT 0,
            bets_placed    INTEGER NOT NULL DEFAULT 0,
            bets_won       INTEGER NOT NULL DEFAULT 0,
            bets_lost      INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS log (
            id      SERIAL PRIMARY KEY,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            message TEXT NOT NULL
        )""" if db.USE_PG else
        """CREATE TABLE IF NOT EXISTS log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            message TEXT NOT NULL
        )""",
    ])


def today() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── bets ─────────────────────────────────────────────────────────────

def insert_bet(b: dict, path: str = DB_PATH) -> int:
    sql = db.q("""INSERT INTO bets
        (condition_id,question,direction,market_price,estimated_prob,
         reason,bet_size,limit_price,order_id,token_id,status,end_date,market_url,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""")
    if db.USE_PG:
        sql += " RETURNING id"
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(sql, (
            b["condition_id"], b["question"], b["direction"],
            b["market_price"], b["estimated_prob"], b.get("reason",""),
            b.get("bet_size",1.0), b["limit_price"],
            b.get("order_id",""), b.get("token_id",""),
            b.get("status","pending"),
            b.get("end_date",""), b.get("market_url",""),
            now_iso(),
        ))
        if db.USE_PG:
            return cur.fetchone()["id"]
        return cur.lastrowid


def update_bet(bet_id: int, fields: dict, path: str = DB_PATH):
    parts = ", ".join(f"{k}={db.PH}" for k in fields)
    sql = db.q(f"UPDATE bets SET {parts} WHERE id={db.PH}")
    with db.conn() as c:
        c.cursor().execute(sql, list(fields.values()) + [bet_id])


def get_pending(path: str = DB_PATH) -> list[dict]:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM bets WHERE status IN ('pending','filled')")
        return db.rows(cur)


def already_bet(condition_id: str, path: str = DB_PATH) -> bool:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q(
            "SELECT id FROM bets WHERE condition_id=? AND status IN ('pending','filled')"),
            (condition_id,))
        return cur.fetchone() is not None


def all_bets(limit=100, path: str = DB_PATH) -> list[dict]:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT * FROM bets ORDER BY created_at DESC LIMIT ?"), (limit,))
        return db.rows(cur)


# ── daily stats ───────────────────────────────────────────────────────

def get_daily(day: Optional[str] = None, path: str = DB_PATH) -> dict:
    day = day or today()
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT * FROM daily_stats WHERE date=?"), (day,))
        r = db.one(cur)
        return r if r else {"date": day, "spent": 0, "realized_pnl": 0,
                            "bets_placed": 0, "bets_won": 0, "bets_lost": 0}


def add_daily(day: str, field: str, delta: float, path: str = DB_PATH):
    s = get_daily(day, path)
    s[field] = s.get(field, 0) + delta
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("""INSERT INTO daily_stats
            (date,spent,realized_pnl,bets_placed,bets_won,bets_lost)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
              spent=EXCLUDED.spent, realized_pnl=EXCLUDED.realized_pnl,
              bets_placed=EXCLUDED.bets_placed, bets_won=EXCLUDED.bets_won,
              bets_lost=EXCLUDED.bets_lost"""),
            (s["date"], s["spent"], s["realized_pnl"],
             s["bets_placed"], s["bets_won"], s["bets_lost"]))


def all_time(path: str = DB_PATH) -> dict:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute("""SELECT COALESCE(SUM(spent),0) total_spent,
            COALESCE(SUM(realized_pnl),0) total_pnl,
            COALESCE(SUM(bets_placed),0) total_bets,
            COALESCE(SUM(bets_won),0) total_won,
            COALESCE(SUM(bets_lost),0) total_lost
            FROM daily_stats""")
        r = db.one(cur)
        return r if r else {}


# ── log ───────────────────────────────────────────────────────────────

def log(level: str, message: str, path: str = DB_PATH):
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("INSERT INTO log (ts,level,message) VALUES (?,?,?)"),
                    (now_iso(), level, message))
        cur.execute("DELETE FROM log WHERE id NOT IN "
                    "(SELECT id FROM log ORDER BY id DESC LIMIT 300)")


def get_log(limit=50, path: str = DB_PATH) -> list[dict]:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT * FROM log ORDER BY id DESC LIMIT ?"), (limit,))
        return db.rows(cur)
