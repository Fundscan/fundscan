"""
Position-size-aware net yield.

Ranks opportunities the way a trader actually experiences them: a position
has to be entered and later exited against real order book depth, and a
funding rate only matters if you can actually get size into (and out of) the
instrument without giving back the edge to slippage.

This module extends the fee model in math.py (FEE_PER_LEG, LEGS) rather
than duplicating it. Fees are still sourced from there — this only supplies
a size-derived slippage estimate that replaces the flat SLIPPAGE placeholder
math.net_apy() uses, via math.net_apy_at_size().
"""
from typing import Optional

from . import math as fm

# Position size presets shown on the dashboard (GBP; treated 1:1 against the
# USDT-denominated order books fetchers return — this scanner doesn't do FX).
POSITION_SIZES = [250, 1000, 5000, 25000]
DEFAULT_POSITION_SIZE = 1000

# Position size as % of 24h volume — thresholds for the liquidity badge.
LIQUIDITY_AMBER_PCT = 0.0025  # 0.25%
LIQUIDITY_RED_PCT = 0.01      # 1% — the spec's "flag anything above 1%" line


def _walk_book(levels: list[list[float]], notional: float) -> tuple[float, float]:
    """
    Walk price levels (best price first) consuming up to `notional`.
    Returns (notional_filled, base_units_filled). If the book runs out
    before `notional` is consumed, notional_filled < notional.
    """
    remaining = notional
    notional_filled = 0.0
    units_filled = 0.0
    for price, qty in levels:
        if remaining <= 0 or price <= 0 or qty <= 0:
            continue
        level_notional = price * qty
        take = min(remaining, level_notional)
        notional_filled += take
        units_filled += take / price
        remaining -= take
        if remaining <= 0:
            break
    return notional_filled, units_filled


def _side_slippage_pct(levels: list[list[float]], notional: float) -> float:
    """
    Fractional slippage cost for filling `notional` against one side of the
    book (asks to buy in, bids to sell out).

    Two components:
      - filled_slippage: the volume-weighted average fill price vs. the
        best available price, for whatever the book could actually absorb.
      - a penalty for the portion the book couldn't absorb at all, scaled
        by how many multiples of total visible depth the position
        overshoots. A position that fits inside the book pays close to
        nothing extra; a position many multiples the size of total depth
        is — correctly — treated as effectively unfillable at any sane
        price, which is what makes illiquid outliers collapse.
    """
    if notional <= 0:
        return 0.0
    if not levels:
        return 1.0  # no quotes at all — nothing can be filled

    best_price = levels[0][0]
    total_depth = sum(price * qty for price, qty in levels if price > 0 and qty > 0)
    if total_depth <= 0:
        return 1.0

    notional_filled, units_filled = _walk_book(levels, notional)
    filled_slippage = (
        abs(notional_filled / units_filled - best_price) / best_price
        if units_filled > 0 else 0.0
    )

    unfilled_fraction = max(0.0, notional - notional_filled) / notional
    overshoot = max(0.0, notional / total_depth - 1.0)
    penalty = unfilled_fraction * overshoot

    return filled_slippage * (1 - unfilled_fraction) + penalty


def entry_exit_slippage_pct(order_book: Optional[dict], position_size: float) -> float:
    """
    Round-trip slippage cost as a fraction of notional: one pass to enter
    the position (crossing the ask) and one to exit it later (crossing the
    bid). Both legs are estimated from the same order book snapshot —
    a live snapshot is the best available estimate for either side of a
    trade that hasn't happened yet.
    """
    book = order_book or {}
    entry = _side_slippage_pct(book.get("asks") or [], position_size)
    exit_ = _side_slippage_pct(book.get("bids") or [], position_size)
    return entry + exit_


def liquidity_pct_of_volume(position_size: float, volume_24h_usd: float) -> Optional[float]:
    """Position size as a fraction of 24h volume. None if volume is unknown/zero."""
    if not volume_24h_usd or volume_24h_usd <= 0:
        return None
    return position_size / volume_24h_usd


def liquidity_flag(pct: Optional[float]) -> str:
    """green/amber/red badge for how large a position is relative to 24h volume."""
    if pct is None or pct > LIQUIDITY_RED_PCT:
        return "red"
    if pct > LIQUIDITY_AMBER_PCT:
        return "amber"
    return "green"


def size_opportunity(row: dict, position_size: float) -> dict:
    """
    Given a scanner row (with order_book + volume_24h_usd) and a position
    size, return a copy of the row with size-derived fields merged in:
        position_size, slippage_pct, net_apy_at_size,
        liquidity_pct, liquidity_flag
    """
    slippage_pct = entry_exit_slippage_pct(row.get("order_book"), position_size)
    liquidity_pct = liquidity_pct_of_volume(position_size, row.get("volume_24h_usd", 0))
    return {
        **row,
        "position_size": position_size,
        "slippage_pct": slippage_pct,
        "net_apy_at_size": fm.net_apy_at_size(row["rate_8h"], slippage_pct),
        "liquidity_pct": liquidity_pct,
        "liquidity_flag": liquidity_flag(liquidity_pct),
    }


def rank_by_size(rows: list[dict], position_size: float) -> list[dict]:
    """Apply size_opportunity to every row and re-sort by net_apy_at_size descending."""
    sized = [size_opportunity(r, position_size) for r in rows]
    sized.sort(key=lambda r: r["net_apy_at_size"], reverse=True)
    return sized
