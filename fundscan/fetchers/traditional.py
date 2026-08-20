"""
CME futures basis fetcher (equity index NQ/ES + FX carry) via Yahoo Finance.

Unlike the crypto perp fetchers, these don't pay periodic funding -- they're
dated quarterly futures that converge to spot at expiry. The economically
equivalent "yield" for a dated future is its annualised cash-and-carry
basis: how rich/cheap it trades vs. spot, annualised over the time left
until expiry. For the FX contracts that basis is, by covered interest
parity, the interest-rate differential between the two currencies -- i.e.
a live, market-implied FX carry signal.

We express that basis as a synthetic rate_8h (basis / FUNDING_PERIODS_PER_YEAR)
so it flows through the existing fee/annualisation/ranking pipeline in
math.py and scanner.py unchanged -- gross_apy, net_apy, breakeven_cycles
and is_profitable all fall out of that one number for free.

Known limitation: Yahoo Finance's public data doesn't include CME order
book depth, so these rows carry an empty order_book. sizing.py treats an
empty book as unknowable — net_apy_at_size comes back None and the sized
dashboard view shows "n/a" (ranked last) instead of a fabricated slippage
penalty. The flat rate_8h/net_apy/CSV/API views are unaffected. A real
depth feed is needed before these rows can be sized honestly.
"""
import logging
from datetime import date, timedelta
from typing import Optional

import yfinance as yf

from .. import math as fm

log = logging.getLogger(__name__)

# (futures ticker, spot ticker, display symbol, notional multiplier,
#  annual spot-leg carry cost, expiry rule)
#
# round_trip_cost for a CME cash-and-carry is NOT the crypto taker-fee
# model. The real trade is: short the future, long the spot equivalent
# (index ETF for NQ/ES, spot FX for the currency contracts), hold to
# expiry. Costs:
#   - Execution (one-off): futures commission+spread both ways plus the
#     spot leg's spread both ways. On ~$100k+ notional these total well
#     under 0.02% -- we book 0.02% as a conservative round figure.
#   - Spot-leg carry (annual): the ETF's expense ratio for equity index
#     contracts (QQQ 0.20%/yr, SPY 0.0945%/yr) -- the dominant real cost
#     there. Spot FX has no expense ratio (carry差 is the signal itself).
# Margin financing and tracking error are the trader's own variables and
# are deliberately not guessed at here.
#
# For the FX contracts the basis IS the carry: by covered interest parity
# the futures/spot gap equals the interest-rate differential between the
# two currencies, so these rows are a live, market-implied FX carry signal
# with no hand-maintained policy-rate table to go stale.
EXECUTION_COST_ROUND_TRIP = 0.0002  # 0.02%

EQUITY = "equity"  # expires 3rd Friday of Mar/Jun/Sep/Dec
FX = "fx"          # terminates 2 business days before 3rd Wednesday

CONTRACTS = [
    ("NQ=F", "^NDX", "NQ", 20, 0.0020, EQUITY),      # QQQ expense 0.20%/yr
    ("ES=F", "^GSPC", "ES", 50, 0.000945, EQUITY),   # SPY expense 0.0945%/yr
    # Slash-free display symbols: row symbols become URL path segments in
    # /rates/{symbol} and /history/{symbol}, where "EUR/USD" would 404.
    ("6E=F", "EURUSD=X", "EURUSD", 125_000, 0.0, FX),
    ("6B=F", "GBPUSD=X", "GBPUSD", 62_500, 0.0, FX),
    ("6A=F", "AUDUSD=X", "AUDUSD", 100_000, 0.0, FX),
]

QUARTERLY_MONTHS = (3, 6, 9, 12)

# Yahoo's continuous front-month ticker rolls to the next contract around
# expiry week; annualising a basis over the last 1-4 calendar days would
# both divide by a near-zero horizon and likely reference the wrong
# contract. Flooring the horizon keeps roll-week numbers conservative.
MIN_DAYS_TO_EXPIRY = 7


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th <weekday> (Mon=0) of the given month."""
    d = date(year, month, 1)
    first = 1 + ((weekday - d.weekday()) % 7)
    return date(year, month, first + 7 * (n - 1))


def _expiry_for(year: int, month: int, rule: str) -> date:
    if rule == FX:
        # CME FX futures terminate 2 business days before the 3rd Wednesday.
        third_wed = _nth_weekday(year, month, 2, 3)
        return third_wed - timedelta(days=2)  # Wednesday - 2 = Monday
    return _nth_weekday(year, month, 4, 3)  # 3rd Friday


def _next_quarterly_expiry(today: date, rule: str) -> date:
    """Next CME quarterly expiry (Mar/Jun/Sep/Dec) on or after today."""
    for year in (today.year, today.year + 1):
        for month in QUARTERLY_MONTHS:
            candidate = _expiry_for(year, month, rule)
            if candidate >= today:
                return candidate
    raise RuntimeError("unreachable")  # pragma: no cover


def _basis_row(
    future_symbol: str, spot_symbol: str, label: str,
    multiplier: float, expense_ratio: float, rule: str,
) -> Optional[dict]:
    future = yf.Ticker(future_symbol).fast_info
    spot = yf.Ticker(spot_symbol).fast_info

    future_price = getattr(future, "last_price", None)
    spot_price = getattr(spot, "last_price", None)
    if not future_price or not spot_price:
        return None

    today = date.today()
    expiry = _next_quarterly_expiry(today, rule)
    days_to_expiry = max((expiry - today).days, MIN_DAYS_TO_EXPIRY)

    basis = (future_price / spot_price) - 1
    annualised_basis = basis * (365 / days_to_expiry)
    rate_8h = annualised_basis / fm.FUNDING_PERIODS_PER_YEAR

    volume = getattr(future, "last_volume", None) or 0
    volume_24h_usd = volume * future_price * multiplier

    return {
        "exchange": "cme",
        "symbol": label,
        "rate_8h": rate_8h,
        "funding_interval_hours": None,
        "next_funding_time": expiry.isoformat(),
        "volume_24h_usd": volume_24h_usd,
        "order_book": {"bids": [], "asks": []},
        # All-in cost for this trade's real economics (see CONTRACTS note);
        # overrides the crypto taker-fee model in math.py/scanner.py.
        "round_trip_cost": EXECUTION_COST_ROUND_TRIP + expense_ratio,
    }


def fetch() -> list[dict]:
    """
    Fetch NQ/ES cash-and-carry basis yield, expressed as a synthetic
    rate_8h so it ranks alongside crypto funding rates. Returns [] on
    any error so a Yahoo Finance outage doesn't break the rest of the scan.
    """
    rows = []
    for future_symbol, spot_symbol, label, multiplier, expense_ratio, rule in CONTRACTS:
        try:
            row = _basis_row(future_symbol, spot_symbol, label, multiplier, expense_ratio, rule)
            if row:
                rows.append(row)
        except Exception as e:
            log.warning("CME/yfinance: skipping %s: %s", label, e)
    return rows
