"""
Telegram alert system for FundScan users.

Agents:
  1. Threshold Alert  — rate crosses user-set APY threshold (1/hr cooldown)
  2. Anomaly Agent    — rate spikes 2x vs previous fetch (1/hr cooldown)
  3. Daily Digest     — top 5 opportunities sent every morning at 8am UTC

Flow:
  1. User visits /account and sees a 6-digit connection code
  2. User messages the bot: /connect <code>
  3. Bot stores their telegram_chat_id → alert_config
  4. Agents fire after each fetch cycle / on schedule

CONFIG REQUIRED:
  TELEGRAM_BOT_TOKEN  — from @BotFather
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from .db import get_conn

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_COOLDOWN_HOURS = 1
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Track when daily digest was last sent (in-process, resets on restart — fine for MVP)
_last_digest_date: str = ""


# ---------------------------------------------------------------------------
# Connection code (shown on /account)
# ---------------------------------------------------------------------------

def generate_connect_code(user_id: int) -> str:
    """Generate a 6-digit code stored against the user for bot linking."""
    code = secrets.randbelow(900000) + 100000  # 100000–999999
    code_str = str(code)
    with get_conn() as conn:
        # Store in alert_configs with no threshold yet (acts as pending link)
        conn.execute(
            """
            INSERT OR REPLACE INTO alert_configs
                (user_id, symbol, min_net_apy, telegram_chat_id)
            VALUES (?, NULL, 999, ?)
            """,
            (user_id, f"PENDING:{code_str}"),
        )
    return code_str


def link_telegram(code: str, chat_id: str) -> bool:
    """Called when bot receives /connect <code>. Returns True if code found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id FROM alert_configs WHERE telegram_chat_id = ?",
            (f"PENDING:{code}",),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE alert_configs SET telegram_chat_id = ?, min_net_apy = 0.15 WHERE id = ?",
            (chat_id, row["id"]),
        )
    return True


# ---------------------------------------------------------------------------
# Alert sending
# ---------------------------------------------------------------------------

def _send_telegram(chat_id: str, text: str) -> None:
    try:
        httpx.post(
            f"{TG_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning("Telegram send failed to %s: %s", chat_id, e)


def _already_alerted(user_id: int, symbol: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ALERT_COOLDOWN_HOURS)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM alert_log
            WHERE user_id = ? AND symbol = ? AND alerted_at > ?
            """,
            (user_id, symbol, cutoff),
        ).fetchone()
    return row is not None


def _record_alert(user_id: int, symbol: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alert_log (user_id, symbol, alerted_at) VALUES (?, ?, ?)",
            (user_id, symbol, datetime.now(timezone.utc).isoformat()),
        )


def _get_connected_users() -> list:
    """All users with a connected Telegram (any tier)."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT ac.*, u.tier, u.email
            FROM alert_configs ac
            JOIN users u ON u.id = ac.user_id
            WHERE ac.telegram_chat_id IS NOT NULL
              AND ac.telegram_chat_id NOT LIKE 'PENDING:%'
            """
        ).fetchall()


def check_and_send_alerts(scan_results: list[dict]) -> None:
    """
    Agent 1 — Threshold Alert.
    Called after each fetch cycle. Fires when net_apy crosses user threshold.
    Rate limited to 1 alert per pair per hour.
    """
    if not TELEGRAM_TOKEN:
        return

    configs = _get_connected_users()
    for cfg in configs:
        for row in scan_results:
            if cfg["symbol"] and cfg["symbol"] != row["symbol"]:
                continue
            if row["net_apy"] < cfg["min_net_apy"]:
                continue
            if _already_alerted(cfg["user_id"], row["symbol"]):
                continue

            msg = (
                f"🚨 *FundScan Alert*\n"
                f"*{row['symbol']}* on {row['exchange'].title()}\n"
                f"Net APY: *{row['net_apy']*100:.2f}%*\n"
                f"Rate/8h: {row['rate_8h']*100:.4f}%\n"
                f"Breakeven: {row['breakeven_cycles']} cycles"
            )
            _send_telegram(cfg["telegram_chat_id"], msg)
            _record_alert(cfg["user_id"], row["symbol"])
            log.info("Threshold alert: user=%s symbol=%s net_apy=%.2f%%",
                     cfg["user_id"], row["symbol"], row["net_apy"] * 100)


def check_anomalies(scan_results: list[dict]) -> None:
    """
    Agent 2 — Anomaly Agent.
    Fires when a symbol's rate has doubled (2x) vs the previous snapshot.
    Rate limited to 1 alert per pair per hour.
    """
    if not TELEGRAM_TOKEN:
        return

    configs = _get_connected_users()
    if not configs:
        return

    # Build lookup of current rates
    current = {(r["exchange"], r["symbol"]): r for r in scan_results}

    # Get previous snapshots (second-most-recent per exchange/symbol)
    with get_conn() as conn:
        prev_rows = conn.execute(
            """
            SELECT s.exchange, s.symbol, s.rate_8h
            FROM funding_snapshots s
            INNER JOIN (
                SELECT exchange, symbol, MAX(ts) AS max_ts
                FROM funding_snapshots
                WHERE ts < (SELECT MAX(ts) FROM funding_snapshots)
                GROUP BY exchange, symbol
            ) prev ON s.exchange = prev.exchange
                   AND s.symbol  = prev.symbol
                   AND s.ts      = prev.max_ts
            """
        ).fetchall()

    for prev in prev_rows:
        key = (prev["exchange"], prev["symbol"])
        cur = current.get(key)
        if not cur or prev["rate_8h"] <= 0:
            continue
        ratio = cur["rate_8h"] / prev["rate_8h"]
        if ratio < 2.0:
            continue

        for cfg in configs:
            if _already_alerted(cfg["user_id"], f"ANOMALY:{prev['symbol']}"):
                continue
            msg = (
                f"⚡ *Anomaly Detected*\n"
                f"*{prev['symbol']}* on {prev['exchange'].title()} spiked "
                f"*{ratio:.1f}x* in one cycle\n"
                f"Previous rate: {prev['rate_8h']*100:.4f}%\n"
                f"Current rate: {cur['rate_8h']*100:.4f}%\n"
                f"Net APY now: *{cur['net_apy']*100:.2f}%*"
            )
            _send_telegram(cfg["telegram_chat_id"], msg)
            _record_alert(cfg["user_id"], f"ANOMALY:{prev['symbol']}")
            log.info("Anomaly alert: symbol=%s ratio=%.1fx", prev["symbol"], ratio)


def send_daily_digest(scan_results: list[dict]) -> None:
    """
    Agent 3 — Daily Digest.
    Sends top 5 opportunities to all connected users once per day at 8am UTC.
    """
    global _last_digest_date

    if not TELEGRAM_TOKEN:
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour != 8 or _last_digest_date == today:
        return

    configs = _get_connected_users()
    if not configs:
        return

    top5 = [r for r in scan_results if r["is_profitable"]][:5]
    if not top5:
        return

    lines = [f"📊 *FundScan Daily Digest — {today}*\n"]
    for i, r in enumerate(top5, 1):
        lines.append(
            f"{i}. *{r['symbol']}* ({r['exchange'].title()}) — "
            f"{r['net_apy']*100:.1f}% APY"
        )
    lines.append("\nfundscan.uk")
    msg = "\n".join(lines)

    chat_ids_sent = set()
    for cfg in configs:
        if cfg["telegram_chat_id"] in chat_ids_sent:
            continue
        _send_telegram(cfg["telegram_chat_id"], msg)
        chat_ids_sent.add(cfg["telegram_chat_id"])

    _last_digest_date = today
    log.info("Daily digest sent to %d users", len(chat_ids_sent))


# ---------------------------------------------------------------------------
# Owner notifications (signup, churn, revenue)
# ---------------------------------------------------------------------------

def _owner_chat_id() -> str | None:
    """Return the Telegram chat ID for the account owner (boursierbilguun207@gmail.com)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT ac.telegram_chat_id
            FROM alert_configs ac
            JOIN users u ON u.id = ac.user_id
            WHERE u.email = 'boursierbilguun207@gmail.com'
              AND ac.telegram_chat_id IS NOT NULL
              AND ac.telegram_chat_id NOT LIKE 'PENDING:%'
            LIMIT 1
            """
        ).fetchone()
    return row["telegram_chat_id"] if row else None


def notify_owner(msg: str) -> None:
    """Send a message to the owner's Telegram."""
    chat_id = _owner_chat_id()
    if chat_id:
        _send_telegram(chat_id, msg)


def notify_new_signup(email: str) -> None:
    """Agent 4 — New Signup. Fires when someone upgrades to Pro."""
    notify_owner(f"💰 *New Pro subscriber!*\n{email}")
    log.info("New signup notification sent for %s", email)


def notify_churn(email: str) -> None:
    """Agent 5 — Churn Agent. Fires when someone cancels Pro."""
    notify_owner(f"😬 *Cancellation*\n{email} downgraded to free.")
    log.info("Churn notification sent for %s", email)


# ---------------------------------------------------------------------------
# Multi-exchange comparison alert
# ---------------------------------------------------------------------------

def check_multi_exchange(scan_results: list[dict]) -> None:
    """
    Agent: fires when the same symbol has meaningfully different rates on
    Bybit vs Binance (arbitrage signal). Sends to all connected users.
    """
    if not TELEGRAM_TOKEN:
        return

    configs = _get_connected_users()
    if not configs:
        return

    # Group by symbol
    by_symbol: dict[str, list[dict]] = {}
    for r in scan_results:
        by_symbol.setdefault(r["symbol"], []).append(r)

    for symbol, rows in by_symbol.items():
        if len(rows) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda x: x["net_apy"], reverse=True)
        best = rows_sorted[0]
        worst = rows_sorted[-1]
        diff = best["net_apy"] - worst["net_apy"]
        if diff < 0.10:  # Only alert if >10% APY spread between exchanges
            continue
        for cfg in configs:
            if _already_alerted(cfg["user_id"], f"ARBI:{symbol}"):
                continue
            msg = (
                f"🔄 *Cross-Exchange Spread*\n"
                f"*{symbol}* — {diff*100:.1f}% APY spread\n"
                f"Best: {best['exchange'].title()} @ *{best['net_apy']*100:.1f}%*\n"
                f"Other: {worst['exchange'].title()} @ {worst['net_apy']*100:.1f}%"
            )
            _send_telegram(cfg["telegram_chat_id"], msg)
            _record_alert(cfg["user_id"], f"ARBI:{symbol}")
            log.info("Arb alert: symbol=%s diff=%.1f%%", symbol, diff * 100)


# ---------------------------------------------------------------------------
# Watchlist price drop alert
# ---------------------------------------------------------------------------

def check_watchlist_drops(scan_results: list[dict]) -> None:
    """
    Agent: alerts users when a pair on their watchlist drops significantly
    (rate falls >50% vs previous fetch — opportunity may be ending).
    """
    if not TELEGRAM_TOKEN:
        return

    with get_conn() as conn:
        watchlist_rows = conn.execute(
            """
            SELECT w.user_id, w.symbol, w.exchange, ac.telegram_chat_id
            FROM watchlist w
            JOIN alert_configs ac ON ac.user_id = w.user_id
            WHERE ac.telegram_chat_id IS NOT NULL
              AND ac.telegram_chat_id NOT LIKE 'PENDING:%'
            """
        ).fetchall()

    if not watchlist_rows:
        return

    current = {(r["exchange"], r["symbol"]): r for r in scan_results}

    with get_conn() as conn:
        prev_rows = conn.execute(
            """
            SELECT s.exchange, s.symbol, s.rate_8h
            FROM funding_snapshots s
            INNER JOIN (
                SELECT exchange, symbol, MAX(ts) AS max_ts
                FROM funding_snapshots
                WHERE ts < (SELECT MAX(ts) FROM funding_snapshots)
                GROUP BY exchange, symbol
            ) prev ON s.exchange = prev.exchange
                   AND s.symbol  = prev.symbol
                   AND s.ts      = prev.max_ts
            """
        ).fetchall()

    prev = {(r["exchange"], r["symbol"]): r["rate_8h"] for r in prev_rows}

    for w in watchlist_rows:
        key = (w["exchange"], w["symbol"])
        cur = current.get(key)
        old_rate = prev.get(key)
        if not cur or not old_rate or old_rate <= 0:
            continue
        drop_ratio = cur["rate_8h"] / old_rate
        if drop_ratio > 0.5:  # Less than 50% drop — not significant
            continue
        alert_key = f"DROP:{w['symbol']}:{w['exchange']}"
        if _already_alerted(w["user_id"], alert_key):
            continue
        msg = (
            f"📉 *Watchlist Drop*\n"
            f"*{w['symbol']}* on {w['exchange'].title()} dropped "
            f"*{(1-drop_ratio)*100:.0f}%*\n"
            f"New APY: {cur['net_apy']*100:.1f}%"
        )
        _send_telegram(w["telegram_chat_id"], msg)
        _record_alert(w["user_id"], alert_key)
        log.info("Watchlist drop alert: user=%s symbol=%s", w["user_id"], w["symbol"])
