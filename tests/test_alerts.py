"""
Tests for the alert threshold logic in fundscan/alerts.py.
No real Telegram or email sends — all network calls are mocked.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True, scope="module")
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-padding!")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")

    import fundscan.db as db_mod
    db_mod.DB_PATH = Path(db_path)
    db_mod.init_db(Path(db_path))

    # Seed a user + alert config
    import fundscan.auth as auth_mod
    user = auth_mod.get_or_create_user("alert@example.com")

    with db_mod.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO alert_configs (user_id, symbol, min_net_apy, telegram_chat_id)
            VALUES (?, NULL, 0.10, '12345678')
            """,
            (user["id"],),
        )

    yield db_path, user["id"]
    os.unlink(db_path)


def _alerts():
    import fundscan.alerts as a
    return a


def _make_result(symbol="BTCUSDT", exchange="bybit", net_apy=0.20, rate_8h=0.0002):
    from fundscan.math import annualised_gross, breakeven_cycles, is_profitable
    return {
        "symbol": symbol,
        "exchange": exchange,
        "rate_8h": rate_8h,
        "net_apy": net_apy,
        "gross_apy": annualised_gross(rate_8h),
        "breakeven_cycles": breakeven_cycles(rate_8h),
        "is_profitable": is_profitable(rate_8h),
        "fetched_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Threshold alert fires when net_apy >= min_net_apy
# ---------------------------------------------------------------------------

def test_alert_fires_above_threshold(tmp_db):
    alerts = _alerts()
    result = _make_result(net_apy=0.25)  # above 0.10 threshold

    with patch.object(alerts, "_send_telegram") as mock_tg, \
         patch.object(alerts, "send_email") as mock_email:
        alerts.check_and_send_alerts([result])

    mock_tg.assert_called_once()
    assert "BTCUSDT" in mock_tg.call_args[0][1]


def test_alert_suppressed_below_threshold(tmp_db):
    alerts = _alerts()
    result = _make_result(symbol="ETHUSDT", net_apy=0.05)  # below 0.10

    with patch.object(alerts, "_send_telegram") as mock_tg, \
         patch.object(alerts, "send_email") as mock_email:
        alerts.check_and_send_alerts([result])

    mock_tg.assert_not_called()


# ---------------------------------------------------------------------------
# Cooldown: second alert within 1 hour is suppressed
# ---------------------------------------------------------------------------

def test_alert_cooldown(tmp_db):
    alerts = _alerts()
    result = _make_result(symbol="SOLUSDT", net_apy=0.30)

    with patch.object(alerts, "_send_telegram") as mock_tg, \
         patch.object(alerts, "send_email"):
        alerts.check_and_send_alerts([result])
        first_call_count = mock_tg.call_count
        # second call — same symbol, within cooldown window
        alerts.check_and_send_alerts([result])
        second_call_count = mock_tg.call_count

    assert first_call_count == 1
    assert second_call_count == 1  # not called again


# ---------------------------------------------------------------------------
# generate_connect_code + link_telegram
# ---------------------------------------------------------------------------

def test_connect_code_lifecycle(tmp_db):
    import fundscan.auth as auth_mod
    alerts = _alerts()
    user = auth_mod.get_or_create_user("tg@example.com")

    code = alerts.generate_connect_code(user["id"])
    assert len(code) == 6
    assert code.isdigit()

    ok = alerts.link_telegram(code, "9999999")
    assert ok is True

    # Code can only be used once
    ok2 = alerts.link_telegram(code, "9999999")
    assert ok2 is False


def test_invalid_connect_code(tmp_db):
    alerts = _alerts()
    ok = alerts.link_telegram("000000", "1234")
    assert ok is False
