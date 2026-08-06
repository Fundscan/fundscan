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


# ---------------------------------------------------------------------------
# create_notify_signup — low-friction "notify me" email capture
# ---------------------------------------------------------------------------

def test_notify_signup_creates_email_only_config(tmp_db):
    alerts = _alerts()
    user = alerts.create_notify_signup("notify@example.com", 15, symbol=None)

    import fundscan.db as db_mod
    with db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM alert_configs WHERE user_id = ? AND symbol IS NULL",
            (user["id"],),
        ).fetchone()
    assert row is not None
    assert row["telegram_chat_id"] == ""  # not NULL -- see create_notify_signup docstring
    assert row["min_net_apy"] == pytest.approx(0.15)


def test_notify_signup_resubmit_updates_threshold_not_duplicates(tmp_db):
    alerts = _alerts()
    user = alerts.create_notify_signup("resubmit@example.com", 10, symbol="ETHUSDT")
    alerts.create_notify_signup("resubmit@example.com", 25, symbol="ETHUSDT")

    import fundscan.db as db_mod
    with db_mod.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_configs WHERE user_id = ? AND symbol = 'ETHUSDT'",
            (user["id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["min_net_apy"] == pytest.approx(0.25)


def test_notify_signup_different_symbols_create_separate_rows(tmp_db):
    alerts = _alerts()
    user = alerts.create_notify_signup("multi@example.com", 10, symbol="BTCUSDT")
    alerts.create_notify_signup("multi@example.com", 10, symbol="ETHUSDT")

    import fundscan.db as db_mod
    with db_mod.get_conn() as conn:
        rows = conn.execute(
            "SELECT symbol FROM alert_configs WHERE user_id = ? ORDER BY symbol",
            (user["id"],),
        ).fetchall()
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]


def test_notify_signup_fires_email_not_telegram(tmp_db):
    """
    The whole point of create_notify_signup is to ride the existing
    _send_alert() -> send_email() path without a Telegram connection.
    Exercises _send_alert directly (rather than check_and_send_alerts,
    which would also process every other wildcard config already seeded
    elsewhere in this module-scoped fixture) so this only asserts on the
    behavior for this one signup's config.
    """
    alerts = _alerts()
    alerts.create_notify_signup("firesemail@example.com", 10, symbol="DOGEUSDT")

    import fundscan.db as db_mod
    with db_mod.get_conn() as conn:
        cfg = conn.execute(
            "SELECT ac.*, u.email FROM alert_configs ac JOIN users u ON u.id = ac.user_id "
            "WHERE ac.symbol = 'DOGEUSDT' AND u.email = 'firesemail@example.com'"
        ).fetchone()
    row = _make_result(symbol="DOGEUSDT", net_apy=0.20)

    with patch.object(alerts, "_send_telegram") as mock_tg, \
         patch.object(alerts, "send_email") as mock_email:
        alerts._send_alert(cfg, row)

    mock_tg.assert_not_called()
    mock_email.assert_called_once()
    assert mock_email.call_args[0][0] == "firesemail@example.com"
