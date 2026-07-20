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


def _top_symbols_by_volume(client: httpx.Client) -> dict[str, float]:
    """Return top N USDT-perp symbols ranked by 24h quote volume, mapped to that volume in USD."""
    r = client.get(f"{BASE}/fapi/v1/ticker/24hr", timeout=TIMEOUT)
    r.raise_for_status()
    tickers = [t for t in r.json() if t["symbol"].endswith("USDT")]
    tickers.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    return {t["symbol"]: float(t.get("quoteVolume") or 0) for t in tickers[:TOP_N]}


def _order_book(client: httpx.Client, symbol: str) -> dict:
    """Fetch order book depth for one symbol. Returns {bids, asks} as [[price, qty], ...] floats."""
    r = client.get(
        f"{BASE}/fapi/v1/depth",
        params={"symbol": symbol, "limit": 50},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return {
        "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
        "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
    }


def _current_funding(client: httpx.Client, symbol: str, volume: float) -> Optional[dict]:
    """Fetch current funding rate + order book for one symbol."""
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
    try:
        book = _order_book(client, symbol)
    except Exception as e:
        log.warning("Binance: order book failed for %s: %s", symbol, e)
        book = {"bids": [], "asks": []}
    return {
        "exchange": "binance",
        "symbol": symbol,
        "rate_8h": float(item["fundingRate"]),
        "funding_interval_hours": 8,
        "next_funding_time": item.get("fundingTime"),
        "volume_24h_usd": volume,
        "order_book": book,
    }


def fetch() -> list[dict]:
    """
    Fetch current funding rates for top 20 USDT perps on Binance.
    Returns [] on any error so one exchange failing doesn't break others.
    """
    try:
        with httpx.Client() as client:
            volumes = _top_symbols_by_volume(client)
            rows = []
            for symbol, volume in volumes.items():
                try:
                    row = _current_funding(client, symbol, volume)
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
