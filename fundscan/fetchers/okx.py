"""
OKX public funding rate fetcher.
Uses /api/v5/public endpoints — no API key required.
"""
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

BASE = "https://www.okx.com"
TIMEOUT = 10
TOP_N = 20


def _top_symbols_by_volume(client: httpx.Client) -> list[str]:
    """Return top N USDT-swap instruments ranked by 24h volume."""
    r = client.get(
        f"{BASE}/api/v5/market/tickers",
        params={"instType": "SWAP"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX API error: {data.get('msg')}")
    tickers = [t for t in data["data"] if t["instId"].endswith("-USDT-SWAP")]
    tickers.sort(key=lambda t: float(t.get("volCcy24h") or 0), reverse=True)
    return [t["instId"] for t in tickers[:TOP_N]]


def _current_funding(client: httpx.Client, inst_id: str) -> Optional[dict]:
    """Fetch current funding rate for one instrument."""
    r = client.get(
        f"{BASE}/api/v5/public/funding-rate",
        params={"instId": inst_id},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0" or not data.get("data"):
        return None
    item = data["data"][0]
    # OKX uses instId like BTC-USDT-SWAP; normalise to BTCUSDT for consistency
    symbol = inst_id.replace("-USDT-SWAP", "USDT")
    return {
        "exchange": "okx",
        "symbol": symbol,
        "rate_8h": float(item["fundingRate"]),
        "funding_interval_hours": 8,
        "next_funding_time": item.get("nextFundingTime"),
    }


def fetch() -> list[dict]:
    """
    Fetch current funding rates for top 20 USDT-swap perps on OKX.
    Returns [] on any error so one exchange failing doesn't break others.
    """
    try:
        with httpx.Client() as client:
            symbols = _top_symbols_by_volume(client)
            rows = []
            for inst_id in symbols:
                try:
                    row = _current_funding(client, inst_id)
                    if row:
                        rows.append(row)
                except Exception as e:
                    log.warning("OKX: skipping %s: %s", inst_id, e)
            return rows
    except Exception as e:
        log.error("OKX fetcher failed: %s", e)
        return []
