"""
FundScan FastAPI application.
Run: uvicorn fundscan.app:app --reload
"""
from dotenv import load_dotenv
load_dotenv(dotenv_path="/Users/bilguunboursier/Documents/Claude/Projects/fundscan/.env")
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import math as fm
from .db import init_db, insert_snapshots, query_delayed, query_history, query_latest
from .scanner import scan
from .alerts import (check_and_send_alerts, check_anomalies, send_daily_digest,
                     check_multi_exchange, check_watchlist_drops,
                     generate_connect_code, link_telegram)
from .onboarding import run_onboarding, run_weekly_report
from .maintenance import run_db_backup, run_seo_check
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
    results, missing = _tier_results(user)
    tier = user["tier"] if user else "anonymous"
    return {
        "tier": tier,
        "fetched_at": _state["last_fetch_at"],
        "count": len(results),
        "missing": missing,
        "opportunities": results,
    }


@app.get("/rates/{symbol}")
def rate_detail(request: Request, symbol: str):
    user = _current_user(request)
    results, _ = _tier_results(user)
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


FREE_TIER_LIMIT = 5
FREE_TIER_DELAY = 10  # minutes


def _tier_results(user: Optional[dict]) -> tuple[list[dict], int]:
    """
    Return (results, total_count) gated by tier.

    Pro  → live in-memory results, full list.
    Free → top FREE_TIER_LIMIT pairs from DB, delayed FREE_TIER_DELAY minutes.
    Returns total_count so the UI can show "N pairs hidden".
    """
    if user and user.get("tier") == "pro":
        results = _state["results"]
        return results, 0

    # Free / unauthenticated: delayed DB data, capped at 5
    rows = query_delayed(FREE_TIER_DELAY)
    if not rows:
        # DB not populated yet — serve empty rather than crash
        return [], 0
    full_count = len(set((r["symbol"], r["exchange"]) for r in rows))
    results = [
        {
            "exchange": r["exchange"],
            "symbol": r["symbol"],
            "rate_8h": r["rate_8h"],
            "net_apy": r["net_apy"],
            "gross_apy": fm.annualised_gross(r["rate_8h"]),
            "breakeven_cycles": round(be, 1) if (be := fm.breakeven_cycles(r["rate_8h"])) else None,
            "is_profitable": fm.is_profitable(r["rate_8h"]),
            "fetched_at": r["ts"],
            "funding_interval_hours": 8,
            "next_funding_time": None,
        }
        for r in rows
    ][:FREE_TIER_LIMIT]
    missing = max(0, full_count - FREE_TIER_LIMIT)
    return results, missing


# ---------------------------------------------------------------------------
# HTMX dashboard
# ---------------------------------------------------------------------------

def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _render_table_rows(results: list[dict]) -> str:
    if not results:
        return '<tr><td colspan="5" style="text-align:center;color:var(--muted)">Fetching data…</td></tr>'

    rows = []
    for r in results:
        profitable = r["is_profitable"]
        row_class = "" if profitable else " class='greyed'"
        net_label = _pct(r["net_apy"]) if profitable else f"{_pct(r['net_apy'])} — below fees"
        net_color = "#22c55e" if profitable else "var(--muted)"
        be = r["breakeven_cycles"]
        be_str = f"{be:.1f}" if be is not None else "∞"
        rows.append(
            f'<tr{row_class} data-symbol="{r["symbol"]}" data-exchange="{r["exchange"]}" onclick="toggleChart(this)">'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["exchange"].title()}</td>'
            f'<td>{_pct(r["rate_8h"])}</td>'
            f'<td style="color:{net_color};font-weight:600">{net_label}</td>'
            f'<td>{be_str}</td>'
            f'</tr>'
            f'<tr class="chart-row" id="chart-{r["symbol"]}-{r["exchange"]}" style="display:none">'
            f'<td colspan="5"><canvas id="canvas-{r["symbol"]}-{r["exchange"]}" height="80"></canvas></td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _render_summary(results: list[dict]) -> str:
    profitable = [r for r in results if r["is_profitable"]]
    best = max((r["net_apy"] for r in profitable), default=None)
    best_str = _pct(best) if best is not None else "—"
    return (
        f'<span class="stat-value">{best_str}</span><span class="stat-label"> best net APY</span>'
        f'&nbsp;&nbsp;&nbsp;'
        f'<span class="stat-value">{len(profitable)}</span><span class="stat-label"> pairs above fees</span>'
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
    results, missing = _tier_results(user)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": user, "missing_count": missing},
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

    smtp_host = os.getenv("SMTP_HOST", "")
    if smtp_host:
        try:
            send_magic_link(email, token)
        except Exception as e:
            log.error("Failed to send magic link to %s: %s", email, e)
            # Don't leak whether send failed — show same page
    else:
        # Dev mode: log the link so you can click it without SMTP
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
    """
    Redirect to Stripe Checkout for the Pro subscription.
    Requires STRIPE_SECRET_KEY and STRIPE_PRICE_ID in .env.
    """
    from .billing import checkout_url
    user = _current_user(request)
    if not user:
        return RedirectResponse("/auth/request", status_code=302)

    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_price = os.getenv("STRIPE_PRICE_ID", "")
    if stripe_key and stripe_price:
        try:
            url = checkout_url(user["email"])
            return RedirectResponse(url, status_code=302)
        except Exception as e:
            log.error("Stripe checkout failed: %s", e)

    return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Upgrade to Pro — FundScan</title>
<style>body{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{max-width:420px;padding:2.5rem;border:1px solid rgba(201,165,81,.4);border-radius:8px;background:#0F1B30}
p{color:#A7B2C4;margin:.75rem 0;font-size:.95rem}code{color:#C9A551}
a{color:#C9A551;text-decoration:none}</style></head>
<body><div class="box">
<h1 style="font-size:1.2rem;margin-bottom:.5rem">Upgrade to Pro</h1>
<p>Stripe billing will be live once credentials are configured.</p>
<p>Set <code>STRIPE_SECRET_KEY</code> and <code>STRIPE_PRICE_ID</code> in your <code>.env</code> file.</p>
<p style="margin-top:1.5rem"><a href="/">← Back to home</a></p>
</div></body></html>""")


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
    if tier == "pro":
        plan_html = '<span style="color:#22c55e;font-weight:600">Pro</span>'
        action_html = '<a href="https://billing.stripe.com/p/login/test_placeholder" style="color:#C9A551">Manage subscription →</a>'
    else:
        plan_html = "Free"
        action_html = '<a href="/billing/checkout" style="background:#C9A551;color:#221A08;padding:.6rem 1.2rem;border-radius:4px;text-decoration:none;font-weight:600">Upgrade to Pro — £20/mo</a>'

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

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Account — FundScan</title>
<style>body{{background:#0A1424;color:#EEF1F6;font-family:system-ui,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
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
<div class="links">
  <a href="/app" class="back">← Scanner</a>
  <a href="/auth/logout" class="signout">Sign out</a>
</div>
</div></body></html>""")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Account — alert threshold update
# ---------------------------------------------------------------------------

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

@app.get("/htmx/rows", response_class=HTMLResponse)
def htmx_rows(request: Request):
    user = _current_user(request)
    results, _ = _tier_results(user)
    return _render_table_rows(results)


@app.get("/htmx/summary", response_class=HTMLResponse)
def htmx_summary(request: Request):
    user = _current_user(request)
    results, _ = _tier_results(user)
    content = _render_summary(results)
    return f'<div class="summary" id="summary" hx-get="/htmx/summary" hx-trigger="every 30s" hx-swap="outerHTML">{content}</div>'


@app.get("/htmx/status", response_class=HTMLResponse)
def htmx_status():
    last = _state["last_fetch_at"]
    if last:
        dt = datetime.fromisoformat(last)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
        label = f"updated {secs}s ago"
    else:
        label = "fetching…"
    return (
        f'<div class="status" id="status-bar" '
        f'hx-get="/htmx/status" hx-trigger="every 30s" hx-swap="outerHTML">'
        f'{label}</div>'
    )
