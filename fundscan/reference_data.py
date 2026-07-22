"""
Reference data: quote browser across traditional asset classes, available
to both tiers (free gets a curated top-7-per-category slice; pro gets the
full curated universe) and backed by a choice of data source.

Unlike scanner.py, this isn't a funding-rate arbitrage feed -- there's no
"net_apy" concept for most of these (a stock doesn't pay funding). Each
category is just a live price + daily change table.

Sources (see PROVIDERS below) -- same two for both tiers, no API key
needed for either. Pro vs free is purely how many instruments per
category (see FREE_* lists vs the full lists below), not source count:
  - yahoo:  yfinance. Covers every category.
  - nasdaq: api.nasdaq.com (stocks/ETFs) + frankfurter.app/ECB (forex).
            Doesn't cover futures/commodities/bonds/options.

Options data always comes from Yahoo regardless of the selected source --
Nasdaq/ECB doesn't expose option chains.

CFDs and spread bets aren't a separate instrument universe -- they're
broker account wrappers (IG, CMC Markets, Plus500, etc.) around the same
underlying stocks/forex/commodities prices. There's no feed for "CFD
price" that differs from the underlying, so those two categories reuse
the combined underlying data with a note explaining why.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx
import yfinance as yf

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 120
_cache: dict[str, tuple[float, list[dict]]] = {}

# (key, label) -- same two sources for both tiers, no API key needed.
PROVIDERS = [
    ("yahoo", "Yahoo Finance"),
    ("nasdaq", "Nasdaq / ECB"),
]
VALID_SOURCES = {key for key, _ in PROVIDERS}

# ---------------------------------------------------------------------------
# Pro-tier curated universes -- as broad as a single bulk call can
# reasonably cover for free. Not "every listed instrument in the world"
# (that needs a paid feed); this is the practical ceiling on free data.
# ---------------------------------------------------------------------------

STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "LLY", "V",
    "UNH", "JPM", "XOM", "JNJ", "PG", "MA", "HD", "MRK", "COST", "ABBV",
    "CVX", "PEP", "KO", "ADBE", "WMT", "CRM", "BAC", "NFLX", "TMO", "ACN",
    "LIN", "MCD", "ABT", "CSCO", "DHR", "WFC", "TXN", "DIS", "VZ", "PM",
    "NEE", "NKE", "CMCSA", "INTC", "AMD", "UPS", "RTX", "QCOM", "HON", "UNP",
    "LOW", "SPGI", "IBM", "CAT", "GE", "AMGN", "BA", "ELV", "SBUX", "DE",
    "PLD", "ISRG", "GS", "BLK", "MDT", "AXP", "NOW", "BKNG", "GILD", "ADI",
    "LMT", "MMC", "SYK", "TJX", "MDLZ", "ADP", "VRTX", "REGN", "CI", "CB",
    "SCHW", "MO", "ZTS", "PGR", "SO", "BSX", "ETN", "BDX", "DUK", "APD",
    "CME", "AON", "ITW", "PANW", "SLB", "TGT", "FDX",
]

ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM",
    "AGG", "BND", "TLT", "IEF", "SHY", "LQD", "HYG", "GLD", "SLV", "USO",
    "UNG", "DBC", "XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB",
    "XLRE", "XLU", "XLC", "SMH", "SOXX", "ARKK", "VNQ", "VYM", "SCHD", "JEPI",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "GDX", "GDXJ", "XOP", "KRE", "IBB", "ICLN",
]

FUTURES = [
    ("ES=F", "ES"), ("NQ=F", "NQ"), ("YM=F", "YM"), ("RTY=F", "RTY"),
    ("CL=F", "CL"), ("BZ=F", "BZ"), ("NG=F", "NG"),
    ("GC=F", "GC"), ("SI=F", "SI"), ("HG=F", "HG"), ("PL=F", "PL"), ("PA=F", "PA"),
    ("ZC=F", "ZC"), ("ZS=F", "ZS"), ("ZW=F", "ZW"),
    ("ZN=F", "ZN"), ("ZB=F", "ZB"), ("ZT=F", "ZT"), ("ZF=F", "ZF"),
    ("6E=F", "6E"), ("6J=F", "6J"), ("6B=F", "6B"), ("6A=F", "6A"), ("6C=F", "6C"),
    ("KC=F", "KC"), ("CT=F", "CT"), ("SB=F", "SB"), ("CC=F", "CC"),
    ("LE=F", "LE"), ("HE=F", "HE"),
]

COMMODITIES = [
    ("CL=F", "Crude Oil (WTI)"), ("BZ=F", "Crude Oil (Brent)"), ("NG=F", "Natural Gas"),
    ("GC=F", "Gold"), ("SI=F", "Silver"), ("HG=F", "Copper"),
    ("PL=F", "Platinum"), ("PA=F", "Palladium"),
    ("ZC=F", "Corn"), ("ZS=F", "Soybeans"), ("ZW=F", "Wheat"),
    ("KC=F", "Coffee"), ("CT=F", "Cotton"), ("SB=F", "Sugar"), ("CC=F", "Cocoa"),
    ("LE=F", "Live Cattle"), ("HE=F", "Lean Hogs"),
]

FOREX = [
    ("EURUSD=X", "EUR/USD"), ("USDJPY=X", "USD/JPY"), ("GBPUSD=X", "GBP/USD"),
    ("USDCHF=X", "USD/CHF"), ("USDCAD=X", "USD/CAD"), ("AUDUSD=X", "AUD/USD"),
    ("NZDUSD=X", "NZD/USD"),
    ("EURGBP=X", "EUR/GBP"), ("EURJPY=X", "EUR/JPY"), ("EURCHF=X", "EUR/CHF"),
    ("EURAUD=X", "EUR/AUD"), ("EURCAD=X", "EUR/CAD"),
    ("GBPJPY=X", "GBP/JPY"), ("GBPCHF=X", "GBP/CHF"), ("GBPAUD=X", "GBP/AUD"), ("GBPCAD=X", "GBP/CAD"),
    ("AUDJPY=X", "AUD/JPY"), ("AUDNZD=X", "AUD/NZD"), ("AUDCAD=X", "AUD/CAD"),
    ("CADJPY=X", "CAD/JPY"), ("CHFJPY=X", "CHF/JPY"), ("NZDJPY=X", "NZD/JPY"),
    ("USDSGD=X", "USD/SGD"), ("USDHKD=X", "USD/HKD"), ("USDMXN=X", "USD/MXN"),
    ("USDZAR=X", "USD/ZAR"), ("USDTRY=X", "USD/TRY"), ("USDCNH=X", "USD/CNH"),
    ("USDSEK=X", "USD/SEK"), ("USDNOK=X", "USD/NOK"), ("USDPLN=X", "USD/PLN"),
]

BONDS = [
    ("^IRX", "US 13-Week T-Bill Yield"), ("^FVX", "US 5Y Treasury Yield"),
    ("^TNX", "US 10Y Treasury Yield"), ("^TYX", "US 30Y Treasury Yield"),
    ("SHY", "1-3Y Treasury ETF (proxy)"), ("IEF", "7-10Y Treasury ETF (proxy)"),
    ("TLT", "20Y+ Treasury ETF (proxy)"), ("BND", "US Total Bond Market ETF (proxy)"),
]

# Most-active underlyings for options -- "every strike/expiry" is only
# tractable per-underlying; there's no free feed for "every option contract
# in existence" (that's tens of millions of strike/expiry combinations).
OPTIONS_UNDERLYINGS = ["SPY", "QQQ", "AAPL", "MSFT", "TSLA", "NVDA", "AMZN", "IWM"]

# ---------------------------------------------------------------------------
# Free-tier universes -- 7 best-known per category.
# ---------------------------------------------------------------------------

FREE_STOCKS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
FREE_ETFS = ["SPY", "QQQ", "IWM", "VTI", "GLD", "TLT", "DIA"]
FREE_FUTURES = [
    ("ES=F", "ES"), ("NQ=F", "NQ"), ("CL=F", "CL"),
    ("GC=F", "GC"), ("ZN=F", "ZN"), ("6E=F", "6E"), ("SI=F", "SI"),
]
FREE_COMMODITIES = [
    ("GC=F", "Gold"), ("SI=F", "Silver"), ("CL=F", "Crude Oil (WTI)"),
    ("NG=F", "Natural Gas"), ("HG=F", "Copper"), ("ZC=F", "Corn"), ("ZW=F", "Wheat"),
]
FREE_FOREX = [
    ("EURUSD=X", "EUR/USD"), ("USDJPY=X", "USD/JPY"), ("GBPUSD=X", "GBP/USD"),
    ("USDCHF=X", "USD/CHF"), ("USDCAD=X", "USD/CAD"), ("AUDUSD=X", "AUD/USD"),
    ("NZDUSD=X", "NZD/USD"),
]
FREE_BONDS = [
    ("^IRX", "US 13-Week T-Bill Yield"), ("^FVX", "US 5Y Treasury Yield"),
    ("^TNX", "US 10Y Treasury Yield"), ("^TYX", "US 30Y Treasury Yield"),
    ("SHY", "1-3Y Treasury ETF (proxy)"), ("IEF", "7-10Y Treasury ETF (proxy)"),
    ("TLT", "20Y+ Treasury ETF (proxy)"),
]
FREE_OPTIONS_UNDERLYINGS = ["SPY", "QQQ", "AAPL", "MSFT", "TSLA", "NVDA", "AMZN"]

CFD_STOCK_CAP = {"free": 7, "pro": 30}


def _cached(key: str) -> Optional[list[dict]]:
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _store(key: str, rows: list[dict]) -> list[dict]:
    _cache[key] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------------------
# Source: Yahoo Finance (yfinance) -- free, no key, covers every category
# ---------------------------------------------------------------------------

def _yahoo_quotes(pairs: list[tuple[str, str]]) -> list[dict]:
    """One bulk yfinance download for the whole category instead of N sequential calls."""
    yahoo_tickers = [p[0] for p in pairs]
    label_by_ticker = dict(pairs)

    try:
        df = yf.download(
            tickers=yahoo_tickers, period="5d", interval="1d",
            group_by="ticker", threads=True, progress=False,
        )
    except Exception as e:
        log.error("Yahoo bulk quote download failed: %s", e)
        return []

    rows = []
    single = len(yahoo_tickers) == 1
    for ticker in yahoo_tickers:
        try:
            sub = df if single else df[ticker]
            closes = sub["Close"].dropna()
            if len(closes) < 1:
                continue
            price = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
            change_pct = ((price / prev) - 1) * 100 if prev else None
            rows.append({
                "symbol": label_by_ticker[ticker],
                "ticker": ticker,
                "price": round(price, 4),
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
            })
        except Exception as e:
            log.warning("Skipping %s in Yahoo bulk quote: %s", ticker, e)
    return rows


# ---------------------------------------------------------------------------
# Source: Nasdaq public API (stocks/ETFs) + Frankfurter/ECB (forex) --
# free, no key, but doesn't cover futures/commodities/bonds/options.
# ---------------------------------------------------------------------------

def _nasdaq_single_quote(ticker: str, label: str, assetclass: str) -> Optional[dict]:
    try:
        r = httpx.get(
            f"https://api.nasdaq.com/api/quote/{ticker}/info",
            params={"assetclass": assetclass},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=10,
        )
        data = r.json().get("data")
        if not data:
            return None
        primary = data.get("primaryData") or {}
        price = float(str(primary.get("lastSalePrice", "")).replace("$", "").replace(",", ""))
        change_pct = float(str(primary.get("percentageChange", "")).replace("%", ""))
        return {"symbol": label, "ticker": ticker, "price": price, "change_pct": change_pct}
    except Exception as e:
        log.warning("Nasdaq quote failed for %s: %s", ticker, e)
        return None


def _nasdaq_quotes(pairs: list[tuple[str, str]], assetclass: str) -> list[dict]:
    rows = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_nasdaq_single_quote, t, label, assetclass): t for t, label in pairs}
        for fut in as_completed(futures):
            row = fut.result()
            if row:
                rows.append(row)
    return rows


def _frankfurter_forex_quotes(pairs: list[tuple[str, str]]) -> list[dict]:
    """
    ECB reference rates via frankfurter.app -- free, no key. Daily rates
    only (ECB publishes once/day), so change_pct isn't available here;
    that's a real limitation of this source vs. Yahoo's intraday quotes.
    """
    by_base: dict[str, list[tuple[str, str, str]]] = {}
    for ticker, label in pairs:
        base, _, quote = label.partition("/")
        by_base.setdefault(base, []).append((quote, label, ticker))

    rows = []
    for base, entries in by_base.items():
        quotes = ",".join(q for q, _, _ in entries)
        try:
            r = httpx.get(
                "https://api.frankfurter.app/latest",
                params={"from": base, "to": quotes}, timeout=10, follow_redirects=True,
            )
            rates = r.json().get("rates", {})
        except Exception as e:
            log.warning("Frankfurter failed for base %s: %s", base, e)
            continue
        for quote, label, ticker in entries:
            price = rates.get(quote)
            if price is None:
                continue
            rows.append({"symbol": label, "ticker": ticker, "price": round(price, 6), "change_pct": None})
    return rows


def _pairs_for_category(category: str, tier: str) -> list[tuple[str, str]]:
    free = tier == "free"
    if category == "stocks":
        return [(s, s) for s in (FREE_STOCKS if free else STOCKS)]
    if category == "etfs":
        return [(s, s) for s in (FREE_ETFS if free else ETFS)]
    if category == "futures":
        return FREE_FUTURES if free else FUTURES
    if category == "commodities":
        return FREE_COMMODITIES if free else COMMODITIES
    if category == "forex":
        return FREE_FOREX if free else FOREX
    if category == "bonds":
        return FREE_BONDS if free else BONDS
    raise ValueError(f"no pairs for category {category}")


def fetch_category(category: str, source: str = "yahoo", tier: str = "pro") -> tuple[list[dict], Optional[str]]:
    """
    Returns (rows, note). `note` is set (rows == []) when the
    category/source combination isn't available rather than silently
    returning nothing.
    """
    if category in ("cfds", "spread_bets"):
        cap = CFD_STOCK_CAP["free" if tier == "free" else "pro"]
        stocks, note = fetch_category("stocks", source, tier)
        if note:
            return [], note
        forex, _ = fetch_category("forex", source, tier)
        commodities, _ = fetch_category("commodities", source, tier)
        return stocks[:cap] + forex + commodities, None

    if source not in VALID_SOURCES:
        return [], f"Unknown source {source}"

    if source == "nasdaq" and category not in ("stocks", "etfs", "forex"):
        label = dict(PROVIDERS)["nasdaq"]
        return [], f"{category.title()} isn't available from {label} — try Yahoo Finance."

    cache_key = f"{category}:{source}:{tier}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached, None

    pairs = _pairs_for_category(category, tier)

    if source == "yahoo":
        rows = _yahoo_quotes(pairs)
    else:
        if category == "forex":
            rows = _frankfurter_forex_quotes(pairs)
        else:
            rows = _nasdaq_quotes(pairs, "etf" if category == "etfs" else "stocks")

    return _store(cache_key, rows), None


def fetch_options(underlying: str, tier: str = "pro", max_expiries: int = 2) -> list[dict]:
    """
    Every strike, both calls and puts, for the nearest `max_expiries`
    expiration dates of one underlying. Real yfinance option chain data --
    this is genuinely "every single one" for the expiries fetched, just
    scoped to one underlying at a time (a full multi-year chain across
    dozens of underlyings would be tens of thousands of rows per click).
    Always sourced from Yahoo -- no free alternative exposes option chains.
    """
    underlying = underlying.upper()
    if tier == "free":
        max_expiries = 1
    cache_key = f"options:{underlying}:{tier}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    try:
        tk = yf.Ticker(underlying)
        expiries = tk.options[:max_expiries]
    except Exception as e:
        log.warning("Options: could not list expiries for %s: %s", underlying, e)
        return []

    rows = []
    for expiry in expiries:
        try:
            chain = tk.option_chain(expiry)
        except Exception as e:
            log.warning("Options: chain fetch failed for %s %s: %s", underlying, expiry, e)
            continue
        for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
            for _, r in df.iterrows():
                rows.append({
                    "underlying": underlying,
                    "expiry": expiry,
                    "type": opt_type,
                    "strike": float(r["strike"]),
                    "last": float(r["lastPrice"]) if r.get("lastPrice") == r.get("lastPrice") else None,
                    "bid": float(r["bid"]) if r.get("bid") == r.get("bid") else None,
                    "ask": float(r["ask"]) if r.get("ask") == r.get("ask") else None,
                    "volume": int(r["volume"]) if r.get("volume") == r.get("volume") else None,
                    "open_interest": int(r["openInterest"]) if r.get("openInterest") == r.get("openInterest") else None,
                })
    return _store(cache_key, rows)
