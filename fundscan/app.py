"""
FundScan FastAPI application.
Run: uvicorn fundscan.app:app --reload
"""
from dotenv import load_dotenv
load_dotenv()
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncio
import csv
import io
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates

from . import math as fm
from . import sizing
from . import pairing
from .backtest import realized_accuracy
from .db import init_db, insert_snapshots, query_delayed, query_history, query_latest, query_sparklines, get_watchlist, toggle_watchlist
from .scanner import scan
from .alerts import (check_and_send_alerts, check_anomalies, send_daily_digest,
                     check_multi_exchange, check_watchlist_drops,
                     generate_connect_code, link_telegram)
from .onboarding import run_onboarding, run_weekly_report
from .maintenance import run_db_backup, run_db_prune, run_seo_check
from .competitor import run_competitor_monitor, run_reddit_scout
from .auth import (
    SESSION_COOKIE,
    consume_magic_token,
    create_magic_token,
    decode_session_cookie,
    get_or_create_user,
    get_user_by_id,
    make_session_cookie,
    send_magic_link,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL", "60"))  # seconds

# ---------------------------------------------------------------------------
# In-memory state (populated by background task)
# ---------------------------------------------------------------------------
_state: dict = {
    "results": [],
    "last_fetch_at": None,
    "started_at": time.time(),
    "fetch_errors": 0,
}


async def _fetch_loop():
    while True:
        try:
            rows = await asyncio.to_thread(scan)
            _state["results"] = rows
            _state["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
            if rows:
                await asyncio.to_thread(insert_snapshots, rows)
                await asyncio.to_thread(check_and_send_alerts, rows)
                await asyncio.to_thread(check_anomalies, rows)
                await asyncio.to_thread(check_multi_exchange, rows)
                await asyncio.to_thread(check_watchlist_drops, rows)
                await asyncio.to_thread(send_daily_digest, rows)
                await asyncio.to_thread(run_onboarding)
                await asyncio.to_thread(run_weekly_report, rows)
                await asyncio.to_thread(run_db_backup)
                await asyncio.to_thread(run_db_prune)
                await asyncio.to_thread(run_seo_check)
                await asyncio.to_thread(run_competitor_monitor)
                await asyncio.to_thread(run_reddit_scout)
            log.info("Fetched %d rows", len(rows))
        except Exception as e:
            _state["fetch_errors"] += 1
            log.error("Fetch loop error: %s", e)
        await asyncio.sleep(FETCH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = Path(os.getenv("DB_PATH", "fundscan.db"))
    init_db(db_path)
    task = asyncio.create_task(_fetch_loop())
    yield
    task.cancel()


app = FastAPI(title="FundScan", version="0.1.0", lifespan=lifespan)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    uptime_s = int(time.time() - _state["started_at"])
    return {
        "status": "ok",
        "uptime_seconds": uptime_s,
        "last_fetch_at": _state["last_fetch_at"],
        "pairs_tracked": len(_state["results"]),
        "fetch_errors": _state["fetch_errors"],
    }


SITE_URL = "https://fundscan.uk"


@app.get("/rates", response_class=HTMLResponse)
def public_rates(request: Request):
    """
    Public, unauthenticated, SEO-indexable rates page. Fully server-rendered
    (no HTMX needed for initial content) so search engines see the actual
    opportunities table, not a loading skeleton.

    Unlike the gated dashboard, this shows the complete current list from
    _state["results"] with no tier limiting -- the point of a public page
    is maximum crawlable content and a path into the funnel via /auth/request.
    """
    results = _state["results"]
    profitable = [r for r in results if r["is_profitable"]]
    below_cost = [r for r in results if not r["is_profitable"]]
    return templates.TemplateResponse(
        request,
        "rates.html",
        {
            "profitable": profitable,
            "below_cost": below_cost,
            "pairs_count": len(results),
            "fetched_at": _state["last_fetch_at"],
            "site_url": SITE_URL,
            "fee_per_leg_pct": fm.FEE_PER_LEG * 100,
            "legs": fm.LEGS,
            "slippage_pct": fm.SLIPPAGE * 100,
            "total_round_trip_pct": fm.TOTAL_ROUND_TRIP_COST * 100,
        },
    )


@app.get("/sitemap.xml")
def sitemap():
    urls = [
        (f"{SITE_URL}/", "daily", "1.0"),
        (f"{SITE_URL}/rates", "hourly", "0.9"),
    ]
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(
            f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>\n"
            for loc, freq, prio in urls
        )
        + "</urlset>"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /rates\n"
        "Disallow: /app\n"
        "Disallow: /account\n"
        "Disallow: /admin\n"
        "Disallow: /billing\n"
        "Disallow: /api/\n"
        "Disallow: /htmx/\n"
        "Disallow: /auth/verify\n"
        "\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body)


@app.get("/rates/{symbol}")
def rate_detail(request: Request, symbol: str):
    user = _current_user(request)
    results, _locked = _tier_results(user)
    symbol = symbol.upper()
    matches = [r for r in results if r["symbol"] == symbol]
    if not matches:
        raise HTTPException(404, f"Symbol {symbol} not found")
    return {
        "symbol": symbol,
        "exchanges": matches,
        "fee_model": {
            "fee_per_leg": fm.FEE_PER_LEG,
            "legs": fm.LEGS,
            "slippage": fm.SLIPPAGE,
            "total_round_trip_cost": fm.TOTAL_ROUND_TRIP_COST,
        },
    }


@app.get("/history/{symbol}")
def history(symbol: str, days: int = 7):
    """
    `accuracy` compares each exchange's current (most recent) headline net
    APY against its realized average over the window -- see backtest.py.
    Absent from the dict for an exchange with no history yet.
    """
    symbol = symbol.upper()
    rows = query_history(symbol, days)
    accuracy = {}
    for exch in sorted({r["exchange"] for r in rows}):
        acc = realized_accuracy([r for r in rows if r["exchange"] == exch])
        if acc:
            accuracy[exch] = acc
    return {
        "symbol": symbol,
        "days": days,
        "points": [
            {"ts": r["ts"], "exchange": r["exchange"],
             "rate_8h": r["rate_8h"], "net_apy": r["net_apy"]}
            for r in rows
        ],
        "accuracy": accuracy,
    }


@app.get("/api/v1/rates")
def api_v1_rates(request: Request):
    """
    Machine-readable rates endpoint. Pro users get live full list.
    Free/anonymous get top 5, delayed 10 minutes (same as the dashboard).
    net_apy and gross_apy are decimals (0.15 = 15%).
    """
    user = _current_user(request)
    results, locked = _tier_results(user)
    tier = user["tier"] if user else "anonymous"
    return {
        "tier": tier,
        "fetched_at": _state["last_fetch_at"],
        "count": len(results),
        "missing_on_free_tier": len(locked),
        "fee_model": {
            "fee_per_leg_pct": fm.FEE_PER_LEG * 100,
            "legs": fm.LEGS,
            "slippage_pct": fm.SLIPPAGE * 100,
            "total_round_trip_pct": fm.TOTAL_ROUND_TRIP_COST * 100,
        },
        "opportunities": [
            {
                "exchange": r["exchange"],
                "symbol": r["symbol"],
                "rate_8h": r["rate_8h"],
                "gross_apy": r["gross_apy"],
                "net_apy": r["net_apy"],
                "breakeven_cycles": r["breakeven_cycles"],
                "is_profitable": r["is_profitable"],
                "fetched_at": r.get("fetched_at"),
            }
            for r in results
        ],
    }


@app.get("/export/rates.csv")
def export_rates_csv(request: Request):
    """
    Download current rates as CSV. Pro: full live list. Free: top 5, delayed.
    """
    user = _current_user(request)
    results, _locked = _tier_results(user)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "exchange", "symbol", "rate_8h_pct", "gross_apy_pct",
        "net_apy_pct", "breakeven_cycles", "is_profitable", "fetched_at",
    ])
    for r in results:
        writer.writerow([
            r["exchange"],
            r["symbol"],
            f"{r['rate_8h'] * 100:.6f}",
            f"{r['gross_apy'] * 100:.4f}",
            f"{r['net_apy'] * 100:.4f}",
            r["breakeven_cycles"] if r["breakeven_cycles"] is not None else "",
            r["is_profitable"],
            r.get("fetched_at", ""),
        ])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fundscan_rates.csv"},
    )


@app.get("/api/sparklines")
def api_sparklines():
    """
    Batch 24h sparkline data for all tracked pairs.
    Returns {"{exchange}:{symbol}": [net_apy, ...]} (24 points max, oldest-first).
    Used by the dashboard to draw inline trend sparklines without N+1 fetches.
    """
    return query_sparklines(hours=24)


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

@app.get("/api/me")
def api_me(request: Request):
    """Return current user's email and tier."""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Not logged in")
    return {"email": user["email"], "tier": user.get("tier", "free")}


@app.get("/api/watchlist")
def api_watchlist(request: Request):
    """Return current user's watchlist as [{symbol, exchange}]."""
    user = _current_user(request)
    if not user:
        return []
    rows = get_watchlist(user["id"])
    return [{"symbol": r["symbol"], "exchange": r["exchange"]} for r in rows]


@app.post("/watchlist/toggle")
async def watchlist_toggle(request: Request):
    """Toggle a pair on/off the watchlist. Body: {symbol, exchange}."""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    body = await request.json()
    symbol = body.get("symbol", "").upper()
    exchange = body.get("exchange", "").lower()
    if not symbol or not exchange:
        raise HTTPException(400, "symbol and exchange required")
    added = toggle_watchlist(user["id"], symbol, exchange)
    return {"symbol": symbol, "exchange": exchange, "watchlisted": added}


# ---------------------------------------------------------------------------
# Landing page helpers — server-render live data into the template
# ---------------------------------------------------------------------------

# Neutral sparkline path used on the preview board (no per-pair history on landing)
_SPARK_PATH = "M0 14 L14 15 L28 14 L42 15 L56 14 L70 15 L84 14 L100 15"


def _build_strip_html(results: list[dict]) -> str:
    """
    Top 12 pairs by net APY rendered as ticker spans.
    Duplicated so the CSS loop animation is seamless.
    """
    top = results[:12]
    if not top:
        # Placeholder strip when no data yet
        top = [{"symbol": "BTCUSDT", "net_apy": 0.0, "is_profitable": False}] * 3

    items = []
    for r in top:
        sym = r["symbol"].replace("USDT", "")
        if r["is_profitable"]:
            val = (
                f'<span style="color:var(--green)">+{r["net_apy"] * 100:.2f}%</span>'
            )
        else:
            val = '<span style="color:var(--mist)">below cost</span>'
        items.append(f"<span><b>{sym} PERP</b>&nbsp; net {val}</span>")

    doubled = items * 2  # duplicate for seamless CSS loop
    return "".join(doubled)


def _build_board_rows(results: list[dict]) -> str:
    """
    Top 3 profitable pairs + 2 below-cost pairs for the landing preview board.
    Falls back to placeholder rows if no live data yet.
    """
    profitable = [r for r in results if r["is_profitable"]][:3]
    below = [r for r in results if not r["is_profitable"]][:2]

    # Graceful fallback when scanner hasn't run yet
    if not profitable and not below:
        return (
            '<div class="brow dim" style="opacity:1;transform:none">'
            '<span class="sym">Fetching data<small>—</small></span>'
            '<span class="num">—</span><span class="flag">LOADING</span>'
            '<span class="cspark"></span><span class="num">—</span>'
            "</div>"
        )

    rows = []
    for r in profitable:
        sym = r["symbol"].replace("USDT", "")
        ex = r["exchange"].upper()
        rate_str = f'{r["rate_8h"] * 100:.4f}%'
        apy_val = r["net_apy"] * 100
        be = r["breakeven_cycles"]
        be_str = f"{be:.1f} cycles" if be is not None else "—"
        rows.append(
            f'<div class="brow">'
            f'<span class="sym">{sym} Perpetual<small>{ex}</small></span>'
            f'<span class="num">{rate_str}</span>'
            f'<span class="num up" data-tick="{apy_val:.2f}">+0.00%</span>'
            f'<span class="cspark"><svg class="spark" viewBox="0 0 100 26" preserveAspectRatio="none">'
            f'<path d="{_SPARK_PATH}"/></svg></span>'
            f'<span class="num">{be_str}</span>'
            f"</div>"
        )

    for r in below:
        sym = r["symbol"].replace("USDT", "")
        ex = r["exchange"].upper()
        rate_str = f'{r["rate_8h"] * 100:.4f}%'
        rows.append(
            f'<div class="brow dim">'
            f'<span class="sym">{sym} Perpetual<small>{ex}</small></span>'
            f'<span class="num">{rate_str}</span>'
            f'<span class="flag">BELOW COST</span>'
            f'<span class="cspark"><svg class="spark" viewBox="0 0 100 26" preserveAspectRatio="none">'
            f'<path d="{_SPARK_PATH}"/></svg></span>'
            f'<span class="num">—</span>'
            f"</div>"
        )

    return "\n".join(rows)


def _current_user(request: Request) -> Optional[dict]:
    """Return the logged-in user dict or None."""
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    uid = decode_session_cookie(cookie)
    if uid is None:
        return None
    return get_user_by_id(uid)


FREE_TIER_LIMIT = 25


def _tier_results(user: Optional[dict]) -> tuple[list[dict], list[dict]]:
    """
    Return (visible_results, locked_results).

    Pro  → (full list, [])
    Free → (top 5, remaining rows as blurred placeholders)
    """
    results = _state["results"]
    if user and user.get("tier") == "pro":
        return results, []

    if not results:
        return [], []
    return results[:FREE_TIER_LIMIT], results[FREE_TIER_LIMIT:]


# ---------------------------------------------------------------------------
# HTMX dashboard
# ---------------------------------------------------------------------------

def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


_LIQ_LABELS = {"green": "LIQUID", "amber": "THIN", "red": "ILLIQUID"}


def _liquidity_badge(r: dict) -> str:
    flag = r.get("liquidity_flag", "red")
    pct = r.get("liquidity_pct")
    title = f"{pct * 100:.2f}% of 24h volume" if pct is not None else "24h volume unknown"
    return f'<span class="liq-badge liq-{flag}" title="{title}">{_LIQ_LABELS[flag]}</span>'


def _render_table_rows(results: list[dict], locked: list[dict] | None = None) -> str:
    if not results and not locked:
        return (
            '<tr class="data-row"><td colspan="8" '
            'style="text-align:center;color:var(--mist);font-family:var(--mono);'
            'font-size:12px;letter-spacing:.06em;padding:2rem 1rem">'
            'Fetching data…</td></tr>'
        )

    rows = []
    for r in results:
        # Headline metric is net-yield-at-size where available (sized rows
        # carry net_apy_at_size); falls back to the flat net_apy otherwise.
        net_at_size = r.get("net_apy_at_size", r["net_apy"])
        profitable = net_at_size > 0
        row_cls = "data-row" if profitable else "data-row greyed"
        net_cls = "num pos" if profitable else "num neg"
        gross_label = _pct(r["gross_apy"])
        net_label = _pct(net_at_size)
        be = r["breakeven_cycles"]
        be_str = f"{be:.1f}" if be is not None else "∞"
        spark_key = f"{r['exchange']}:{r['symbol']}"
        safe_id = f"{r['exchange']}-{r['symbol']}".replace("/", "_")
        exch = r["exchange"].upper()
        rows.append(
            f'<tr class="{row_cls}" data-symbol="{r["symbol"]}" data-exchange="{r["exchange"]}"'
            f' data-apy="{net_at_size}" onclick="toggleChart(this)">'
            f'<td style="padding:.7rem .5rem .7rem .75rem" onclick="event.stopPropagation()">'
            f'<button class="star-btn" id="star-{safe_id}" '
            f'data-symbol="{r["symbol"]}" data-exchange="{r["exchange"]}" '
            f'onclick="toggleWatchlist(this)" title="Watch">☆</button>'
            f'</td>'
            f'<td><span class="sym-name">{r["symbol"]}</span></td>'
            f'<td><span class="exch-badge exch-{r["exchange"]}">{exch}</span></td>'
            f'<td><span class="num">{_pct(r["rate_8h"])}</span></td>'
            f'<td><span class="gross-strike">{gross_label}</span>'
            f'<span class="{net_cls}" style="font-weight:600">{net_label}</span></td>'
            f'<td>{_liquidity_badge(r)}</td>'
            f'<td><span class="brkeven">{be_str} cycles</span></td>'
            f'<td class="spark-cell"><canvas id="spark-{safe_id}" data-spark-key="{spark_key}" width="80" height="28"></canvas></td>'
            f'</tr>'
            f'<tr class="chart-row" id="chart-{r["symbol"]}-{r["exchange"]}" style="display:none">'
            f'<td colspan="8">'
            f'<div class="chart-inner">'
            f'<div class="accuracy-line" id="accuracy-{r["symbol"]}-{r["exchange"]}"></div>'
            f'<canvas id="canvas-{r["symbol"]}-{r["exchange"]}" height="90"></canvas>'
            f'</div>'
            f'</td>'
            f'</tr>'
        )

    # Blurred Pro-only rows
    if locked:
        for r in locked:
            exch = r["exchange"].upper()
            net_at_size = r.get("net_apy_at_size", r["net_apy"])
            gross_label = _pct(r["gross_apy"])
            net_label = _pct(net_at_size)
            be = r["breakeven_cycles"]
            be_str = f"{be:.1f}" if be is not None else "∞"
            rows.append(
                f'<tr class="data-row locked-row" onclick="document.location=\'/billing/checkout\'">'
                f'<td style="padding:.7rem .5rem .7rem .75rem"><span style="color:var(--mist)">☆</span></td>'
                f'<td><span class="sym-name locked-blur">{r["symbol"]}</span></td>'
                f'<td><span class="exch-badge exch-{r["exchange"]} locked-blur">{exch}</span></td>'
                f'<td><span class="num locked-blur">{_pct(r["rate_8h"])}</span></td>'
                f'<td><span class="gross-strike locked-blur">{gross_label}</span>'
                f'<span class="num pos locked-blur" style="font-weight:600">{net_label}</span></td>'
                f'<td class="locked-blur">{_liquidity_badge(r)}</td>'
                f'<td><span class="brkeven locked-blur">{be_str} cycles</span></td>'
                f'<td></td>'
                f'</tr>'
            )
        rows.append(
            f'<tr><td colspan="8" style="padding:.75rem 1rem 1.25rem;text-align:center">'
            f'<a href="/billing/checkout" class="pro-unlock-btn">'
            f'Unlock {len(locked)} more pairs — Upgrade to Pro</a>'
            f'</td></tr>'
        )

    return "\n".join(rows)


def _render_pair_rows(pairs: list[dict]) -> str:
    """Cross-exchange spread board rows: same asset, short the richer venue,
    long the cheaper one. Rendered by /htmx/pairs."""
    if not pairs:
        return (
            '<tr class="data-row"><td colspan="6" '
            'style="text-align:center;color:var(--mist);font-family:var(--mono);'
            'font-size:12px;letter-spacing:.06em;padding:2rem 1rem">'
            'No cross-exchange spreads yet…</td></tr>'
        )

    rows = []
    for p in pairs:
        net_at_size = p["net_apy_at_size"]
        profitable = net_at_size > 0
        row_cls = "data-row" if profitable else "data-row greyed"
        net_cls = "num pos" if profitable else "num neg"
        gross_label = _pct(p["gross_apy"])
        net_label = _pct(net_at_size)
        short_ex, long_ex = p["short_exchange"], p["long_exchange"]
        rows.append(
            f'<tr class="{row_cls}">'
            f'<td><span class="sym-name">{p["asset"]}</span></td>'
            f'<td><span class="exch-badge exch-{short_ex}">SHORT {short_ex.upper()}</span></td>'
            f'<td><span class="exch-badge exch-{long_ex}">LONG {long_ex.upper()}</span></td>'
            f'<td><span class="num">{_pct(p["spread_rate_8h"])}</span></td>'
            f'<td><span class="gross-strike">{gross_label}</span>'
            f'<span class="{net_cls}" style="font-weight:600">{net_label}</span></td>'
            f'<td>{_liquidity_badge(p)}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _render_summary(results: list[dict]) -> str:
    profitable = [r for r in results if r["is_profitable"]]
    all_results = _state["results"]
    all_profitable = [r for r in all_results if r["is_profitable"]]
    best = max((r["net_apy"] for r in profitable), default=None)
    best_str = _pct(best) if best is not None else "—"
    n_pairs = len(all_profitable)
    avg = (sum(r["net_apy"] for r in all_profitable) / n_pairs) if n_pairs else None
    avg_str = _pct(avg) if avg is not None else "—"
    exchanges = len({r["exchange"] for r in all_results})
    return (
        f'<div class="stat"><span class="stat-n">{best_str}</span>'
        f'<span class="stat-l">best net APY</span></div>'
        f'<div class="stat"><span class="stat-n">{n_pairs}</span>'
        f'<span class="stat-l">pairs above fees</span></div>'
        f'<div class="stat"><span class="stat-n">{avg_str}</span>'
        f'<span class="stat-l">avg net APY</span></div>'
        f'<div class="stat"><span class="stat-n">{exchanges}</span>'
        f'<span class="stat-l">exchanges</span></div>'
    )




@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """
    Public homepage: landing page with live data.
    Logged-in users are redirected straight to /app.
    """
    if _current_user(request):
        return RedirectResponse("/app", status_code=302)
    results = _state["results"]
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "strip_html": _build_strip_html(results),
            "board_rows": _build_board_rows(results),
            "pairs_count": len(results),
        },
    )


@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request):
    """Live scanner dashboard. Requires login."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request", status_code=302)
    visible, locked = _tier_results(user)
    exchanges = sorted({r["exchange"] for r in _state["results"]})
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "locked_count": len(locked),
            "exchanges": exchanges,
            "position_sizes": sizing.POSITION_SIZES,
            "default_position_size": sizing.DEFAULT_POSITION_SIZE,
        },
    )


# ---------------------------------------------------------------------------
# Auth routes — magic link flow
# ---------------------------------------------------------------------------

_AUTH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FundScan — Sign in</title>
<style>
  body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
  .box{{width:100%;max-width:380px;padding:2.5rem;border:1px solid rgba(167,178,196,.13);
       border-radius:8px;background:#0F1B30}}
  h1{{font-size:1.2rem;margin-bottom:.5rem}}
  p{{color:#A7B2C4;font-size:.9rem;margin-bottom:1.5rem}}
  .err{{color:#D96A6A;font-size:.85rem;margin-bottom:1rem}}
  input{{width:100%;padding:.75rem 1rem;background:#0A1424;border:1px solid rgba(167,178,196,.24);
        border-radius:4px;color:#EEF1F6;font-size:1rem;margin-bottom:1rem}}
  input:focus{{outline:none;border-color:#C9A551}}
  button{{width:100%;padding:.85rem;background:#C9A551;border:none;border-radius:4px;
         color:#221A08;font-weight:600;font-size:1rem;cursor:pointer}}
  .back{{display:block;margin-top:1.25rem;text-align:center;color:#67748A;font-size:.85rem;text-decoration:none}}
  .back:hover{{color:#C9A551}}
</style>
</head>
<body>
<div class="box">
  <h1>Sign in to FundScan</h1>
  <p>Enter your email and we'll send a one-time sign-in link.</p>
  {error}
  <form method="post" action="/auth/request">
    <input type="email" name="email" placeholder="you@example.com" required autofocus>
    <button type="submit">Send sign-in link</button>
  </form>
  <a href="/" class="back">← Back to home</a>
</div>
</body>
</html>"""


@app.get("/auth/request", response_class=HTMLResponse)
def auth_request_page(request: Request, error: str = ""):
    if _current_user(request):
        return RedirectResponse("/app", status_code=302)
    err_html = f'<p class="err">{error}</p>' if error else ""
    return _AUTH_PAGE.format(error=err_html)


@app.post("/auth/request", response_class=HTMLResponse)
async def auth_request_submit(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    if not email:
        return RedirectResponse("/auth/request", status_code=302)

    get_or_create_user(email)
    token = create_magic_token(email)

    resend_key = os.getenv("RESEND_API_KEY", "")
    if resend_key:
        try:
            send_magic_link(email, token)
        except Exception as e:
            log.error("Failed to send magic link to %s: %s", email, e)
            # Don't leak whether send failed — show same page
    else:
        # Dev mode: log the link so you can click it without Resend configured
        base = os.getenv("BASE_URL", "http://localhost:8000")
        log.warning("DEV — magic link for %s: %s/auth/verify?token=%s", email, base, token)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Check your email — FundScan</title>
<style>body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{max-width:400px;padding:2.5rem;border:1px solid rgba(167,178,196,.13);border-radius:8px;background:#0F1B30}}
p{{color:#A7B2C4;margin:.75rem 0;font-size:.95rem}}a{{color:#C9A551;text-decoration:none}}</style></head>
<body><div class="box">
<h1 style="font-size:1.2rem;margin-bottom:.5rem">Check your inbox</h1>
<p>A sign-in link has been sent to <strong style="color:#EEF1F6">{email}</strong>.</p>
<p>The link expires in 15 minutes. Check your spam folder if it doesn't arrive.</p>
<p style="margin-top:1.5rem"><a href="/">← Back to home</a></p>
</div></body></html>"""


@app.get("/auth/verify")
def auth_verify(request: Request, token: str = ""):
    """Consume magic link token, set session cookie, redirect to /app."""
    if not token:
        return RedirectResponse("/auth/request?error=Missing+token", status_code=302)

    email = consume_magic_token(token)
    if not email:
        return RedirectResponse(
            "/auth/request?error=Link+expired+or+already+used", status_code=302
        )

    user = get_or_create_user(email)
    cookie = make_session_cookie(user["id"])
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        cookie,
        httponly=True,
        samesite="lax",
        secure=os.getenv("BASE_URL", "").startswith("https"),
        max_age=60 * 60 * 24 * 30,  # 30 days
    )
    return resp


@app.get("/auth/logout")
def auth_logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Billing routes (Stripe)
# ---------------------------------------------------------------------------

@app.get("/billing/checkout")
def billing_checkout(request: Request):
    """Serve our custom embedded checkout page."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request?next=/billing/checkout", status_code=302)
    if user.get("tier") == "pro":
        return RedirectResponse("/account", status_code=302)
    pk = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    return templates.TemplateResponse(request, "checkout.html", {"publishable_key": pk})


@app.post("/api/billing/create-session")
async def billing_create_session(request: Request):
    """Create a Stripe embedded checkout session and return client_secret."""
    user = _current_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    if user.get("tier") == "pro":
        raise HTTPException(400, "Already Pro")

    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    price_id = os.getenv("STRIPE_PRICE_ID", "")
    if not stripe_key or not price_id:
        raise HTTPException(500, "Stripe not configured")

    try:
        from .billing import create_embedded_session
        client_secret = create_embedded_session(user["email"])
        return {"clientSecret": client_secret}
    except Exception as e:
        log.error("Stripe create-session failed for %s:\n%s", user["email"], traceback.format_exc())
        raise HTTPException(500, "Could not load checkout. Please try again or email hello@fundscan.uk.")


@app.get("/billing/return")
def billing_return(request: Request, session_id: str = ""):
    """Post-payment landing page after Stripe embedded checkout completes."""
    user = _current_user(request)
    name = user["email"].split("@")[0] if user else "there"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Welcome to Pro — FundScan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,600&family=Instrument+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--navy:#0A1424;--navy-2:#0F1B30;--ivory:#EEF1F6;--soft:#A7B2C4;--gold:#C9A551;--green:#3FBE8E;
  --hairline:rgba(167,178,196,.13);--serif:'Source Serif 4',Georgia,serif;--sans:'Instrument Sans',system-ui,sans-serif}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--navy);color:var(--ivory);font-family:var(--sans);min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:2rem;-webkit-font-smoothing:antialiased}}
.ambient{{position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:radial-gradient(900px 500px at 50% 50%,rgba(63,190,142,.07),transparent 60%)}}
.card{{max-width:480px;width:100%;text-align:center;padding:3rem 2.5rem;
  background:var(--navy-2);border:1px solid var(--hairline);border-radius:12px}}
.check{{width:64px;height:64px;margin:0 auto 1.5rem;background:rgba(63,190,142,.12);
  border-radius:50%;display:flex;align-items:center;justify-content:center}}
h1{{font-family:var(--serif);font-size:2rem;font-weight:600;margin-bottom:.75rem}}
h1 span{{color:var(--gold)}}
p{{color:var(--soft);font-size:15px;line-height:1.7;margin-bottom:2rem}}
.btn{{display:inline-block;padding:14px 32px;background:var(--gold);color:var(--navy);
  font-weight:600;font-size:15px;text-decoration:none;border-radius:4px;transition:opacity .2s}}
.btn:hover{{opacity:.88}}
</style></head>
<body>
<div class="ambient"></div>
<div class="card">
  <div class="check">
    <svg width="28" height="28" viewBox="0 0 28 28" fill="none"><path d="M6 14l5.5 5.5L22 9" stroke="#3FBE8E" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
  </div>
  <h1>Welcome to <span>Pro</span></h1>
  <p>You're all set, {name}. Your account has been upgraded — all pairs, live alerts, and CSV export are now unlocked.</p>
  <a href="/app" class="btn">Go to dashboard →</a>
</div>
<script>
  // Poll for tier upgrade (webhook may take a few seconds)
  let attempts = 0;
  const check = setInterval(async () => {{
    if (++attempts > 12) return clearInterval(check);
    try {{
      const r = await fetch('/api/me');
      if (r.ok) {{
        const d = await r.json();
        if (d.tier === 'pro') clearInterval(check);
      }}
    }} catch{{}}
  }}, 2500);
</script>
</body></html>""")


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """
    Stripe webhook endpoint.
    Register this URL in Stripe dashboard → Webhooks:
      https://yourdomain.com/billing/webhook
    Events to enable: checkout.session.completed,
                      customer.subscription.deleted,
                      invoice.payment_failed
    """
    from .billing import verify_webhook, handle_webhook
    import stripe

    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook(body, sig)
    except stripe.error.SignatureVerificationError:
        log.warning("Stripe webhook signature verification failed")
        raise HTTPException(400, "Invalid signature")
    except Exception as e:
        log.error("Stripe webhook error: %s", e)
        raise HTTPException(400, str(e))

    try:
        handle_webhook(event)
    except Exception as e:
        log.error("handle_webhook failed for %s: %s", event.get("type"), e)
        # Return 200 anyway — Stripe will retry on 4xx/5xx

    return {"ok": True}


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    """Account page — current plan, upgrade/manage link."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request", status_code=302)

    tier = user["tier"]
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    if tier == "pro":
        plan_html = '<span style="color:#22c55e;font-weight:600">Pro</span>'
        stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
        if stripe_key:
            try:
                from .billing import portal_url
                manage_url = portal_url(user["email"], return_url=f"{base_url}/account")
            except Exception as e:
                log.warning("Stripe portal session failed for %s: %s", user["email"], e)
                manage_url = "/account"
        else:
            manage_url = "/account"
        action_html = f'<a href="{manage_url}" style="color:#C9A551">Manage subscription →</a>'
    else:
        plan_html = "Free"
        action_html = '<a href="/billing/checkout" style="color:#C9A551;font-weight:600">Upgrade to Pro — £20/month →</a>'

    # Telegram section
    from .db import get_conn as _get_conn
    with _get_conn() as _conn:
        tg_row = _conn.execute(
            "SELECT telegram_chat_id, min_net_apy FROM alert_configs WHERE user_id = ? AND (symbol IS NULL OR telegram_chat_id NOT LIKE 'PENDING:%')",
            (user["id"],)
        ).fetchone()

    if tg_row and tg_row["telegram_chat_id"] and not tg_row["telegram_chat_id"].startswith("PENDING:"):
        tg_html = f"""
<div class="row"><span class="label">Telegram</span><span style="color:#22c55e">Connected ✓</span></div>
<div class="row"><span class="label">Alert threshold</span>
  <form method="post" action="/account/alert-threshold" style="display:flex;gap:.5rem;align-items:center">
    <input name="min_net_apy" type="number" step="1" min="1" max="9999"
      value="{int(tg_row['min_net_apy']*100)}"
      style="width:60px;background:#0A1424;border:1px solid rgba(167,178,196,.2);color:#EEF1F6;padding:.3rem .5rem;border-radius:4px">
    <span style="color:#A7B2C4;font-size:.85rem">% APY</span>
    <button type="submit" style="background:#C9A551;color:#221A08;border:none;padding:.3rem .7rem;border-radius:4px;cursor:pointer;font-weight:600">Save</button>
  </form>
</div>"""
    else:
        code = generate_connect_code(user["id"])
        tg_html = f"""
<div class="row" style="flex-direction:column;align-items:flex-start;gap:.75rem">
  <span class="label">Telegram Alerts <span style="color:#67748A;font-size:.8rem">(optional)</span></span>
  <div style="background:#0A1424;padding:1rem;border-radius:6px;width:100%;box-sizing:border-box">
    <p style="margin:0 0 .5rem;font-size:.9rem">1. Open Telegram and message <a href="https://t.me/fundscanalerts_bot" target="_blank" style="color:#C9A551">@fundscanalerts_bot</a></p>
    <p style="margin:0 0 .75rem;font-size:.9rem">2. Send this command:</p>
    <code style="background:#162030;padding:.4rem .8rem;border-radius:4px;font-size:1rem;letter-spacing:.05em">/connect {code}</code>
  </div>
</div>"""

    # Watchlist section
    wl_rows = get_watchlist(user["id"])
    if wl_rows:
        wl_items = "".join(
            f'<div class="row" style="font-size:.88rem">'
            f'<span>{r["symbol"]} <span style="color:#67748A">· {r["exchange"].title()}</span></span>'
            f'<form method="post" action="/watchlist/remove" style="margin:0">'
            f'<input type="hidden" name="symbol" value="{r["symbol"]}">'
            f'<input type="hidden" name="exchange" value="{r["exchange"]}">'
            f'<button type="submit" style="background:none;border:none;color:#67748A;cursor:pointer;font-size:.8rem;padding:.2rem .4rem">✕</button>'
            f'</form></div>'
            for r in wl_rows
        )
        wl_html = f'<div style="margin-top:1.5rem"><p style="color:#A7B2C4;font-size:.85rem;margin-bottom:.5rem">Watchlist — starred pairs</p>{wl_items}</div>'
    else:
        wl_html = '<p style="color:#67748A;font-size:.85rem;margin-top:1.5rem">No pairs watchlisted yet. Star a pair on the scanner to add it.</p>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Account — FundScan</title>
<style>body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:2rem 1rem}}
.box{{width:100%;max-width:480px;padding:2.5rem;border:1px solid rgba(167,178,196,.13);
  border-radius:8px;background:#0F1B30}}
h1{{font-size:1.2rem;margin-bottom:1.5rem}}
.row{{display:flex;justify-content:space-between;align-items:center;
  padding:.75rem 0;border-bottom:1px solid rgba(167,178,196,.1)}}
.label{{color:#A7B2C4;font-size:.9rem}}
.links{{margin-top:1.5rem;display:flex;gap:1.5rem}}
a.back{{color:#67748A;font-size:.85rem;text-decoration:none}}
a.back:hover{{color:#C9A551}}
a.signout{{color:#67748A;font-size:.85rem;text-decoration:none}}
a.signout:hover{{color:#EEF1F6}}
</style></head>
<body><div class="box">
<h1>Account</h1>
<div class="row"><span class="label">Email</span><span>{user["email"]}</span></div>
<div class="row"><span class="label">Plan</span><span>{plan_html}</span></div>
<div class="row" style="border:none;padding-top:1.25rem">{action_html}</div>
{tg_html}
{wl_html}
<div class="links">
  <a href="/app" class="back">← Scanner</a>
  <a href="/auth/logout" class="signout">Sign out</a>
</div>
</div></body></html>""")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Account — alert threshold update
# ---------------------------------------------------------------------------

@app.post("/watchlist/remove")
async def watchlist_remove(request: Request):
    """Form-based watchlist removal (from account page)."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request", status_code=302)
    form = await request.form()
    symbol = str(form.get("symbol", "")).upper()
    exchange = str(form.get("exchange", "")).lower()
    if symbol and exchange:
        from .db import get_conn as _gc
        with _gc() as conn:
            conn.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND symbol = ? AND exchange = ?",
                (user["id"], symbol, exchange),
            )
    return RedirectResponse("/account", status_code=302)


@app.post("/account/alert-threshold")
def update_alert_threshold(request: Request, min_net_apy: int = 50):
    from fastapi import Form
    from fastapi.responses import RedirectResponse as RR
    user = _current_user(request)
    if not user:
        return RR("/auth/request", status_code=302)
    from .db import get_conn as _gc
    with _gc() as conn:
        conn.execute(
            "UPDATE alert_configs SET min_net_apy = ? WHERE user_id = ? AND telegram_chat_id NOT LIKE 'PENDING:%'",
            (min_net_apy / 100, user["id"])
        )
    return RedirectResponse("/account", status_code=302)


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "bilguun@fundscan.uk")


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request?next=/admin", status_code=302)
    if user["email"] != ADMIN_EMAIL:
        raise HTTPException(403, f"Forbidden — logged in as {user['email']}, expected {ADMIN_EMAIL}")

    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pro_users = conn.execute("SELECT COUNT(*) FROM users WHERE tier = 'pro'").fetchone()[0]
        free_users = total_users - pro_users
        recent_users = conn.execute(
            "SELECT email, tier, created_at FROM users ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        recent_webhooks = conn.execute(
            "SELECT received_at, event_type FROM webhook_events ORDER BY received_at DESC LIMIT 10"
        ).fetchall()

    mrr = pro_users * 20

    def tier_badge(tier):
        if tier == "pro":
            return '<span style="color:#3FBE8E;font-weight:600">Pro</span>'
        return '<span style="color:#67748A">Free</span>'

    user_rows = "".join(
        f'<tr><td>{r["email"]}</td><td>{tier_badge(r["tier"])}</td>'
        f'<td style="color:#67748A;font-size:.85rem">{r["created_at"][:10]}</td></tr>'
        for r in recent_users
    )

    webhook_rows = "".join(
        f'<tr><td style="font-family:monospace;font-size:.85rem">{r["event_type"]}</td>'
        f'<td style="color:#67748A;font-size:.85rem">{r["received_at"][:19].replace("T"," ")}</td></tr>'
        for r in recent_webhooks
    ) or '<tr><td colspan="2" style="color:#67748A">No webhook events yet</td></tr>'

    results = _state["results"]
    last_fetch = _state["last_fetch_at"] or "—"
    fetch_errors = _state["fetch_errors"]

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Admin — FundScan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400&display=swap" rel="stylesheet">
<style>
:root{{--navy:#0A1424;--navy-2:#0F1B30;--navy-3:#16233C;--ivory:#EEF1F6;--soft:#A7B2C4;
  --mist:#67748A;--hairline:rgba(167,178,196,.13);--gold:#C9A551;--green:#3FBE8E;
  --sans:'Instrument Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',monospace}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--navy);color:var(--ivory);font-family:var(--sans);
  min-height:100vh;padding:2rem;-webkit-font-smoothing:antialiased}}
h1{{font-size:1.4rem;font-weight:600;margin-bottom:2rem;color:var(--ivory)}}
h1 span{{color:var(--gold)}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:2rem}}
.stat-card{{background:var(--navy-2);border:1px solid var(--hairline);border-radius:8px;padding:1.25rem}}
.stat-card .n{{font-family:var(--mono);font-size:2rem;font-weight:500;color:var(--ivory)}}
.stat-card .n.gold{{color:var(--gold)}}
.stat-card .n.green{{color:var(--green)}}
.stat-card .l{{color:var(--mist);font-size:.8rem;margin-top:.25rem;letter-spacing:.06em;text-transform:uppercase}}
.section{{background:var(--navy-2);border:1px solid var(--hairline);border-radius:8px;padding:1.5rem;margin-bottom:1.5rem}}
.section h2{{font-size:.85rem;letter-spacing:.1em;text-transform:uppercase;color:var(--gold);margin-bottom:1rem}}
table{{width:100%;border-collapse:collapse}}
td{{padding:.6rem .5rem;border-bottom:1px solid var(--hairline);font-size:.9rem;color:var(--soft)}}
td:first-child{{color:var(--ivory)}}
tr:last-child td{{border-bottom:none}}
.health{{display:flex;gap:2rem;flex-wrap:wrap}}
.hitem{{font-size:.9rem}}.hitem .k{{color:var(--mist);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.2rem}}
.hitem .v{{font-family:var(--mono);color:var(--ivory)}}
.hitem .v.err{{color:#D96A6A}}
a.back{{color:var(--mist);font-size:.85rem;text-decoration:none;display:inline-block;margin-bottom:1.5rem}}
a.back:hover{{color:var(--gold)}}
</style></head>
<body>
<a href="/app" class="back">← Dashboard</a>
<h1>Fund<span>Scan</span> Admin</h1>

<div class="grid">
  <div class="stat-card"><div class="n">{total_users}</div><div class="l">Total users</div></div>
  <div class="stat-card"><div class="n green">{pro_users}</div><div class="l">Pro users</div></div>
  <div class="stat-card"><div class="n">{free_users}</div><div class="l">Free users</div></div>
  <div class="stat-card"><div class="n gold">£{mrr}</div><div class="l">MRR (est.)</div></div>
</div>

<div class="section">
  <h2>System health</h2>
  <div class="health">
    <div class="hitem"><div class="k">Pairs tracked</div><div class="v">{len(results)}</div></div>
    <div class="hitem"><div class="k">Last fetch</div><div class="v">{str(last_fetch)[:19].replace("T"," ")}</div></div>
    <div class="hitem"><div class="k">Fetch errors</div><div class="v {'err' if fetch_errors > 0 else ''}">{fetch_errors}</div></div>
  </div>
</div>

<div class="section">
  <h2>Recent signups</h2>
  <table><tbody>{user_rows}</tbody></table>
</div>

<div class="section">
  <h2>Recent webhook events</h2>
  <table><tbody>{webhook_rows}</tbody></table>
</div>

</body></html>""")


# ---------------------------------------------------------------------------
# Telegram bot webhook (long-poll via background task)
# ---------------------------------------------------------------------------

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receives updates from Telegram (set via setWebhook or use the bot poller)."""
    data = await request.json()
    msg = data.get("message", {})
    text = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if text.startswith("/connect ") and chat_id:
        code = text.split(" ", 1)[1].strip()
        ok = link_telegram(code, chat_id)
        reply = "✅ Telegram connected! You'll get alerts when funding rates cross your threshold." if ok else "❌ Invalid code. Go to fundscan.uk/account to get a fresh code."
        import httpx as _httpx
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        _httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": reply}, timeout=5)
    return {"ok": True}


# ---------------------------------------------------------------------------
# HTMX partials — tier-gated
# ---------------------------------------------------------------------------

@app.get("/htmx/stats", response_class=HTMLResponse)
def htmx_stats():
    results = _state["results"]
    profitable = [r for r in results if r["is_profitable"]]
    best = max((r["net_apy"] for r in profitable), default=None)
    avg = (sum(r["net_apy"] for r in profitable) / len(profitable)) if profitable else None
    exchanges = len({r["exchange"] for r in results})
    # Biggest mover: highest APY among profitable
    mover = max(profitable, key=lambda r: r["net_apy"], default=None)
    mover_sym = mover["symbol"].replace("USDT","").replace("-PERP","") if mover else "—"

    def card(n, label, pos=False):
        cls = 'n pos' if pos else 'n'
        return (f'<div class="stat-card"><div class="{cls}">{n}</div>'
                f'<div class="l">{label}</div></div>')

    cards = (
        card(f'+{best*100:.2f}%' if best else '—', 'Best net APY', pos=bool(best and best > 0))
        + card(str(len(profitable)), 'Pairs above fees')
        + card(f'+{avg*100:.2f}%' if avg else '—', 'Avg net APY', pos=bool(avg and avg > 0))
        + card(mover_sym, 'Top instrument')
    )
    return (
        f'<div id="stats-section" hx-get="/htmx/stats" hx-trigger="every 30s" hx-swap="outerHTML">'
        f'<div class="stats-row">{cards}</div></div>'
    )


@app.get("/htmx/rows", response_class=HTMLResponse)
def htmx_rows(request: Request, size: int = sizing.DEFAULT_POSITION_SIZE):
    """
    Table body partial. `size` is the trader's position size (GBP) — the
    visible slice is re-ranked by net yield at that size so illiquid
    outliers sink instead of dominating the board. Tier gating (which
    pairs a free user can see at all) is unaffected by position size.
    """
    if size not in sizing.POSITION_SIZES:
        size = sizing.DEFAULT_POSITION_SIZE
    user = _current_user(request)
    visible, locked = _tier_results(user)
    visible = sizing.rank_by_size(visible, size)
    return _render_table_rows(visible, locked)


@app.get("/htmx/pairs", response_class=HTMLResponse)
def htmx_pairs(request: Request, size: int = sizing.DEFAULT_POSITION_SIZE):
    """
    Cross-exchange funding spread board: pairs the same asset's perp across
    two exchanges (short the richer venue, long the cheaper one) and ranks
    by net yield of that spread at the given position size. Built only from
    pairs a user's tier can already see on the main board.
    """
    if size not in sizing.POSITION_SIZES:
        size = sizing.DEFAULT_POSITION_SIZE
    user = _current_user(request)
    visible, _locked = _tier_results(user)
    pairs = pairing.rank_pairs_by_size(visible, size)
    return _render_pair_rows(pairs)


@app.get("/htmx/summary", response_class=HTMLResponse)
def htmx_summary(request: Request):
    user = _current_user(request)
    results, _ = _tier_results(user)
    content = _render_summary(results)
    return (
        f'<div class="summary-bar" id="summary" '
        f'hx-get="/htmx/summary" hx-trigger="every 30s" hx-swap="outerHTML">'
        f'{content}</div>'
    )


@app.get("/htmx/status", response_class=HTMLResponse)
def htmx_status():
    last = _state["last_fetch_at"]
    if last:
        dt = datetime.fromisoformat(last)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        label = f"LIVE · {secs}s ago"
    else:
        label = "FETCHING"
    return (
        f'<span class="live-badge" id="status-bar" '
        f'hx-get="/htmx/status" hx-trigger="every 30s" hx-swap="outerHTML">'
        f'{label}</span>'
    )
