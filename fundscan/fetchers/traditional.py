"""
CME equity-index futures fetcher (NQ, ES) via Yahoo Finance.

Unlike the crypto perp fetchers, NQ/ES don't pay periodic funding -- they're
dated quarterly futures that converge to spot at expiry. The economically
equivalent "yield" for a dated future is its annualised cash-and-carry
basis: how rich/cheap it trades vs. the spot index, annualised over the
time left until expiry.

We express that basis as a synthetic rate_8h (basis / FUNDING_PERIODS_PER_YEAR)
so it flows through the existing fee/annualisation/ranking pipeline in
math.py and scanner.py unchanged -- gross_apy, net_apy, breakeven_cycles
and is_profitable all fall out of that one number for free.

Known limitation: Yahoo Finance's public data doesn't include CME order
book depth, so these rows carry an empty order_book. sizing.py treats an
empty book as unfillable (100% slippage per leg), so NQ/ES will show up
looking illiquid/below-cost in the position-sized dashboard view even
though CME futures are in reality some of the deepest markets there are.
Fine for the flat rate_8h/net_apy/CSV/API views; misleading for the
sized view until a real depth feed is wired in.
"""
import logging
from datetime import date
from typing import Optional

import yfinance as yf

from .. import math as fm

log = logging.getLogger(__name__)

# (futures ticker, spot index ticker, display symbol, $ per index point)
CONTRACTS = [
    ("NQ=F", "^NDX", "NQ", 20),
    ("ES=F", "^GSPC", "ES", 50),
]

QUARTERLY_MONTHS = (3, 6, 9, 12)


def _third_friday(year: int, month: int) -> date:
    """Third Friday of the given month -- CME's standard quarterly expiry day."""
    d = date(year, month, 1)
    first_friday = 1 + ((4 - d.weekday()) % 7)
    return date(year, month, first_friday + 14)


def _next_quarterly_expiry(today: date) -> date:
    """Next CME quarterly expiry (Mar/Jun/Sep/Dec, third Friday) on or after today."""
    for year in (today.year, today.year + 1):
        for month in QUARTERLY_MONTHS:
            candidate = _third_friday(year, month)
            if candidate >= today:
                return candidate
    raise RuntimeError("unreachable")  # pragma: no cover


def _basis_row(future_symbol: str, spot_symbol: str, label: str, multiplier: float) -> Optional[dict]:
    future = yf.Ticker(future_symbol).fast_info
    spot = yf.Ticker(spot_symbol).fast_info

    future_price = getattr(future, "last_price", None)
    spot_price = getattr(spot, "last_price", None)
    if not future_price or not spot_price:
        return None

    today = date.today()
    expiry = _next_quarterly_expiry(today)
    days_to_expiry = max((expiry - today).days, 1)

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
    }


def fetch() -> list[dict]:
    """
    Fetch NQ/ES cash-and-carry basis yield, expressed as a synthetic
    rate_8h so it ranks alongside crypto funding rates. Returns [] on
    any error so a Yahoo Finance outage doesn't break the rest of the scan.
    """
    rows = []
    for future_symbol, spot_symbol, label, multiplier in CONTRACTS:
        try:
            row = _basis_row(future_symbol, spot_symbol, label, multiplier)
            if row:
                rows.append(row)
        except Exception as e:
            log.warning("CME/yfinance: skipping %s: %s", label, e)
    return rows
