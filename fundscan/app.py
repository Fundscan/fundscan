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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import math as fm
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


@app.get("/rates")
def rates(request: Request):
    """
    Opportunities ranked by net APY descending.
    Pro: full live list. Free/anonymous: top 5, delayed 10 min.
    net_apy and gross_apy are decimals (0.15 = 15%).
    """
    user = _current_user(request)
    results, locked = _tier_results(user)
    tier = user["tier"] if user else "anonymous"
    return {
        "tier": tier,
        "fetched_at": _state["last_fetch_at"],
        "count": len(results),
        "missing": len(locked),
        "opportunities": results,
    }


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
    symbol = symbol.upper()
    rows = query_history(symbol, days)
    return {
        "symbol": symbol,
        "days": days,
        "points": [
            {"ts": r["ts"], "exchange": r["exchange"],
             "rate_8h": r["rate_8h"], "net_apy": r["net_apy"]}
            for r in rows
        ],
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
        profitable = r["is_profitable"]
        row_cls = "data-row" if profitable else "data-row greyed"
        net_cls = "num pos" if profitable else "num neg"
        net_label = _pct(r["net_apy"])
        be = r["breakeven_cycles"]
        be_str = f"{be:.1f}" if be is not None else "∞"
        spark_key = f"{r['exchange']}:{r['symbol']}"
        safe_id = f"{r['exchange']}-{r['symbol']}".replace("/", "_")
        exch = r["exchange"].upper()
        rows.append(
            f'<tr class="{row_cls}" data-symbol="{r["symbol"]}" data-exchange="{r["exchange"]}"'
            f' data-apy="{r["net_apy"]}" onclick="toggleChart(this)">'
            f'<td style="padding:.7rem .5rem .7rem .75rem" onclick="event.stopPropagation()">'
            f'<button class="star-btn" id="star-{safe_id}" '
            f'data-symbol="{r["symbol"]}" data-exchange="{r["exchange"]}" '
            f'onclick="toggleWatchlist(this)" title="Watch">☆</button>'
            f'</td>'
            f'<td><span class="sym-name">{r["symbol"]}</span></td>'
            f'<td><span class="exch-badge exch-{r["exchange"]}">{exch}</span></td>'
            f'<td><span class="num">{_pct(r["rate_8h"])}</span></td>'
            f'<td><span class="{net_cls}" style="font-weight:600">{net_label}</span></td>'
            f'<td><span class="brkeven">{be_str} cycles</span></td>'
            f'<td class="spark-cell"><canvas id="spark-{safe_id}" data-spark-key="{spark_key}" width="80" height="28"></canvas></td>'
            f'</tr>'
            f'<tr class="chart-row" id="chart-{r["symbol"]}-{r["exchange"]}" style="display:none">'
            f'<td colspan="8">'
            f'<div class="chart-inner"><canvas id="canvas-{r["symbol"]}-{r["exchange"]}" height="90"></canvas></div>'
            f'</td>'
            f'</tr>'
        )

    # Blurred Pro-only rows
    if locked:
        for r in locked:
            exch = r["exchange"].upper()
            net_label = _pct(r["net_apy"])
            be = r["breakeven_cycles"]
            be_str = f"{be:.1f}" if be is not None else "∞"
            rows.append(
                f'<tr class="data-row locked-row" onclick="document.location=\'/billing/checkout\'">'
                f'<td style="padding:.7rem .5rem .7rem .75rem"><span style="color:var(--mist)">☆</span></td>'
                f'<td><span class="sym-name locked-blur">{r["symbol"]}</span></td>'
                f'<td><span class="exch-badge exch-{r["exchange"]} locked-blur">{exch}</span></td>'
                f'<td><span class="num locked-blur">{_pct(r["rate_8h"])}</span></td>'
                f'<td><span class="num pos locked-blur" style="font-weight:600">{net_label}</span></td>'
                f'<td><span class="brkeven locked-blur">{be_str} cycles</span></td>'
                f'<td></td>'
                f'</tr>'
            )
        rows.append(
            f'<tr><td colspan="7" style="padding:.75rem 1rem 1.25rem;text-align:center">'
            f'<a href="/billing/checkout" class="pro-unlock-btn">'
            f'Unlock {len(locked)} more pairs — Upgrade to Pro</a>'
            f'</td></tr>'
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
        {"user": user, "locked_count": len(locked), "exchanges": exchanges},
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
    """Redirect logged-in users to Stripe checkout. Guests go to sign-in first."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request", status_code=302)
    if user.get("tier") == "pro":
        return RedirectResponse("/account", status_code=302)

    import traceback
    from .billing import checkout_url
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    price_id = os.getenv("STRIPE_PRICE_ID", "")

    _diag = (
        f"STRIPE_SECRET_KEY={'SET('+stripe_key[:8]+'...)' if stripe_key else 'MISSING'} | "
        f"STRIPE_PRICE_ID={'SET('+price_id+')' if price_id else 'MISSING'}"
    )
    log.info("billing_checkout diagnostics: %s", _diag)

    if not stripe_key or not price_id:
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Config Error — FundScan</title>
<style>body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{max-width:520px;padding:2.5rem;border:1px solid rgba(201,165,81,.4);border-radius:8px;background:#0F1B30}}
p{{color:#A7B2C4;margin:.75rem 0;font-size:.95rem}}a{{color:#C9A551;text-decoration:none}}
code{{font-size:.8rem;color:#C9A551}}</style></head>
<body><div class="box">
<h1 style="font-size:1.2rem;margin-bottom:.5rem">Stripe not configured</h1>
<p>Missing env vars. Check Railway Variables.</p>
<code>{_diag}</code>
<p style="margin-top:1.5rem"><a href="/">← Back to home</a></p>
</div></body></html>""")

    try:
        url = checkout_url(user["email"])
        return RedirectResponse(url, status_code=302)
    except Exception as e:
        tb = traceback.format_exc()
        log.error("Stripe checkout failed for %s:\n%s", user["email"], tb)
        err_html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Checkout Error — FundScan</title>
<style>body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;margin:2rem}}
pre{{background:#0F1B30;padding:1.5rem;border-radius:6px;font-size:.8rem;overflow:auto;color:#f87171}}
a{{color:#C9A551}}</style></head>
<body>
<h2>Stripe checkout error</h2>
<pre>{type(e).__name__}: {e}</pre>
<pre>{tb}</pre>
<p><a href="/">← Home</a></p>
</body></html>"""
        return HTMLResponse(err_html, status_code=500)


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
        action_html = '<span style="color:#A7B2C4;font-size:.9rem">Pro subscriptions opening soon</span>'

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
def htmx_rows(request: Request):
    user = _current_user(request)
    visible, locked = _tier_results(user)
    return _render_table_rows(visible, locked)


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
