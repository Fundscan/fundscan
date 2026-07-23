"""
Fee-adjusted yield calculations for funding rate arbitrage.

All rates are expressed as decimals (e.g. 0.0001 = 0.01%).

Per-venue taker fees (base/non-VIP tier, verified against each exchange's
own published fee schedule, July 2026):
    Bybit        0.055%
    Binance      0.05%
    OKX          0.05%
    Hyperliquid  0.045%
    Kraken       0.05%
A single flat 0.26%/leg assumption was previously used for every venue --
that number belongs to none of the exchanges this app actually scans (it's
closer to a spot-market rate) and overstated real costs by roughly 5x,
understating every net APY and inflating every breakeven-cycles figure.

CME is intentionally absent from PER_VENUE_FEE_PER_LEG: it's a dated-future
cash-and-carry basis trade, not a spot+perp funding-arb round trip, so this
per-leg taker model doesn't describe its economics at all (CME charges a
flat commission per contract, not a % of notional). CME rows fall back to
FEE_PER_LEG as a conservative placeholder -- see fetchers/traditional.py
for that limitation.
"""
from typing import Optional

FUNDING_PERIODS_PER_YEAR = 3 * 365  # 8h intervals → 1095 per year

# Fee structure -- FEE_PER_LEG is now only a fallback default for venues
# not present in PER_VENUE_FEE_PER_LEG below (i.e. CME, or any future
# venue added without an updated fee entry).
FEE_PER_LEG = 0.0026          # 0.26% taker per leg (conservative fallback)
LEGS = 4                       # open spot + open perp + close spot + close perp
SLIPPAGE = 0.0005              # 0.05% one-way slippage assumption
TOTAL_ROUND_TRIP_COST = FEE_PER_LEG * LEGS + SLIPPAGE  # 1.09% -- fallback total

# Real published base-tier taker fees per venue this app scans.
PER_VENUE_FEE_PER_LEG: dict[str, float] = {
    "bybit": 0.00055,
    "binance": 0.0005,
    "okx": 0.0005,
    "hyperliquid": 0.00045,
    "kraken": 0.0005,
}


def fee_per_leg(exchange: Optional[str]) -> float:
    """Real venue taker fee if known, else the conservative flat default."""
    if exchange is None:
        return FEE_PER_LEG
    return PER_VENUE_FEE_PER_LEG.get(exchange, FEE_PER_LEG)


def annualised_gross(rate_8h: float) -> float:
    """Raw funding rate per 8h → annualised gross yield (decimal)."""
    return rate_8h * FUNDING_PERIODS_PER_YEAR


def round_trip_fee_cost(exchange: Optional[str] = None) -> float:
    """
    Pure exchange fee cost (no slippage assumption) — the part of the
    round-trip cost that doesn't vary with position size. Uses the given
    venue's real taker fee if known, else the flat fallback.
    """
    return fee_per_leg(exchange) * LEGS


def round_trip_fee_cost_two_venue(exchange_a: str, exchange_b: str) -> float:
    """
    Round-trip fee cost for a cross-exchange spread trade (short one venue,
    long another): 2 legs (open + close) on each venue, each at that
    venue's own real taker fee.
    """
    return fee_per_leg(exchange_a) * 2 + fee_per_leg(exchange_b) * 2


def net_apy(rate_8h: float, exchange: Optional[str] = None) -> float:
    """
    Fee-adjusted net APY.
    Subtract round-trip cost (amortised over the holding period is wrong —
    fees are paid once on entry/exit, so we deduct them from gross annual
    yield directly as a one-time cost expressed in annual terms).
    Net APY = gross annual - total round-trip cost.
    Negative means the opportunity costs more in fees than it earns.
    `exchange` selects the real per-venue fee; omitted/unknown falls back
    to the flat conservative default.
    """
    total_cost = round_trip_fee_cost(exchange) + SLIPPAGE
    return annualised_gross(rate_8h) - total_cost


def breakeven_cycles(rate_8h: float, exchange: Optional[str] = None) -> Optional[float]:
    """
    Number of 8h funding payments needed to recover round-trip fees.
    Returns None if rate is zero or negative (never breaks even).
    """
    if rate_8h <= 0:
        return None
    total_cost = round_trip_fee_cost(exchange) + SLIPPAGE
    return total_cost / rate_8h


def is_profitable(rate_8h: float, exchange: Optional[str] = None) -> bool:
    """True if net APY is positive after fees."""
    return net_apy(rate_8h, exchange) > 0


def net_apy_at_size(rate_8h: float, slippage_cost: float, exchange: Optional[str] = None) -> float:
    """
    Net APY using an actual position-size-derived slippage cost in place
    of the flat SLIPPAGE placeholder used by net_apy(). Fees stay sourced
    from the venue's real per-leg rate (or the flat fallback) — this does
    not duplicate the fee model, it swaps in a better slippage estimate
    for a given size.
    """
    return annualised_gross(rate_8h) - round_trip_fee_cost(exchange) - slippage_cost


def net_apy_at_size_two_venue(
    rate_8h: float, slippage_cost: float, exchange_a: str, exchange_b: str
) -> float:
    """Two-venue equivalent of net_apy_at_size, for cross-exchange spread pairs."""
    return annualised_gross(rate_8h) - round_trip_fee_cost_two_venue(exchange_a, exchange_b) - slippage_cost
