"""
Database layer — raw SQL via sqlite3.
Schema is intentionally Postgres-compatible: no SQLite-isms.
To migrate: swap the connection factory for psycopg2/asyncpg and the
placeholder style from ? to %s. Column types are all standard SQL.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Resolved at import time from env; defaults to ./fundscan.db (cwd when running
# `uvicorn fundscan.app:app` from the repo root, which is writable).
# Override in production via DB_PATH env var or by calling init_db(path=...).
DB_PATH = Path(os.getenv("DB_PATH", "fundscan.db"))

DDL = """
CREATE TABLE IF NOT EXISTS funding_snapshots (
    id          INTEGER PRIMARY KEY,       -- SERIAL in Postgres
    ts          TEXT    NOT NULL,          -- ISO-8601 UTC; use TIMESTAMPTZ in Postgres
    exchange    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    rate_8h     REAL    NOT NULL,
    net_apy     REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts
    ON funding_snapshots (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    email       TEXT    NOT NULL UNIQUE,
    tier        TEXT    NOT NULL DEFAULT 'free',   -- 'free' | 'pro'
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS magic_tokens (
    token       TEXT    PRIMARY KEY,
    email       TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0          -- BOOLEAN
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id          INTEGER PRIMARY KEY,
    received_at TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    payload     TEXT    NOT NULL                    -- JSON blob
);

CREATE TABLE IF NOT EXISTS alert_configs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol      TEXT,                              -- NULL = any symbol
    min_net_apy REAL    NOT NULL,
    telegram_chat_id TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    alerted_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_log_user_symbol
    ON alert_log (user_id, symbol, alerted_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol      TEXT    NOT NULL,
    exchange    TEXT    NOT NULL,
    added_at    TEXT    NOT NULL,
    UNIQUE(user_id, symbol, exchange)
);

CREATE TABLE IF NOT EXISTS competitor_snapshots (
    id          INTEGER PRIMARY KEY,
    url         TEXT    NOT NULL,
    content_hash TEXT   NOT NULL,
    checked_at  TEXT    NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Optional[Path] = None) -> None:
    global DB_PATH
    if path:
        DB_PATH = path
    with get_conn() as conn:
        conn.executescript(DDL)
    log.info("DB initialised at %s", DB_PATH)


def insert_snapshots(rows: list[dict]) -> None:
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO funding_snapshots (ts, exchange, symbol, rate_8h, net_apy)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (r["fetched_at"], r["exchange"], r["symbol"], r["rate_8h"], r["net_apy"])
                for r in rows
            ],
        )


def query_latest() -> list[sqlite3.Row]:
    """Most recent snapshot per (exchange, symbol)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT s.*
            FROM funding_snapshots s
            INNER JOIN (
                SELECT exchange, symbol, MAX(ts) AS max_ts
                FROM funding_snapshots
                GROUP BY exchange, symbol
            ) latest ON s.exchange = latest.exchange
                     AND s.symbol  = latest.symbol
                     AND s.ts      = latest.max_ts
            ORDER BY s.net_apy DESC
            """
        ).fetchall()


def query_delayed(delay_minutes: int = 10) -> list[sqlite3.Row]:
    """
    Most recent snapshot per (exchange, symbol) that is at least
    `delay_minutes` old — used for the free tier.
    Returns [] if no qualifying snapshots exist yet.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT s.*
            FROM funding_snapshots s
            INNER JOIN (
                SELECT exchange, symbol, MAX(ts) AS max_ts
                FROM funding_snapshots
                WHERE ts <= datetime('now', ?)
                GROUP BY exchange, symbol
            ) latest ON s.exchange = latest.exchange
                     AND s.symbol  = latest.symbol
                     AND s.ts      = latest.max_ts
            ORDER BY s.net_apy DESC
            """,
            (f"-{delay_minutes} minutes",),
        ).fetchall()


def query_history(symbol: str, days: int = 7) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT ts, exchange, rate_8h, net_apy
            FROM funding_snapshots
            WHERE symbol = ?
              AND ts >= datetime('now', ?)
            ORDER BY ts ASC
            """,
            (symbol, f"-{days} days"),
        ).fetchall()
