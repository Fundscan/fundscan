"""
Binance public funding rate fetcher.
Uses /fapi/v1 (USD-M futures) endpoints — no API key required.
"""
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

BASE = "https://fapi.binance.com"
TIMEOUT = 10
TOP_N = 20


def _top_symbols_by_volume(client: httpx.Client) -> list[str]:
    """Return top N USDT-perp symbols ranked by 24h quote volume."""
    r = client.get(f"{BASE}/fapi/v1/ticker/24hr", timeout=TIMEOUT)
    r.raise_for_status()
    tickers = [t for t in r.json() if t["symbol"].endswith("USDT")]
    tickers.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    return [t["symbol"] for t in tickers[:TOP_N]]


def _current_funding(client: httpx.Client, symbol: str) -> Optional[dict]:
    """Fetch current funding rate for one symbol."""
    r = client.get(
        f"{BASE}/fapi/v1/fundingRate",
        params={"symbol": symbol, "limit": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    item = items[-1]
    return {
        "exchange": "binance",
        "symbol": symbol,
        "rate_8h": float(item["fundingRate"]),
        "funding_interval_hours": 8,
        "next_funding_time": item.get("fundingTime"),
    }


def fetch() -> list[dict]:
    """
    Fetch current funding rates for top 20 USDT perps on Binance.
    Returns [] on any error so one exchange failing doesn't break others.
    """
    try:
        with httpx.Client() as client:
            symbols = _top_symbols_by_volume(client)
            rows = []
            for symbol in symbols:
                try:
                    row = _current_funding(client, symbol)
                    if row:
                        rows.append(row)
                except Exception as e:
                    log.warning("Binance: skipping %s: %s", symbol, e)
            return rows
    except Exception as e:
        log.error("Binance fetcher failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# OKX template — copy this pattern when adding OKX:
# BASE = "https://www.okx.com"
# def fetch() -> list[dict]:
#     endpoint = "/api/v5/public/funding-rate"
#     ...
# ---------------------------------------------------------------------------
