"""
Unit tests for fee/yield maths.
All inputs and expected outputs are derived by hand.

Fee model:
  total_cost = 4 legs × 0.0026 + 0.0005 slippage = 0.0109
  gross_apy  = rate_8h × 3 × 365 = rate_8h × 1095
  net_apy    = gross_apy - 0.0109
  breakeven  = 0.0109 / rate_8h  (funding cycles to recover fees)
"""
import pytest
from fundscan.math import (
    TOTAL_ROUND_TRIP_COST,
    annualised_gross,
    breakeven_cycles,
    is_profitable,
    net_apy,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_total_round_trip_cost():
    # 4 × 0.0026 + 0.0005 = 0.0109
    assert abs(TOTAL_ROUND_TRIP_COST - 0.0109) < 1e-10


# ---------------------------------------------------------------------------
# annualised_gross
# ---------------------------------------------------------------------------

def test_gross_apy_typical():
    # 0.01% per 8h = 0.0001; gross = 0.0001 × 1095 = 0.1095 (10.95%)
    assert abs(annualised_gross(0.0001) - 0.1095) < 1e-9


def test_gross_apy_zero():
    assert annualised_gross(0.0) == 0.0


def test_gross_apy_negative():
    # negative rates are valid (funding paid to shorts)
    assert annualised_gross(-0.0001) == pytest.approx(-0.1095)


def test_gross_apy_high():
    # 0.3% per 8h (extreme) → 0.003 × 1095 = 3.285 (328.5%)
    assert annualised_gross(0.003) == pytest.approx(3.285)


# ---------------------------------------------------------------------------
# net_apy
# ---------------------------------------------------------------------------

def test_net_apy_above_fees():
    # gross = 0.1095, cost = 0.0109 → net = 0.0986
    assert net_apy(0.0001) == pytest.approx(0.1095 - 0.0109)


def test_net_apy_zero_rate():
    # gross = 0, net = -0.0109
    assert net_apy(0.0) == pytest.approx(-TOTAL_ROUND_TRIP_COST)


def test_net_apy_breakeven_rate():
    # find rate where net = 0: rate = cost/1095
    breakeven_rate = TOTAL_ROUND_TRIP_COST / 1095
    assert abs(net_apy(breakeven_rate)) < 1e-12


def test_net_apy_below_fees():
    # very small rate → net is negative
    assert net_apy(0.000001) < 0


def test_net_apy_high_rate():
    # 0.3%/8h → net = 3.285 - 0.0109 = 3.2741
    assert net_apy(0.003) == pytest.approx(3.285 - 0.0109)


# ---------------------------------------------------------------------------
# breakeven_cycles
# ---------------------------------------------------------------------------

def test_breakeven_typical():
    # cost/rate = 0.0109 / 0.0001 = 109 cycles
    result = breakeven_cycles(0.0001)
    assert result == pytest.approx(109.0)


def test_breakeven_zero_rate():
    assert breakeven_cycles(0.0) is None


def test_breakeven_negative_rate():
    assert breakeven_cycles(-0.0001) is None


def test_breakeven_high_rate():
    # 0.003 → 0.0109 / 0.003 ≈ 3.633 cycles
    result = breakeven_cycles(0.003)
    assert result == pytest.approx(0.0109 / 0.003)


def test_breakeven_exact_fee_rate():
    # rate = total_cost → exactly 1 cycle
    result = breakeven_cycles(TOTAL_ROUND_TRIP_COST)
    assert result == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# is_profitable
# ---------------------------------------------------------------------------

def test_profitable_above_threshold():
    high_rate = TOTAL_ROUND_TRIP_COST / 1095 + 0.0001
    assert is_profitable(high_rate) is True


def test_not_profitable_zero():
    assert is_profitable(0.0) is False


def test_not_profitable_below_threshold():
    tiny_rate = TOTAL_ROUND_TRIP_COST / 1095 - 0.000001
    assert is_profitable(tiny_rate) is False


def test_not_profitable_negative():
    assert is_profitable(-0.001) is False


# ---------------------------------------------------------------------------
# Fetcher isolation — one exchange failing must not propagate
# ---------------------------------------------------------------------------

def test_fetcher_isolation():
    """Scanner must return results even when one fetcher raises."""
    from unittest.mock import patch
    from fundscan.scanner import scan

    def bad_fetcher():
        raise RuntimeError("exchange down")

    def good_fetcher():
        return [{
            "exchange": "mock",
            "symbol": "BTCUSDT",
            "rate_8h": 0.0001,
            "funding_interval_hours": 8,
            "next_funding_time": None,
            "volume_24h_usd": 50_000_000,
        }]

    with patch("fundscan.scanner.FETCHERS", [bad_fetcher, good_fetcher]):
        results = scan()

    assert len(results) == 1
    assert results[0]["exchange"] == "mock"
    assert results[0]["net_apy"] == pytest.approx(net_apy(0.0001))


def test_scan_drops_low_volume_rows():
    """
    Rows below MIN_VOLUME_24H_USD are excluded from the ranking entirely --
    thin markets produce real but unreliable/untradeable-at-size funding
    rates that would otherwise pollute the top of the board.
    """
    from unittest.mock import patch
    from fundscan.scanner import scan, MIN_VOLUME_24H_USD

    def thin_fetcher():
        return [{
            "exchange": "mock", "symbol": "THINUSDT", "rate_8h": 0.01,
            "funding_interval_hours": 8, "next_funding_time": None,
            "volume_24h_usd": MIN_VOLUME_24H_USD - 1,
        }]

    def liquid_fetcher():
        return [{
            "exchange": "mock", "symbol": "LIQUIDUSDT", "rate_8h": 0.0001,
            "funding_interval_hours": 8, "next_funding_time": None,
            "volume_24h_usd": MIN_VOLUME_24H_USD,
        }]

    with patch("fundscan.scanner.FETCHERS", [thin_fetcher, liquid_fetcher]):
        results = scan()

    symbols = {r["symbol"] for r in results}
    assert "LIQUIDUSDT" in symbols
    assert "THINUSDT" not in symbols
