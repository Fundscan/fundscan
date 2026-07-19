"""
Cross-exchange funding spread pairing.

Distinct from the single-exchange spot+perp opportunities in scanner.py:
this pairs the SAME base asset's perpetual contract across two exchanges
and computes the funding-rate spread trade — short the richer venue, long
the cheaper one — which is delta-neutral without holding spot at all.

Extends math.py (annualised_gross, net_apy, round_trip_fee_cost,
net_apy_at_size) and sizing.py (entry_exit_slippage_pct,
liquidity_pct_of_volume, liquidity_flag) rather than duplicating either.
"""
from itertools import combinations

from . import math as fm
from . import sizing as sz

# Manually curated multiplier-prefix aliases. Exchanges disagree on how they
# name leveraged/rebased meme-coin contracts (Binance/Bybit use "1000PEPE",
# Hyperliquid uses "kPEPE", OKX typically lists the plain "PEPE"). Blind
# prefix-stripping (e.g. treating any leading "k" as a x1000 marker) would
# misfire on real tickers like KAVA or KSM, so unmapped mismatches simply
# fail to pair rather than pairing incorrectly. Extend this table as new
# mismatches are found — do not replace it with a generic regex.
KNOWN_ALIASES: dict[tuple[str, str], str] = {
    ("binance", "1000PEPE"): "PEPE",
    ("binance", "1000BONK"): "BONK",
    ("binance", "1000SHIB"): "SHIB",
    ("binance", "1000FLOKI"): "FLOKI",
    ("bybit", "1000PEPE"): "PEPE",
    ("bybit", "1000BONK"): "BONK",
    ("bybit", "1000SHIB"): "SHIB",
    ("bybit", "1000FLOKI"): "FLOKI",
    ("hyperliquid", "kPEPE"): "PEPE",
    ("hyperliquid", "kBONK"): "BONK",
    ("hyperliquid", "kSHIB"): "SHIB",
    ("hyperliquid", "kFLOKI"): "FLOKI",
}

# Suffix each exchange's fetcher leaves on the symbol, stripped before the
# alias lookup. OKX's fetcher already normalises BTC-USDT-SWAP -> BTCUSDT,
# so it shares Binance/Bybit's USDT suffix here.
_EXCHANGE_SUFFIXES = {
    "binance": "USDT",
    "bybit": "USDT",
    "okx": "USDT",
    "hyperliquid": "-PERP",
}


def base_asset(exchange: str, symbol: str) -> str:
    """
    Canonical base asset for a (exchange, symbol) pair, e.g.
    ("binance", "1000PEPEUSDT") -> "PEPE", ("hyperliquid", "BTC-PERP") -> "BTC".
    Falls back to the suffix-stripped symbol if no alias is known.
    """
    suffix = _EXCHANGE_SUFFIXES.get(exchange, "")
    raw = symbol[: -len(suffix)] if suffix and symbol.endswith(suffix) else symbol
    return KNOWN_ALIASES.get((exchange, raw), raw)


def build_pairs(rows: list[dict]) -> list[dict]:
    """
    Group scanner rows by canonical base asset, and for every base asset
    quoted on 2+ exchanges, emit one spread opportunity per exchange pair:
    short the higher-funding venue, long the lower-funding one. Two rows
    from the same exchange (e.g. a redenominated symbol overlap) are never
    paired against each other.
    """
    by_asset: dict[str, list[dict]] = {}
    for row in rows:
        asset = base_asset(row["exchange"], row["symbol"])
        by_asset.setdefault(asset, []).append(row)

    pairs = []
    for asset, venue_rows in by_asset.items():
        if len(venue_rows) < 2:
            continue
        for a, b in combinations(venue_rows, 2):
            if a["exchange"] == b["exchange"]:
                continue
            short_leg, long_leg = (a, b) if a["rate_8h"] >= b["rate_8h"] else (b, a)
            spread_rate_8h = short_leg["rate_8h"] - long_leg["rate_8h"]
            pairs.append({
                "asset": asset,
                "short_exchange": short_leg["exchange"],
                "short_symbol": short_leg["symbol"],
                "long_exchange": long_leg["exchange"],
                "long_symbol": long_leg["symbol"],
                "spread_rate_8h": spread_rate_8h,
                "gross_apy": fm.annualised_gross(spread_rate_8h),
                "net_apy": fm.net_apy(spread_rate_8h),
                "is_profitable": fm.is_profitable(spread_rate_8h),
                # Internal-only refs for sizing; stripped before rendering.
                "_short_row": short_leg,
                "_long_row": long_leg,
            })
    return pairs


def size_pair_opportunity(pair: dict, position_size: float) -> dict:
    """
    Given a spread pair (from build_pairs) and a position size, compute the
    size-derived fields: slippage summed across BOTH legs' order books, net
    yield at size, and a liquidity score against the thinner leg's 24h
    volume (the trade is constrained by whichever venue is shallower).
    """
    short_row, long_row = pair["_short_row"], pair["_long_row"]
    slippage_pct = (
        sz.entry_exit_slippage_pct(short_row.get("order_book"), position_size)
        + sz.entry_exit_slippage_pct(long_row.get("order_book"), position_size)
    )
    short_vol = short_row.get("volume_24h_usd") or 0
    long_vol = long_row.get("volume_24h_usd") or 0
    worst_volume = min(short_vol, long_vol)
    liquidity_pct = sz.liquidity_pct_of_volume(position_size, worst_volume)

    out = {k: v for k, v in pair.items() if not k.startswith("_")}
    out.update({
        "position_size": position_size,
        "slippage_pct": slippage_pct,
        "net_apy_at_size": fm.net_apy_at_size(pair["spread_rate_8h"], slippage_pct),
        "liquidity_pct": liquidity_pct,
        "liquidity_flag": sz.liquidity_flag(liquidity_pct),
    })
    return out


def rank_pairs_by_size(rows: list[dict], position_size: float) -> list[dict]:
    """Build cross-exchange spread pairs from scanner rows, size them against
    `position_size`, and sort by net_apy_at_size descending."""
    pairs = build_pairs(rows)
    sized = [size_pair_opportunity(p, position_size) for p in pairs]
    sized.sort(key=lambda p: p["net_apy_at_size"], reverse=True)
    return sized
