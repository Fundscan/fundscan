"""
Agent 8  — Competitor Monitor: weekly scrape of rival sites, emails you if anything changed.
Agent 9  — Reddit Scout: searches Reddit for people asking about funding rates.
"""
import hashlib
import logging
from datetime import datetime, timezone

import httpx

from .alerts import notify_owner
from .auth import send_email
from .db import get_conn

log = logging.getLogger(__name__)

OWNER_EMAIL = "bilguun@fundscan.uk"

COMPETITORS = [
    {"name": "Coinglass", "url": "https://www.coinglass.com/FundingRate"},
    {"name": "Velo Data", "url": "https://velodata.app"},
    {"name": "Laevitas", "url": "https://laevitas.ch"},
]

REDDIT_KEYWORDS = [
    "funding rate tracker",
    "funding rate scanner",
    "cash and carry crypto",
    "perpetual funding rate tool",
]

_last_competitor_week: str = ""
_last_reddit_week: str = ""


# ---------------------------------------------------------------------------
# Competitor Monitor
# ---------------------------------------------------------------------------

def _fetch_hash(url: str) -> str | None:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 FundScan-Monitor/1.0"})
        return hashlib.sha256(r.text[:5000].encode()).hexdigest()
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def _get_stored_hash(url: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT content_hash FROM competitor_snapshots WHERE url = ? ORDER BY checked_at DESC LIMIT 1",
            (url,)
        ).fetchone()
    return row["content_hash"] if row else None


def _store_hash(url: str, hash_val: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO competitor_snapshots (url, content_hash, checked_at) VALUES (?, ?, ?)",
            (url, hash_val, datetime.now(timezone.utc).isoformat())
        )


def run_competitor_monitor() -> None:
    """Runs weekly on Monday. Checks competitors for changes and emails a report."""
    global _last_competitor_week

    now = datetime.now(timezone.utc)
    week = now.strftime("%Y-W%W")
    if _last_competitor_week == week or now.weekday() != 0:
        return

    changes = []
    unchanged = []

    for c in COMPETITORS:
        new_hash = _fetch_hash(c["url"])
        if not new_hash:
            continue
        old_hash = _get_stored_hash(c["url"])
        _store_hash(c["url"], new_hash)

        if old_hash and old_hash != new_hash:
            changes.append(c["name"])
        else:
            unchanged.append(c["name"])

    lines = [f"🔍 Competitor Monitor — {now.strftime('%d %b %Y')}\n"]
    if changes:
        lines.append("⚠️ CHANGES DETECTED:")
        for name in changes:
            comp = next(c for c in COMPETITORS if c["name"] == name)
            lines.append(f"  • {name}: {comp['url']}")
    else:
        lines.append("✅ No changes detected this week.")

    lines.append(f"\nChecked: {', '.join(unchanged + changes)}")
    body = "\n".join(lines)

    send_email(OWNER_EMAIL, f"Competitor Monitor — {now.strftime('%d %b %Y')}", body)
    _last_competitor_week = week
    log.info("Competitor report sent. Changes: %s", changes)


# ---------------------------------------------------------------------------
# Reddit Scout
# ---------------------------------------------------------------------------

def run_reddit_scout() -> None:
    """Runs weekly. Finds Reddit posts about funding rates and emails you."""
    global _last_reddit_week

    now = datetime.now(timezone.utc)
    week = now.strftime("%Y-W%W")
    if _last_reddit_week == week or now.weekday() != 0:
        return

    found_posts = []
    seen_urls = set()

    for keyword in REDDIT_KEYWORDS:
        try:
            r = httpx.get(
                "https://www.reddit.com/search.json",
                params={"q": keyword, "sort": "new", "t": "week", "limit": 5},
                headers={"User-Agent": "FundScan-Scout/1.0"},
                timeout=10,
            )
            posts = r.json().get("data", {}).get("children", [])
            for post in posts:
                d = post["data"]
                url = f"https://reddit.com{d.get('permalink', '')}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                found_posts.append({
                    "title": d.get("title", ""),
                    "subreddit": d.get("subreddit_name_prefixed", ""),
                    "url": url,
                    "score": d.get("score", 0),
                })
        except Exception as e:
            log.warning("Reddit search failed for '%s': %s", keyword, e)

    if not found_posts:
        _last_reddit_week = week
        return

    lines = [f"🎯 Reddit Scout — {now.strftime('%d %b %Y')}\n",
             f"Found {len(found_posts)} posts about funding rates this week:\n"]

    for p in found_posts[:10]:
        lines.append(f"• [{p['subreddit']}] {p['title']}")
        lines.append(f"  {p['url']}\n")

    body = "\n".join(lines)
    send_email(OWNER_EMAIL, f"Reddit Scout — {now.strftime('%d %b %Y')}", body)
    _last_reddit_week = week
    log.info("Reddit scout: found %d posts", len(found_posts))
