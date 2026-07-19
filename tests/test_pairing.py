"""
Unit tests for cross-exchange funding spread pairing (fundscan/pairing.py).
"""
import pytest

from fundscan import math as fm
from fundscan import sizing as sz
from fundscan.pairing import (
    base_asset,
    build_pairs,
    rank_pairs_by_size,
    size_pair_opportunity,
)

DEEP_BOOK = {
    "bids": [[100.0, 50_000], [99.9, 50_000]],
    "asks": [[100.1, 50_000], [100.2, 50_000]],
}


def make_row(exchange, symbol, rate_8h, volume=10_000_000, book=None):
    return {
        "exchange": exchange,
        "symbol": symbol,
        "rate_8h": rate_8h,
        "volume_24h_usd": volume,
        "order_book": book or DEEP_BOOK,
    }


# ---------------------------------------------------------------------------
# base_asset — symbol normalization across exchange naming conventions
# ---------------------------------------------------------------------------

def test_base_asset_strips_plain_usdt_suffix():
    assert base_asset("binance", "BTCUSDT") == "BTC"
    assert base_asset("bybit", "ETHUSDT") == "ETH"
    assert base_asset("okx", "BTCUSDT") == "BTC"  # OKX fetcher already normalises


def test_base_asset_strips_hyperliquid_perp_suffix():
    assert base_asset("hyperliquid", "BTC-PERP") == "BTC"


def test_base_asset_resolves_known_multiplier_alias():
    # Binance/Bybit "1000PEPE" vs Hyperliquid "kPEPE" vs plain "PEPE" must
    # all resolve to the same canonical asset.
    assert base_asset("binance", "1000PEPEUSDT") == "PEPE"
    assert base_asset("bybit", "1000PEPEUSDT") == "PEPE"
    assert base_asset("hyperliquid", "kPEPE-PERP") == "PEPE"


def test_base_asset_unmapped_prefix_falls_back_conservatively():
    # No alias entry for a hypothetical mismatch -> falls back to the raw
    # suffix-stripped symbol rather than guessing.
    assert base_asset("binance", "1000XYZUSDT") == "1000XYZ"


def test_base_asset_does_not_misfire_on_real_k_tickers():
    # A real asset that happens to start with "K" must NOT be treated as a
    # x1000-scaled alias just because Hyperliquid's kPEPE convention exists.
    assert base_asset("hyperliquid", "KAVA-PERP") == "KAVA"


# ---------------------------------------------------------------------------
# build_pairs
# ---------------------------------------------------------------------------

def test_build_pairs_shorts_the_higher_funding_venue():
    rows = [
        make_row("bybit", "BTCUSDT", 0.0005),
        make_row("binance", "BTCUSDT", 0.0001),
    ]
    pairs = build_pairs(rows)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["asset"] == "BTC"
    assert pair["short_exchange"] == "bybit"
    assert pair["long_exchange"] == "binance"
    assert pair["spread_rate_8h"] == pytest.approx(0.0004)


def test_build_pairs_skips_assets_on_only_one_exchange():
    rows = [make_row("bybit", "BTCUSDT", 0.0005)]
    assert build_pairs(rows) == []


def test_build_pairs_never_pairs_same_exchange_with_itself():
    # Two rows on the same exchange for the same canonical asset (e.g. a
    # redenomination overlap) must not produce a "spread" against itself.
    rows = [
        make_row("binance", "1000PEPEUSDT", 0.0005),
        make_row("binance", "PEPEUSDT", 0.0001),
    ]
    assert build_pairs(rows) == []


def test_build_pairs_covers_all_venue_combinations():
    rows = [
        make_row("bybit", "BTCUSDT", 0.0005),
        make_row("binance", "BTCUSDT", 0.0001),
        make_row("okx", "BTCUSDT", -0.0002),
    ]
    pairs = build_pairs(rows)
    assert len(pairs) == 3  # C(3,2)
    exchanges_seen = {frozenset([p["short_exchange"], p["long_exchange"]]) for p in pairs}
    assert frozenset(["bybit", "binance"]) in exchanges_seen
    assert frozenset(["bybit", "okx"]) in exchanges_seen
    assert frozenset(["binance", "okx"]) in exchanges_seen


def test_build_pairs_uses_gross_and_net_apy_from_math_module():
    rows = [
        make_row("bybit", "BTCUSDT", 0.0005),
        make_row("binance", "BTCUSDT", 0.0001),
    ]
    pair = build_pairs(rows)[0]
    assert pair["gross_apy"] == pytest.approx(fm.annualised_gross(0.0004))
    assert pair["net_apy"] == pytest.approx(fm.net_apy(0.0004))


# ---------------------------------------------------------------------------
# size_pair_opportunity — reuses sizing.py, doesn't duplicate slippage math
# ---------------------------------------------------------------------------

def test_size_pair_opportunity_sums_slippage_across_both_legs():
    short_row = make_row("bybit", "BTCUSDT", 0.0005)
    long_row = make_row("binance", "BTCUSDT", 0.0001)
    pair = build_pairs([short_row, long_row])[0]
    out = size_pair_opportunity(pair, 1000)

    expected_slippage = (
        sz.entry_exit_slippage_pct(short_row["order_book"], 1000)
        + sz.entry_exit_slippage_pct(long_row["order_book"], 1000)
    )
    assert out["slippage_pct"] == pytest.approx(expected_slippage)
    assert out["net_apy_at_size"] == pytest.approx(
        fm.net_apy_at_size(pair["spread_rate_8h"], expected_slippage)
    )


def test_size_pair_opportunity_liquidity_uses_thinner_leg():
    short_row = make_row("bybit", "BTCUSDT", 0.0005, volume=1_000_000)
    long_row = make_row("binance", "BTCUSDT", 0.0001, volume=50_000)  # thinner
    pair = build_pairs([short_row, long_row])[0]
    out = size_pair_opportunity(pair, 1000)
    assert out["liquidity_pct"] == pytest.approx(1000 / 50_000)


def test_size_pair_opportunity_strips_internal_row_refs():
    rows = [make_row("bybit", "BTCUSDT", 0.0005), make_row("binance", "BTCUSDT", 0.0001)]
    out = size_pair_opportunity(build_pairs(rows)[0], 1000)
    assert not any(k.startswith("_") for k in out)


# ---------------------------------------------------------------------------
# rank_pairs_by_size
# ---------------------------------------------------------------------------

def test_rank_pairs_by_size_sorts_descending():
    rows = [
        make_row("bybit", "BTCUSDT", 0.0005),
        make_row("binance", "BTCUSDT", 0.0001),
        make_row("bybit", "ETHUSDT", 0.00015),
        make_row("okx", "ETHUSDT", 0.00005),
    ]
    ranked = rank_pairs_by_size(rows, 1000)
    apys = [p["net_apy_at_size"] for p in ranked]
    assert apys == sorted(apys, reverse=True)


def test_rank_pairs_by_size_illiquid_leg_sinks_the_spread():
    thin_book = {
        "bids": [[1.00, 20], [0.99, 20]],
        "asks": [[1.01, 20], [1.02, 20]],
    }
    rows = [
        make_row("bybit", "MICROUSDT", 0.02, volume=3000, book=thin_book),
        make_row("binance", "MICROUSDT", 0.0001),
        make_row("bybit", "BTCUSDT", 0.0003),
        make_row("binance", "BTCUSDT", 0.0001),
    ]
    ranked = rank_pairs_by_size(rows, 5000)
    micro_pair = next(p for p in ranked if p["asset"] == "MICRO")
    btc_pair = next(p for p in ranked if p["asset"] == "BTC")
    # The MICRO spread has a far higher raw rate differential but a leg with
    # almost no depth -- it must not out-rank the liquid BTC spread at size.
    assert btc_pair["net_apy_at_size"] > micro_pair["net_apy_at_size"]
    assert micro_pair["liquidity_flag"] == "red"
