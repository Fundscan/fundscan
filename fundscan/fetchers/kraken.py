"""
Kraken Futures public funding rate fetcher.
Uses the public Futures API — no auth required.

Funding endpoint: GET /derivatives/api/v4/historicalfundingrates?symbol=<SYM>
  Returns `rates`: [{timestamp, fundingRate, relativeFundingRate}, ...], oldest first.
  `fundingRate` is an absolute price-space number (funding payment per contract in
  quote currency) -- NOT a percentage. `relativeFundingRate` is the genuine
  relative rate (fundingRate / markPrice) and is what we want.
  Settled hourly (continuous accrual, like Hyperliquid) -- we convert to an
  8h-equivalent rate the same way the Hyperliquid fetcher does.

Ticker endpoint: GET /derivatives/api/v3/tickers
  Used only for `volumeQuote` (24h notional volume in USD).

Order book: GET /derivatives/api/v3/orderbook?symbol=<SYM>
  Returns bids/asks as [[price, qty], ...] -- but bids come back sorted
  ascending (worst price first), the opposite of what sizing.py's
  `_walk_book` expects ("levels, best price first"). Asks are already
  ascending, which is correctly best-first for asks. We reverse bids only.
"""
import logging

import httpx

log = logging.getLogger(__name__)

BASE = "https://futures.kraken.com/derivatives/api"
TIMEOUT = 10

# The only pairs KKB can currently trade (BTC/GBP, ETH/GBP, SOL/GBP via
# Kraken spot + these perps). Matches KKB's kkb_config.json capital
# allocation -- adding a symbol here does nothing on KKB's side until its
# own config/instruments list is updated separately.
SYMBOLS = ["PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"]


def _funding_rate_8h(client: httpx.Client, symbol: str) -> float | None:
    r = client.get(
        f"{BASE}/v4/historicalfundingrates",
        params={"symbol": symbol},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rates = r.json().get("rates", [])
    if not rates:
        return None
    latest = rates[-1]  # oldest-first; last entry is most recent
    relative_rate = latest["relativeFundingRate"]
    # Kraken funding settles hourly (continuous accrual) -- convert to the
    # 8h-equivalent the rest of the pipeline expects, same as Hyperliquid.
    return relative_rate * 8


def _order_book(client: httpx.Client, symbol: str) -> dict:
    r = client.get(f"{BASE}/v3/orderbook", params={"symbol": symbol}, timeout=TIMEOUT)
    r.raise_for_status()
    book = r.json().get("orderBook", {})
    bids = [[float(p), float(q)] for p, q in book.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in book.get("asks", [])]
    # Bids come back worst-first; reverse so index 0 is the best (highest) bid.
    bids.sort(key=lambda lvl: lvl[0], reverse=True)
    return {"bids": bids, "asks": asks}


def fetch() -> list[dict]:
    """
    Fetch current funding rates for Kraken's tradeable perps (see SYMBOLS).
    Returns a list of dicts with keys:
        exchange, symbol, rate_8h, funding_interval_hours, next_funding_time,
        volume_24h_usd, order_book
    Returns [] on any error (caller handles gracefully).
    """
    try:
        with httpx.Client() as client:
            r = client.get(f"{BASE}/v3/tickers", timeout=TIMEOUT)
            r.raise_for_status()
            tickers = {t["symbol"]: t for t in r.json().get("tickers", [])}

            rows = []
            for symbol in SYMBOLS:
                try:
                    ticker = tickers.get(symbol)
                    if not ticker:
                        continue
                    rate_8h = _funding_rate_8h(client, symbol)
                    if rate_8h is None:
                        continue
                    try:
                        book = _order_book(client, symbol)
                    except Exception as e:
                        log.warning("Kraken: order book failed for %s: %s", symbol, e)
                        book = {"bids": [], "asks": []}
                    rows.append({
                        "exchange": "kraken",
                        "symbol": symbol,
                        "rate_8h": rate_8h,
                        "funding_interval_hours": 1,
                        "next_funding_time": None,
                        "volume_24h_usd": float(ticker.get("volumeQuote") or 0),
                        "order_book": book,
                    })
                except Exception as e:
                    log.warning("Kraken: skipping %s: %s", symbol, e)
            return rows
    except Exception as e:
        log.error("Kraken fetcher failed: %s", e)
        return []
