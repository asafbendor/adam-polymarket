"""
Persistent memory for agents.
Uses PostgreSQL on Railway, SQLite locally.
"""
from datetime import datetime, timezone
import db


def init():
    db.init_tables([
        """CREATE TABLE IF NOT EXISTS memory (
            agent   TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            updated TEXT NOT NULL,
            PRIMARY KEY (agent, key)
        )""",
        """CREATE TABLE IF NOT EXISTS bet_outcomes (
            id             SERIAL PRIMARY KEY,
            question       TEXT,
            direction      TEXT,
            estimated_prob REAL,
            market_price   REAL,
            outcome        TEXT,
            pnl            REAL,
            scout_reason   TEXT,
            created_at     TEXT
        )""" if db.USE_PG else
        """CREATE TABLE IF NOT EXISTS bet_outcomes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            question       TEXT,
            direction      TEXT,
            estimated_prob REAL,
            market_price   REAL,
            outcome        TEXT,
            pnl            REAL,
            scout_reason   TEXT,
            created_at     TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS agent_log (
            id      SERIAL PRIMARY KEY,
            agent   TEXT,
            ts      TEXT,
            message TEXT
        )""" if db.USE_PG else
        """CREATE TABLE IF NOT EXISTS agent_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            agent   TEXT,
            ts      TEXT,
            message TEXT
        )""",
    ])


def remember(agent: str, key: str, value: str):
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("""INSERT INTO memory (agent,key,value,updated)
            VALUES (?,?,?,?)
            ON CONFLICT(agent,key) DO UPDATE SET value=EXCLUDED.value, updated=EXCLUDED.updated"""),
            (agent, key, value, datetime.now(timezone.utc).isoformat()))


def recall(agent: str, key: str) -> str | None:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT value FROM memory WHERE agent=? AND key=?"), (agent, key))
        r = db.one(cur)
        return r["value"] if r else None


def recall_all(agent: str) -> dict:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT key,value FROM memory WHERE agent=? ORDER BY rowid DESC LIMIT 20"),
                    (agent,))
        return {r["key"]: r["value"] for r in db.rows(cur)}


def record_outcome(question, direction, estimated_prob, market_price,
                   outcome, pnl, reason):
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("""INSERT INTO bet_outcomes
            (question,direction,estimated_prob,market_price,outcome,pnl,scout_reason,created_at)
            VALUES (?,?,?,?,?,?,?,?)"""),
            (question, direction, estimated_prob, market_price, outcome, pnl, reason,
             datetime.now(timezone.utc).isoformat()))


def get_outcomes(limit=50) -> list[dict]:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("SELECT * FROM bet_outcomes ORDER BY id DESC LIMIT ?"), (limit,))
        return db.rows(cur)


def agent_log(agent: str, message: str):
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q("INSERT INTO agent_log (agent,ts,message) VALUES (?,?,?)"),
                    (agent, datetime.now(timezone.utc).isoformat(), message))
        cur.execute(db.q(
            "DELETE FROM agent_log WHERE id NOT IN "
            "(SELECT id FROM agent_log ORDER BY id DESC LIMIT 500)"))


def get_log_entries(agent: str, limit=20) -> list[dict]:
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(db.q(
            "SELECT ts,message FROM agent_log WHERE agent=? ORDER BY id DESC LIMIT ?"),
            (agent, limit))
        return db.rows(cur)
