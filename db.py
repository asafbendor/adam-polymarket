"""
Database abstraction - SQLite locally, PostgreSQL on Railway.
When DATABASE_URL is set, uses psycopg2. Otherwise uses sqlite3.
"""
import os
import sqlite3
from contextlib import contextmanager

# Railway provides "postgres://" but psycopg2 needs "postgresql://"
_raw_url = os.getenv("DATABASE_URL", "")
DATABASE_URL = _raw_url.replace("postgres://", "postgresql://", 1) if _raw_url.startswith("postgres://") else _raw_url
USE_PG = bool(DATABASE_URL and DATABASE_URL.startswith("postgresql"))
PH = "%s" if USE_PG else "?"   # placeholder char


@contextmanager
def conn():
    if USE_PG:
        import psycopg2
        import psycopg2.extras
        c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        db_path = os.getenv("DB_PATH", "adam.db")
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def rows(cursor) -> list[dict]:
    """Fetch all rows as list of dicts (works for both sqlite3 and psycopg2)."""
    fetched = cursor.fetchall()
    if not fetched:
        return []
    if isinstance(fetched[0], dict):          # psycopg2 RealDictRow
        return [dict(r) for r in fetched]
    return [dict(r) for r in fetched]          # sqlite3.Row also supports dict()


def one(cursor) -> dict | None:
    r = cursor.fetchone()
    if r is None:
        return None
    return dict(r)


def init_tables(ddl_statements: list[str]):
    """Run CREATE TABLE IF NOT EXISTS statements."""
    with conn() as c:
        cur = c.cursor()
        for sql in ddl_statements:
            cur.execute(sql)


def q(sql: str) -> str:
    """Replace ? with %s for postgres if needed."""
    if USE_PG:
        return sql.replace("?", "%s")
    return sql
