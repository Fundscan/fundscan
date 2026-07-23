"""
Scanner: pull from all exchange fetchers, compute yields, rank results.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .fetchers import FETCHERS
from . import math as fm

log = logging.getLogger(__name__)

# Rows below this 24h notional volume are dropped from every ranking --
# thin markets produce technically-real but unreliable/untradeable-at-size
# funding rates that would otherwise pollute the top of the board.
MIN_VOLUME_24H_USD = 10_000_000


def _run_fetcher(fetch_fn) -> list[dict]:
    """Run one fetcher, return [] on any exception (isolation guarantee)."""
    try:
        return fetch_fn()
    except Exception as e:
        log.error("Fetcher %s raised unexpectedly: %s", fetch_fn.__name__, e)
        return []


def scan() -> list[dict]:
    """
    Run all exchange fetchers in parallel, merge results, compute yields.

    Each result dict contains:
        exchange, symbol, rate_8h, gross_apy, net_apy,
        breakeven_cycles, is_profitable, fetched_at
    Rows below MIN_VOLUME_24H_USD are dropped before ranking.
    Sorted by net_apy descending (best opportunities first).
    """
    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as pool:
        futures = {pool.submit(_run_fetcher, fn): fn for fn in FETCHERS}
        for future in as_completed(futures):
            raw.extend(future.result())

    raw = [r for r in raw if r.get("volume_24h_usd", 0) >= MIN_VOLUME_24H_USD]

    fetched_at = datetime.now(timezone.utc).isoformat()
    enriched = []
    for row in raw:
        rate = row["rate_8h"]
        exchange = row["exchange"]
        be = fm.breakeven_cycles(rate, exchange)
        enriched.append({
            **row,
            "gross_apy": fm.annualised_gross(rate),
            "net_apy": fm.net_apy(rate, exchange),
            "breakeven_cycles": round(be, 1) if be is not None else None,
            "is_profitable": fm.is_profitable(rate, exchange),
            "fetched_at": fetched_at,
        })

    enriched.sort(key=lambda r: r["net_apy"], reverse=True)
    return enriched
