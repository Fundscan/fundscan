"""
Risk-free benchmark rate: what cash pays, for anchoring every yield on
the board. Every net APY figure begs the question "compared to what?" --
this is the answer professionals use: the 13-week US T-bill yield.

Cached for 6 hours: the T-bill yield moves basis points per day; fetching
it more often adds Yahoo calls for no information.
"""
import logging
import time
from typing import Optional

import yfinance as yf

log = logging.getLogger(__name__)

TICKER = "^IRX"  # 13-week US Treasury bill yield, quoted in percent
CACHE_TTL_SECONDS = 6 * 60 * 60

_cache: dict = {"at": 0.0, "rate": None}


def risk_free_rate() -> Optional[float]:
    """Annual risk-free rate as a decimal (0.037 = 3.7%), or None if unavailable."""
    now = time.time()
    if _cache["rate"] is not None and (now - _cache["at"]) < CACHE_TTL_SECONDS:
        return _cache["rate"]
    try:
        quote = yf.Ticker(TICKER).fast_info
        pct = getattr(quote, "last_price", None)
        if pct is None or pct <= 0 or pct > 20:
            raise ValueError(f"implausible ^IRX quote: {pct}")
        _cache["rate"] = pct / 100.0
        _cache["at"] = now
    except Exception as e:
        log.warning("Risk-free rate fetch failed (%s); keeping cached %s", e, _cache["rate"])
        # Serve a stale value forever rather than flapping to None mid-day.
    return _cache["rate"]
