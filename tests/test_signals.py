"""
Unit tests for KKB integration Phase 3 signal generation (fundscan/signals.py).
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from fundscan.signals import SIGNAL_TTL_SECONDS, build_kraken_signals

DEEP_BOOK = {
    "bids": [[100.0, 50_000], [99.9, 50_000]],
    "asks": [[100.1, 50_000], [100.2, 50_000]],
}
THIN_BOOK = {
    "bids": [[100.0, 1], [99.9, 1]],
    "asks": [[100.1, 1], [100.2, 1]],
}


def make_row(symbol, rate_8h, book=None, volume=50_000_000):
    return {
        "exchange": "kraken",
        "symbol": symbol,
        "rate_8h": rate_8h,
        "order_book": book or DEEP_BOOK,
        "volume_24h_usd": volume,
    }


def test_only_configured_pairs_with_positive_rate_get_a_signal():
    rows = [
        make_row("PF_XBTUSD", 0.0002),          # tradeable, positive -> signal
        make_row("PF_ETHUSD", -0.0001),         # tradeable, negative -> no signal (can't short spot)
        make_row("PF_DOGEUSD", 0.0005),         # not in the tradeable set -> ignored
        {**make_row("BTCUSDT", 0.0002), "exchange": "bybit"},  # wrong exchange -> ignored
    ]
    with patch("fundscan.signals.query_history", return_value=[]):
        out = build_kraken_signals(rows)

    symbols = {s["perp_symbol"] for s in out["signals"]}
    assert symbols == {"PF_XBTUSD"}


def test_signal_shape_matches_schema():
    rows = [make_row("PF_SOLUSD", 0.0003)]
    with patch("fundscan.signals.query_history", return_value=[]):
        out = build_kraken_signals(rows)

    assert "generated_at" in out
    sig = out["signals"][0]
    for field in (
        "signal_id", "pair", "perp_symbol", "action", "funding_rate_pct",
        "confidence", "suggested_capital_gbp", "reason", "expires_at",
    ):
        assert field in sig

    assert uuid.UUID(sig["signal_id"])  # valid uuid, doesn't raise
    assert sig["pair"] == "SOL/GBP"
    assert sig["perp_symbol"] == "PF_SOLUSD"
    assert sig["action"] == "enter_long_spot_short_perp"
    assert sig["suggested_capital_gbp"] == 650.0
    assert 0.0 <= sig["confidence"] <= 1.0

    generated = datetime.fromisoformat(out["generated_at"])
    expires = datetime.fromisoformat(sig["expires_at"])
    assert (expires - generated).total_seconds() == pytest.approx(SIGNAL_TTL_SECONDS)


def test_confidence_lower_for_illiquid_unprofitable_row():
    good_row = make_row("PF_XBTUSD", 0.001, book=DEEP_BOOK)
    bad_row = make_row("PF_ETHUSD", 0.00001, book=THIN_BOOK, volume=100)  # tiny rate, thin book

    with patch("fundscan.signals.query_history", return_value=[]):
        good_out = build_kraken_signals([good_row])
        bad_out = build_kraken_signals([bad_row])

    good_conf = good_out["signals"][0]["confidence"]
    bad_conf = bad_out["signals"][0]["confidence"]
    assert good_conf > bad_conf


def test_confidence_lower_when_current_rate_spikes_above_realized_average():
    row = make_row("PF_XBTUSD", 0.002)

    # realized_accuracy treats the LAST row as "current" -- 9 low entries
    # then one high one simulates the most recent snapshot spiking well
    # above the rest of the week's average.
    spike_history = [{"net_apy": 0.05}] * 9 + [{"net_apy": 0.30}]
    with patch("fundscan.signals.query_history", return_value=spike_history):
        out_with_spike_history = build_kraken_signals([row])

    with patch("fundscan.signals.query_history", return_value=[]):
        out_no_history = build_kraken_signals([row])

    # With enough history showing the current rate is way above the realized
    # average, confidence should be penalised relative to having no history
    # to check against at all.
    assert out_with_spike_history["signals"][0]["confidence"] < out_no_history["signals"][0]["confidence"]


def test_env_override_changes_tradeable_pairs(monkeypatch):
    monkeypatch.setenv(
        "KRAKEN_TRADEABLE_PAIRS",
        '{"PF_XRPUSD": {"pair": "XRP/GBP", "suggested_capital_gbp": 100.0}}',
    )
    rows = [make_row("PF_XRPUSD", 0.0004), make_row("PF_XBTUSD", 0.0004)]
    with patch("fundscan.signals.query_history", return_value=[]):
        out = build_kraken_signals(rows)

    symbols = {s["perp_symbol"] for s in out["signals"]}
    assert symbols == {"PF_XRPUSD"}  # BTC no longer configured, XRP now is


def test_malformed_env_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("KRAKEN_TRADEABLE_PAIRS", "not valid json")
    rows = [make_row("PF_XBTUSD", 0.0004)]
    with patch("fundscan.signals.query_history", return_value=[]):
        out = build_kraken_signals(rows)

    assert {s["perp_symbol"] for s in out["signals"]} == {"PF_XBTUSD"}
