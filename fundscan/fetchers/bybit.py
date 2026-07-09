"""
Bybit public funding rate fetcher.
Uses v5 market endpoints — no API key required.
"""
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.bybit.com"
TIMEOUT = 10  # seconds
TOP_N = 20


def _get(client: httpx.Client, path: str, params: dict) -> Any:
    r = client.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError(f"Bybit API error: {data.get('retMsg')}")
    return data["result"]


def _top_symbols_by_volume(client: httpx.Client) -> list[str]:
    """Return top N USDT-perp symbols ranked by 24h turnover."""
    result = _get(client, "/v5/market/tickers", {"category": "linear"})
    tickers = [
        t for t in result["list"]
        if t["symbol"].endswith("USDT")
    ]
    tickers.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
    return [t["symbol"] for t in tickers[:TOP_N]]


def fetch() -> list[dict]:
    """
    Fetch current funding rates for top 20 USDT perps on Bybit.
    Returns a list of dicts with keys:
        exchange, symbol, rate_8h, funding_interval_hours, next_funding_time
    Returns [] on any error (caller handles gracefully).
    """
    try:
        with httpx.Client() as client:
            symbols = _top_symbols_by_volume(client)
            rows = []
            for symbol in symbols:
                try:
                    result = _get(
                        client,
                        "/v5/market/funding/history",
                        {"category": "linear", "symbol": symbol, "limit": 1},
                    )
                    items = result.get("list", [])
                    if not items:
                        continue
                    item = items[0]
                    rows.append({
                        "exchange": "bybit",
                        "symbol": symbol,
                        "rate_8h": float(item["fundingRate"]),
                        "funding_interval_hours": 8,
                        "next_funding_time": item.get("fundingRateTimestamp"),
                    })
                except Exception as e:
                    log.warning("Bybit: skipping %s: %s", symbol, e)
            return rows
    except Exception as e:
        log.error("Bybit fetcher failed: %s", e)
        return []


def fetch_history(symbol: str, days: int = 30) -> list[dict]:
    """
    Fetch historical funding rates for a single symbol.
    Bybit returns up to 200 records per call; we page to cover `days`.
    Returns list of {timestamp_ms, rate_8h}.
    """
    limit = min(days * 3, 200)  # 3 funding events per day max
    try:
        with httpx.Client() as client:
            result = _get(
                client,
                "/v5/market/funding/history",
                {"category": "linear", "symbol": symbol, "limit": limit},
            )
            return [
                {
                    "timestamp_ms": int(item["fundingRateTimestamp"]),
                    "rate_8h": float(item["fundingRate"]),
                }
                for item in result.get("list", [])
            ]
    except Exception as e:
        log.error("Bybit history fetch failed for %s: %s", symbol, e)
        return []
