"""
Tests for the date-window queries in fundscan/db.py.

Regression coverage for a real bug: `ts` is stored as Python's ISO-8601
(`T` separator, e.g. "2026-08-06T07:04:53+00:00"), but `datetime('now', ...)`
returns SQLite's own format ("2026-08-06 07:04:53" -- space separator, no
offset). Comparing those two as raw strings is wrong ('T' > ' '
lexicographically), so `ts <= datetime('now', ...)` was false for every row
regardless of actual time -- which is why query_delayed() was dead code
before it got wired into the free tier. These tests insert snapshots at
known offsets from "now" and assert the window queries return exactly the
rows a human would expect.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DB_PATH"] = db_path

    import fundscan.db as db_mod
    db_mod.DB_PATH = Path(db_path)
    db_mod.init_db(Path(db_path))

    yield db_mod
    os.unlink(db_path)


def _snapshot(minutes_ago, exchange="bybit", symbol="BTCUSDT", net_apy=0.10):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"fetched_at": ts, "exchange": exchange, "symbol": symbol, "rate_8h": 0.0002, "net_apy": net_apy}


# ---------------------------------------------------------------------------
# query_delayed
# ---------------------------------------------------------------------------

def test_query_delayed_includes_row_older_than_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=15)])
    rows = tmp_db.query_delayed(delay_minutes=10)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"


def test_query_delayed_excludes_row_newer_than_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=2)])
    rows = tmp_db.query_delayed(delay_minutes=10)
    assert rows == []


def test_query_delayed_returns_most_recent_qualifying_snapshot_per_pair(tmp_db):
    # Two snapshots for the same pair, both old enough -- should return
    # only the most recent of the two (highest net_apy here), not both.
    tmp_db.insert_snapshots([
        _snapshot(minutes_ago=30, net_apy=0.05),
        _snapshot(minutes_ago=15, net_apy=0.08),
    ])
    rows = tmp_db.query_delayed(delay_minutes=10)
    assert len(rows) == 1
    assert rows[0]["net_apy"] == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# query_history / query_sparklines -- same ts-comparison pattern
# ---------------------------------------------------------------------------

def test_query_history_includes_row_inside_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=60, symbol="ETHUSDT")])
    rows = tmp_db.query_history("ETHUSDT", days=7)
    assert len(rows) == 1


def test_query_history_excludes_row_outside_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=10 * 24 * 60, symbol="ETHUSDT")])  # 10 days ago
    rows = tmp_db.query_history("ETHUSDT", days=7)
    assert rows == []


def test_query_sparklines_includes_row_inside_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=60, exchange="okx", symbol="SOLUSDT")])
    result = tmp_db.query_sparklines(hours=24)
    assert "okx:SOLUSDT" in result


def test_query_sparklines_excludes_row_outside_window(tmp_db):
    tmp_db.insert_snapshots([_snapshot(minutes_ago=48 * 60, exchange="okx", symbol="SOLUSDT")])  # 48h ago
    result = tmp_db.query_sparklines(hours=24)
    assert "okx:SOLUSDT" not in result
