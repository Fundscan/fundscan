"""
Tests for the public accuracy log aggregation (app._accuracy_log), which
powers /accuracy -- current headline net APY vs. realized average, per pair,
sorted by the largest gap first.
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
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-padding!")
    # fundscan.alerts reads TELEGRAM_BOT_TOKEN into a module-level global at
    # import time, not per-call -- must be set before the first import of
    # fundscan.app/fundscan.alerts anywhere in the test session, or it's
    # permanently "" for the rest of the process (see test_alerts.py, which
    # sets the same default for the same reason).
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")

    import fundscan.db as db_mod
    db_mod.DB_PATH = Path(db_path)
    db_mod.init_db(Path(db_path))

    yield db_mod
    os.unlink(db_path)


def _seed(db_mod, exchange, symbol, net_apys, minutes_apart=10):
    """Insert snapshots oldest-first, `minutes_apart` apart, ending now."""
    n = len(net_apys)
    rows = []
    for i, apy in enumerate(net_apys):
        minutes_ago = (n - 1 - i) * minutes_apart
        ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        rows.append({"fetched_at": ts, "exchange": exchange, "symbol": symbol, "rate_8h": 0.0002, "net_apy": apy})
    db_mod.insert_snapshots(rows)


def test_pair_below_min_samples_is_excluded(tmp_db):
    from fundscan.app import _accuracy_log, ACCURACY_MIN_SAMPLES
    _seed(tmp_db, "bybit", "BTCUSDT", [0.10, 0.12])  # only 2 < ACCURACY_MIN_SAMPLES (3)
    assert ACCURACY_MIN_SAMPLES == 3
    result = _accuracy_log(days=7)
    assert result["pair_count"] == 0
    assert result["entries"] == []


def test_gap_is_current_minus_realized_average(tmp_db):
    from fundscan.app import _accuracy_log
    # realized avg of [0.10, 0.10, 0.10] = 0.10, current (last) = 0.10 -> gap 0
    _seed(tmp_db, "bybit", "BTCUSDT", [0.10, 0.10, 0.10])
    result = _accuracy_log(days=7)
    assert result["pair_count"] == 1
    entry = result["entries"][0]
    assert entry["exchange"] == "bybit"
    assert entry["symbol"] == "BTCUSDT"
    assert entry["current_net_apy"] == pytest.approx(0.10)
    assert entry["realized_avg_net_apy"] == pytest.approx(0.10)
    assert entry["gap"] == pytest.approx(0.0)


def test_entries_sorted_by_largest_gap_first(tmp_db):
    from fundscan.app import _accuracy_log
    # small gap: 0.10,0.10,0.11 -> avg .1033, current .11, gap ~.0067
    _seed(tmp_db, "bybit", "SMALLGAP", [0.10, 0.10, 0.11])
    # big gap: current spikes to 0.50 vs a much lower realized average
    _seed(tmp_db, "bybit", "BIGGAP", [0.05, 0.05, 0.50])
    result = _accuracy_log(days=7)
    symbols = [e["symbol"] for e in result["entries"]]
    assert symbols[0] == "BIGGAP"  # largest |gap| first
    assert symbols[-1] == "SMALLGAP"


def test_within_5pt_pct_and_avg_abs_gap(tmp_db):
    from fundscan.app import _accuracy_log
    # gap ~0 -> within 5pt
    _seed(tmp_db, "bybit", "CLOSE", [0.10, 0.10, 0.10])
    # gap way more than 5 points (avg .05 -> current .50, gap=.45=45pt)
    _seed(tmp_db, "bybit", "FAR", [0.05, 0.05, 0.50])
    result = _accuracy_log(days=7)
    assert result["pair_count"] == 2
    assert result["within_5pt_pct"] == pytest.approx(0.5)  # 1 of 2 within 5pt
    assert result["avg_abs_gap"] > 0


def test_no_data_returns_empty_result(tmp_db):
    from fundscan.app import _accuracy_log
    result = _accuracy_log(days=7)
    assert result["pair_count"] == 0
    assert result["entries"] == []
    assert result["avg_abs_gap"] is None
    assert result["within_5pt_pct"] is None
