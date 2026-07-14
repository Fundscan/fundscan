"""
Hyperliquid public funding rate fetcher.
Uses the Info API — no auth required.

Endpoint: POST https://api.hyperliquid.xyz/info
Body:     {"type": "metaAndAssetCtxs"}

Response: [universe_meta, asset_contexts]
  universe_meta.universe = [{name, szDecimals, maxLeverage, ...}, ...]
  asset_contexts         = [{funding, openInterest, dayNtlVlm, markPx, ...}, ...]

funding is the current 1-hour rate (continuously accrued, settled hourly).
We convert to 8h equivalent: rate_8h = hourly_rate * 8.
"""
import logging

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.hyperliquid.xyz/info"
TIMEOUT = 10
TOP_N = 20


def fetch() -> list[dict]:
    """
    Fetch current funding rates for the top N assets on Hyperliquid by
    24h notional volume.

    Returns a list of dicts with keys:
        exchange, symbol, rate_8h, funding_interval_hours, next_funding_time
    Returns [] on any error.
    """
    try:
        with httpx.Client() as client:
            r = client.post(
                BASE,
                json={"type": "metaAndAssetCtxs"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()

        universe: list[dict] = data[0]["universe"]
        ctxs: list[dict] = data[1]

        pairs = []
        for asset, ctx in zip(universe, ctxs):
            try:
                hourly_rate = float(ctx["funding"])
                vol = float(ctx.get("dayNtlVlm") or 0)
                pairs.append({
                    "name": asset["name"],
                    "hourly_rate": hourly_rate,
                    "volume": vol,
                })
            except (KeyError, ValueError, TypeError):
                continue

        # Rank by 24h volume, take top N
        pairs.sort(key=lambda x: x["volume"], reverse=True)
        top = pairs[:TOP_N]

        return [
            {
                "exchange": "hyperliquid",
                "symbol": f"{p['name']}-PERP",
                # Hyperliquid funding accrues hourly; multiply by 8 for 8h equivalent
                "rate_8h": p["hourly_rate"] * 8,
                "funding_interval_hours": 1,
                "next_funding_time": None,
            }
            for p in top
        ]

    except Exception as e:
        log.error("Hyperliquid fetcher failed: %s", e)
        return []
