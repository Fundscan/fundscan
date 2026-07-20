"""
Fee-adjusted yield calculations for funding rate arbitrage.

All rates are expressed as decimals (e.g. 0.0001 = 0.01%).
Fee model: 0.26% per leg × 4 legs + 0.05% slippage (one-way).
Round-trip total cost = 4 × 0.0026 + 0.0005 = 0.0109 (1.09%)
"""
from typing import Optional

FUNDING_PERIODS_PER_YEAR = 3 * 365  # 8h intervals → 1095 per year

# Fee structure
FEE_PER_LEG = 0.0026          # 0.26% taker per leg
LEGS = 4                       # open spot + open perp + close spot + close perp
SLIPPAGE = 0.0005              # 0.05% one-way slippage assumption
TOTAL_ROUND_TRIP_COST = FEE_PER_LEG * LEGS + SLIPPAGE  # 1.09%


def annualised_gross(rate_8h: float) -> float:
    """Raw funding rate per 8h → annualised gross yield (decimal)."""
    return rate_8h * FUNDING_PERIODS_PER_YEAR


def net_apy(rate_8h: float) -> float:
    """
    Fee-adjusted net APY.
    Subtract round-trip cost (amortised over the holding period is wrong —
    fees are paid once on entry/exit, so we deduct them from gross annual
    yield directly as a one-time cost expressed in annual terms).
    Net APY = gross annual - total round-trip cost.
    Negative means the opportunity costs more in fees than it earns.
    """
    return annualised_gross(rate_8h) - TOTAL_ROUND_TRIP_COST


def breakeven_cycles(rate_8h: float) -> Optional[float]:
    """
    Number of 8h funding payments needed to recover round-trip fees.
    Returns None if rate is zero or negative (never breaks even).
    """
    if rate_8h <= 0:
        return None
    return TOTAL_ROUND_TRIP_COST / rate_8h


def is_profitable(rate_8h: float) -> bool:
    """True if net APY is positive after fees."""
    return net_apy(rate_8h) > 0


def round_trip_fee_cost() -> float:
    """
    Pure exchange fee cost (no slippage assumption) — the part of the
    round-trip cost that doesn't vary with position size.
    """
    return FEE_PER_LEG * LEGS


def net_apy_at_size(rate_8h: float, slippage_cost: float) -> float:
    """
    Net APY using an actual position-size-derived slippage cost in place
    of the flat SLIPPAGE placeholder used by net_apy(). Fees stay sourced
    from FEE_PER_LEG/LEGS above — this does not duplicate the fee model,
    it swaps in a better slippage estimate for a given size.
    """
    return annualised_gross(rate_8h) - round_trip_fee_cost() - slippage_cost
