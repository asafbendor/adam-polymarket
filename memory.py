"""
Persistent memory for agents.
Each agent has a key-value store for learnings, fixes, and observations.
"""
import json
import sqlite3
from datetime import datetime, timezone

DB = "adam.db"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS memory (
                agent   TEXT NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT NOT NULL,
                updated TEXT NOT NULL,
                PRIMARY KEY (agent, key)
            );
            CREATE TABLE IF NOT EXISTS bet_outcomes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question     TEXT,
                direction    TEXT,
                estimated_prob REAL,
                market_price REAL,
                outcome      TEXT,
                pnl          REAL,
                scout_reason TEXT,
                created_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                agent   TEXT,
                ts      TEXT,
                message TEXT
            );
        """)


def remember(agent: str, key: str, value: str):
    """Store a learning or fix for an agent."""
    with _conn() as c:
        c.execute("""INSERT INTO memory (agent,key,value,updated) VALUES (?,?,?,?)
            ON CONFLICT(agent,key) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
            (agent, key, value, datetime.now(timezone.utc).isoformat()))


def recall(agent: str, key: str) -> str | None:
    """Retrieve a stored learning."""
    with _conn() as c:
        r = c.execute("SELECT value FROM memory WHERE agent=? AND key=?", (agent, key)).fetchone()
        return r["value"] if r else None


def recall_all(agent: str) -> dict:
    """Get all memories for an agent."""
    with _conn() as c:
        rows = c.execute("SELECT key,value FROM memory WHERE agent=?", (agent,)).fetchall()
        return {r["key"]: r["value"] for r in rows}


def record_outcome(question: str, direction: str, estimated_prob: float,
                    market_price: float, outcome: str, pnl: float, reason: str):
    """Record the outcome of a bet for Scout to learn from."""
    with _conn() as c:
        c.execute("""INSERT INTO bet_outcomes
            (question,direction,estimated_prob,market_price,outcome,pnl,scout_reason,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (question, direction, estimated_prob, market_price, outcome, pnl, reason,
             datetime.now(timezone.utc).isoformat()))


def get_outcomes(limit=50) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM bet_outcomes ORDER BY id DESC LIMIT ?", (limit,))]


def agent_log(agent: str, message: str):
    with _conn() as c:
        c.execute("INSERT INTO agent_log (agent,ts,message) VALUES (?,?,?)",
                  (agent, datetime.now(timezone.utc).isoformat(), message))
        c.execute("DELETE FROM agent_log WHERE id NOT IN "
                  "(SELECT id FROM agent_log ORDER BY id DESC LIMIT 500)")
