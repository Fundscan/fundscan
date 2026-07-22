"""
Magic-link authentication + session management.

Flow:
  1. POST /auth/request  {email} → generate token, email a link
  2. GET  /auth/verify?token=…   → validate, set signed cookie, redirect to /
  3. GET  /auth/logout            → clear cookie

Sessions: signed cookies using itsdangerous (same library FastAPI uses internally).
No passwords. Free vs pro tier controlled by users.tier column.

CONFIG REQUIRED (set in .env):
  SECRET_KEY      — random 32-byte hex string, e.g. `openssl rand -hex 32`
  RESEND_API_KEY  — from https://resend.com (re_...)
  FROM_EMAIL      — verified sender address in Resend
  BASE_URL        — e.g. https://fundscan.uk (used for magic link URL)
"""
import logging
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Optional

import httpx

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .db import get_conn

log = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
TOKEN_TTL_MINUTES = 15
SESSION_COOKIE = "fs_session"

_signer = URLSafeTimedSerializer(SECRET_KEY, salt="session")
_token_signer = URLSafeTimedSerializer(SECRET_KEY, salt="magic-link")


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

# Emails that always get Pro regardless of billing status (comps). Enforced
# on every load, not just at signup, so it self-heals even if the row
# already existed as free or a webhook ever touched it.
COMP_PRO_EMAILS = {"klaiduskazlauskas01@gmail.com"}


def _apply_comp_tier(user: dict) -> dict:
    if user["email"] in COMP_PRO_EMAILS and user["tier"] != "pro":
        set_user_tier(user["email"], "pro")
        user["tier"] = "pro"
    return user


def get_or_create_user(email: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO users (email, tier, created_at) VALUES (?, 'free', ?)",
                (email, now),
            )
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        user = dict(row)
    # Applied after the connection above closes -- set_user_tier() opens its
    # own connection, and SQLite locks on a second writer while the first is open.
    return _apply_comp_tier(user)


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        user = dict(row)
    return _apply_comp_tier(user)


def set_user_tier(email: str, tier: str) -> None:
    assert tier in ("free", "pro")
    with get_conn() as conn:
        conn.execute("UPDATE users SET tier = ? WHERE email = ?", (tier, email))


# ---------------------------------------------------------------------------
# Magic link
# ---------------------------------------------------------------------------

def create_magic_token(email: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used) VALUES (?, ?, ?, 0)",
            (token, email, expires_at),
        )
    return token


def consume_magic_token(token: str) -> Optional[str]:
    """Return email if token valid + unused, else None. Marks token as used."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM magic_tokens WHERE token = ? AND used = 0",
            (token,),
        ).fetchone()
        if not row:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return None
        conn.execute("UPDATE magic_tokens SET used = 1 WHERE token = ?", (token,))
        return row["email"]


def _send_via_resend(to: str, subject: str, text: str) -> None:
    """Send email via Resend HTTP API. Raises on failure."""
    api_key = os.getenv("RESEND_API_KEY", "")
    from_email = os.getenv("FROM_EMAIL", "noreuter@fundscan.uk")
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_email, "to": [to], "subject": subject, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def _send_via_smtp(to: str, subject: str, text: str) -> None:
    """Send email via SMTP. Raises on failure."""
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    from_email = os.getenv("FROM_EMAIL", user)
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to
    with smtplib.SMTP(host, port, timeout=15) as conn:
        conn.ehlo()
        conn.starttls()
        conn.login(user, password)
        conn.sendmail(from_email, [to], msg.as_string())


def _dispatch_email(to: str, subject: str, text: str) -> None:
    """
    Try Resend first (if RESEND_API_KEY set), fall back to SMTP
    (if SMTP_HOST set). Logs a warning and skips if neither is configured.
    """
    if os.getenv("RESEND_API_KEY"):
        _send_via_resend(to, subject, text)
    elif os.getenv("SMTP_HOST"):
        _send_via_smtp(to, subject, text)
    else:
        log.warning("DEV — no email provider configured, skipping email to %s", to)


def send_magic_link(email: str, token: str) -> None:
    link = f"{BASE_URL}/auth/verify?token={token}"
    try:
        _dispatch_email(
            to=email,
            subject="Your FundScan login link",
            text=(
                f"Click to log in to FundScan (expires in {TOKEN_TTL_MINUTES} minutes):\n\n"
                f"{link}\n\nIf you didn't request this, ignore this email."
            ),
        )
        log.info("Magic link sent to %s", email)
    except Exception as e:
        log.error("Failed to send magic link to %s: %s", email, e)
        raise


def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email. Used for alerts, onboarding, billing notices."""
    try:
        _dispatch_email(to=to, subject=subject, text=body)
        log.info("Email sent to %s: %s", to, subject)
    except Exception as e:
        log.error("Failed to send email to %s: %s", to, e)


# ---------------------------------------------------------------------------
# Session cookie (signed, not encrypted — don't put secrets in it)
# ---------------------------------------------------------------------------

def make_session_cookie(user_id: int) -> str:
    return _signer.dumps({"uid": user_id})


def decode_session_cookie(cookie: str) -> Optional[int]:
    """Return user_id or None if invalid/expired."""
    try:
        data = _signer.loads(cookie, max_age=60 * 60 * 24 * 30)  # 30-day sessions
        return data["uid"]
    except (BadSignature, SignatureExpired, KeyError):
        return None
