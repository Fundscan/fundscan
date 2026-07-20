"""
Unit tests for position-size-aware net yield (fundscan/sizing.py).

Fixture order books are hand-built [[price, qty], ...] lists so notional
depth is easy to verify by hand: depth of a level = price * qty.
"""
import pytest

from fundscan import math as fm
from fundscan.sizing import (
    DEFAULT_POSITION_SIZE,
    LIQUIDITY_AMBER_PCT,
    LIQUIDITY_RED_PCT,
    POSITION_SIZES,
    _side_slippage_pct,
    _walk_book,
    entry_exit_slippage_pct,
    liquidity_flag,
    liquidity_pct_of_volume,
    rank_by_size,
    size_opportunity,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Deep, liquid book (e.g. a BTC/ETH-style perp) — depth per side is far
# beyond any of the dashboard's position size presets.
DEEP_BOOK = {
    "bids": [[100.0, 50_000], [99.9, 50_000], [99.8, 50_000]],
    "asks": [[100.1, 50_000], [100.2, 50_000], [100.3, 50_000]],
}

# Thin, illiquid micro-cap book: total depth per side ~= 67*(1+.99+.98) =
# ~199 (bids) and 67*(1.01+1.02+1.03) ~= 205 (asks). A $1,000 position is
# ~5x that depth on either side.
THIN_BOOK = {
    "bids": [[1.00, 67], [0.99, 67], [0.98, 67]],
    "asks": [[1.01, 67], [1.02, 67], [1.03, 67]],
}

EMPTY_BOOK = {"bids": [], "asks": []}


# ---------------------------------------------------------------------------
# _walk_book
# ---------------------------------------------------------------------------

def test_walk_book_full_fill_within_top_level():
    notional_filled, units_filled = _walk_book([[100.0, 10]], 500.0)
    assert notional_filled == pytest.approx(500.0)
    assert units_filled == pytest.approx(5.0)


def test_walk_book_spills_into_second_level():
    levels = [[100.0, 5], [101.0, 5]]  # level 1 depth = 500
    notional_filled, units_filled = _walk_book(levels, 700.0)
    # 500 from level 1 (5 units) + 200 from level 2 (200/101 units)
    assert notional_filled == pytest.approx(700.0)
    assert units_filled == pytest.approx(5 + 200 / 101)


def test_walk_book_partial_fill_when_book_exhausted():
    levels = [[100.0, 5], [101.0, 5]]  # total depth = 1005
    notional_filled, units_filled = _walk_book(levels, 5000.0)
    assert notional_filled == pytest.approx(1005.0)
    assert notional_filled < 5000.0


# ---------------------------------------------------------------------------
# _side_slippage_pct / entry_exit_slippage_pct
# ---------------------------------------------------------------------------

def test_side_slippage_empty_book_is_total():
    assert _side_slippage_pct([], 1000.0) == 1.0


def test_side_slippage_zero_notional_is_free():
    assert _side_slippage_pct(DEEP_BOOK["asks"], 0.0) == 0.0


def test_side_slippage_zero_price_top_level_does_not_crash():
    # A degenerate/glitch quote (some exchanges occasionally return a
    # zero-price level) must not crash the best_price division -- the
    # zero-price level should simply be skipped in favor of the next
    # valid one.
    levels = [[0.0, 100], [1.0, 100]]
    assert _side_slippage_pct(levels, 50.0) == pytest.approx(0.0)


def test_side_slippage_all_zero_price_levels_is_total():
    assert _side_slippage_pct([[0.0, 100], [0.0, 50]], 1000.0) == 1.0


def test_side_slippage_deep_book_negligible_at_small_size():
    # $1,000 barely dents a book with $5M+ per level
    pct = _side_slippage_pct(DEEP_BOOK["asks"], 1000.0)
    assert pct < 0.001


def test_side_slippage_thin_book_is_large_relative_to_deep_book():
    thin = _side_slippage_pct(THIN_BOOK["asks"], 1000.0)
    deep = _side_slippage_pct(DEEP_BOOK["asks"], 1000.0)
    assert thin > deep
    assert thin > 1.0  # position is several multiples of total book depth


def test_entry_exit_slippage_sums_both_sides():
    entry = _side_slippage_pct(THIN_BOOK["asks"], 1000.0)
    exit_ = _side_slippage_pct(THIN_BOOK["bids"], 1000.0)
    combined = entry_exit_slippage_pct(THIN_BOOK, 1000.0)
    assert combined == pytest.approx(entry + exit_)


def test_entry_exit_slippage_handles_missing_book():
    assert entry_exit_slippage_pct(None, 1000.0) == 2.0  # both sides empty -> 1.0 + 1.0
    assert entry_exit_slippage_pct({}, 1000.0) == 2.0


# ---------------------------------------------------------------------------
# liquidity_pct_of_volume / liquidity_flag
# ---------------------------------------------------------------------------

def test_liquidity_pct_of_volume_typical():
    assert liquidity_pct_of_volume(1000, 100_000) == pytest.approx(0.01)


def test_liquidity_pct_of_volume_zero_volume_is_none():
    assert liquidity_pct_of_volume(1000, 0) is None


def test_liquidity_flag_green_below_amber_threshold():
    assert liquidity_flag(LIQUIDITY_AMBER_PCT - 0.0001) == "green"


def test_liquidity_flag_amber_between_thresholds():
    assert liquidity_flag((LIQUIDITY_AMBER_PCT + LIQUIDITY_RED_PCT) / 2) == "amber"


def test_liquidity_flag_red_above_one_percent():
    assert liquidity_flag(LIQUIDITY_RED_PCT + 0.0001) == "red"


def test_liquidity_flag_red_when_volume_unknown():
    assert liquidity_flag(None) == "red"


# ---------------------------------------------------------------------------
# size_opportunity — uses math.net_apy_at_size, doesn't duplicate the fee model
# ---------------------------------------------------------------------------

def test_size_opportunity_merges_expected_fields():
    row = {"rate_8h": 0.0002, "order_book": DEEP_BOOK, "volume_24h_usd": 10_000_000,
           "exchange": "bybit", "symbol": "BTCUSDT"}
    out = size_opportunity(row, 1000)
    assert out["exchange"] == "bybit"  # original fields preserved
    assert out["position_size"] == 1000
    assert out["liquidity_flag"] == "green"
    expected_net = fm.net_apy_at_size(0.0002, out["slippage_pct"])
    assert out["net_apy_at_size"] == pytest.approx(expected_net)


def test_size_opportunity_uses_fee_model_constants_not_a_duplicate():
    # net_apy_at_size must equal gross - round_trip_fee_cost - slippage,
    # sourced from the same FEE_PER_LEG/LEGS constants as math.net_apy().
    row = {"rate_8h": 0.0001, "order_book": DEEP_BOOK, "volume_24h_usd": 10_000_000}
    out = size_opportunity(row, 1000)
    expected = fm.annualised_gross(0.0001) - fm.round_trip_fee_cost() - out["slippage_pct"]
    assert out["net_apy_at_size"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# rank_by_size
# ---------------------------------------------------------------------------

def test_rank_by_size_sorts_descending_by_net_at_size():
    liquid = {"exchange": "bybit", "symbol": "BTCUSDT", "rate_8h": 0.0002,
              "order_book": DEEP_BOOK, "volume_24h_usd": 10_000_000}
    illiquid_high_gross = {"exchange": "bybit", "symbol": "MICROUSDT",
                            "rate_8h": 6.28 / fm.FUNDING_PERIODS_PER_YEAR,
                            "order_book": THIN_BOOK, "volume_24h_usd": 3000}
    ranked = rank_by_size([illiquid_high_gross, liquid], 1000)
    # At size, the illiquid 628%-gross pair should NOT beat the liquid one
    # despite having a vastly higher raw funding rate.
    assert ranked[0]["symbol"] == "BTCUSDT"
    assert ranked[0]["net_apy_at_size"] >= ranked[1]["net_apy_at_size"]


def test_rank_by_size_flips_order_at_different_sizes():
    # At a tiny size the illiquid pair's depth is enough to keep it ahead;
    # at $1k it collapses below the liquid pair. Ranking must reflect that.
    liquid = {"exchange": "bybit", "symbol": "BTCUSDT", "rate_8h": 0.0002,
              "order_book": DEEP_BOOK, "volume_24h_usd": 10_000_000}
    illiquid_high_gross = {"exchange": "bybit", "symbol": "MICROUSDT",
                            "rate_8h": 6.28 / fm.FUNDING_PERIODS_PER_YEAR,
                            "order_book": THIN_BOOK, "volume_24h_usd": 3000}
    small = rank_by_size([liquid, illiquid_high_gross], 100)
    large = rank_by_size([liquid, illiquid_high_gross], 1000)
    assert small[0]["symbol"] == "MICROUSDT"
    assert large[0]["symbol"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# Flagship case: 600%+ gross APY collapses to near zero net at £1,000
# ---------------------------------------------------------------------------

def test_illiquid_outlier_collapses_near_zero_net_at_1k():
    """
    A micro-cap perp advertising ~628% gross APY, but with only ~$200 of
    depth on either side of its book. Nobody can actually get $1,000 in and
    back out of that without giving the entire edge back to slippage.
    """
    rate_8h = 6.28 / fm.FUNDING_PERIODS_PER_YEAR
    row = {
        "exchange": "bybit",
        "symbol": "MICROUSDT",
        "rate_8h": rate_8h,
        "order_book": THIN_BOOK,
        "volume_24h_usd": 3000,  # $1k is ~33% of 24h volume too
    }

    gross_apy = fm.annualised_gross(rate_8h)
    assert gross_apy > 6.0  # the advertised 600%+ headline

    out = size_opportunity(row, DEFAULT_POSITION_SIZE)
    assert DEFAULT_POSITION_SIZE == 1000

    # The headline collapses to (near) zero — nowhere close to the 628%
    # gross figure, and well within a +/-15 percentage-point band of zero.
    assert abs(out["net_apy_at_size"]) < 0.15
    assert out["net_apy_at_size"] < gross_apy - 5.0

    # And it's flagged as illiquid, not silently dropped from the board.
    assert out["liquidity_flag"] == "red"


def test_illiquid_outlier_still_shown_not_deleted():
    """Illiquid outliers sink in rank_by_size but are never filtered out."""
    rate_8h = 6.28 / fm.FUNDING_PERIODS_PER_YEAR
    row = {"exchange": "bybit", "symbol": "MICROUSDT", "rate_8h": rate_8h,
           "order_book": THIN_BOOK, "volume_24h_usd": 3000}
    ranked = rank_by_size([row], DEFAULT_POSITION_SIZE)
    assert len(ranked) == 1
    assert ranked[0]["symbol"] == "MICROUSDT"


def test_position_size_presets():
    assert POSITION_SIZES == [250, 1000, 5000, 25000]
    assert DEFAULT_POSITION_SIZE in POSITION_SIZES
