"""
Agent 6 — Onboarding email sequence.
Agent 7 — Feedback Collector.
Agent 10 — Weekly Report (best opportunity of the week, emailed to all users every Sunday).

Called once per day from the fetch loop. Checks all users and sends
emails at the right intervals since signup.

Sequences:
  Day 1  — Welcome + how to use the dashboard
  Day 3  — Explain Pro tier + what they're missing
  Day 7  — Feedback request ("what do you think?")
"""
import logging
from datetime import datetime, timedelta, timezone

from .auth import send_email
from .db import get_conn

log = logging.getLogger(__name__)

BASE_URL = "https://fundscan.uk"

SEQUENCES = [
    {
        "day": 1,
        "subject": "Welcome to FundScan 👋",
        "body": """Hi,

Thanks for signing up to FundScan!

Here's what you can do right now:

→ Visit the dashboard: {base_url}/app
   You'll see the top 5 funding rate opportunities updated every 60 seconds.

→ What are funding rates?
   Exchanges pay traders to keep perpetual futures balanced. When rates are high,
   you can earn yield by holding spot and shorting perps — a strategy called
   cash-and-carry. FundScan finds the best rates so you don't have to.

→ Click any row to see 7-day rate history for that pair.

Questions? Just reply to this email.

— FundScan
""",
    },
    {
        "day": 3,
        "subject": "You're missing 60+ opportunities on FundScan",
        "body": """Hi,

You've been on FundScan for a few days — hope it's been useful!

Right now on the free tier you can see the top 5 pairs. There are currently
60+ more pairs hidden, many with higher rates.

Pro gives you:
  ✓ All pairs, real-time (no 10-minute delay)
  ✓ Telegram alerts when rates cross your threshold
  ✓ Anomaly alerts when a rate spikes suddenly

Upgrade for £20/month: {base_url}/billing/checkout

— FundScan
""",
    },
    {
        "day": 7,
        "subject": "Quick question about FundScan",
        "body": """Hi,

You've been using FundScan for a week — I'd love to know what you think.

What's one thing that would make FundScan more useful for you?

Just reply to this email — I read every response.

— Bilguun, FundScan
""",
    },
]

# Track which emails have been sent (stored in DB)
_DDL = """
CREATE TABLE IF NOT EXISTS onboarding_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    day        INTEGER NOT NULL,
    sent_at    TEXT    NOT NULL,
    UNIQUE(user_id, day)
);
"""


def _ensure_table():
    with get_conn() as conn:
        conn.executescript(_DDL)


def _already_sent(user_id: int, day: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM onboarding_log WHERE user_id = ? AND day = ?",
            (user_id, day)
        ).fetchone()
    return row is not None


def _mark_sent(user_id: int, day: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_log (user_id, day, sent_at) VALUES (?, ?, ?)",
            (user_id, day, datetime.now(timezone.utc).isoformat())
        )


def run_onboarding() -> None:
    """
    Called once per fetch cycle. Sends onboarding emails to users who reached
    the right day since signup, if not already sent.
    """
    _ensure_table()
    now = datetime.now(timezone.utc)

    with get_conn() as conn:
        users = conn.execute(
            "SELECT id, email, created_at FROM users"
        ).fetchall()

    for user in users:
        try:
            created = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))
        except Exception:
            continue

        days_old = (now - created).days

        for seq in SEQUENCES:
            target_day = seq["day"]
            # Send on the target day or the day after (in case the job missed it)
            if days_old < target_day or days_old > target_day + 1:
                continue
            if _already_sent(user["id"], target_day):
                continue

            body = seq["body"].format(base_url=BASE_URL)
            send_email(user["email"], seq["subject"], body)
            _mark_sent(user["id"], target_day)
            log.info("Onboarding day %d sent to %s", target_day, user["email"])


_last_weekly_report: str = ""


def run_weekly_report(scan_results: list[dict]) -> None:
    """
    Agent 10 — Weekly Report.
    Every Sunday sends all users an email with the top opportunity of the week.
    """
    global _last_weekly_report

    now = datetime.now(timezone.utc)
    week = now.strftime("%Y-W%W")
    if _last_weekly_report == week or now.weekday() != 6:  # Sunday only
        return

    _ensure_table()
    top = [r for r in scan_results if r.get("is_profitable")][:3]
    if not top:
        return

    with get_conn() as conn:
        users = conn.execute("SELECT email FROM users").fetchall()

    lines = [
        f"📈 FundScan Weekly — Best opportunities this week\n",
    ]
    for i, r in enumerate(top, 1):
        lines.append(
            f"{i}. {r['symbol']} on {r['exchange'].title()}\n"
            f"   Net APY: {r['net_apy']*100:.1f}% | Rate/8h: {r['rate_8h']*100:.4f}%\n"
        )
    lines.append(f"\nView all pairs: {BASE_URL}/app")
    body = "\n".join(lines)

    for user in users:
        send_email(user["email"], "FundScan Weekly — Top funding rate opportunities", body)

    _last_weekly_report = week
    log.info("Weekly report sent to %d users", len(users))
