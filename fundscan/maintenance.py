"""
Maintenance agents:
  - DB Backup: daily copy of fundscan.db with timestamp
  - SEO Agent: weekly check of Google ranking for key terms
"""
import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .alerts import notify_owner
from .auth import send_email

log = logging.getLogger(__name__)

_last_backup_date: str = ""
_last_seo_week: str = ""

BACKUP_DIR = Path(os.getenv("DB_PATH", "fundscan.db")).parent / "backups"
OWNER_EMAIL = "bilguun@fundscan.uk"


def run_db_backup() -> None:
    """Agent: copies fundscan.db to backups/ once per day."""
    global _last_backup_date

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if _last_backup_date == today:
        return

    db_path = Path(os.getenv("DB_PATH", "fundscan.db"))
    if not db_path.exists():
        return

    BACKUP_DIR.mkdir(exist_ok=True)
    backup_path = BACKUP_DIR / f"fundscan_{today}.db"
    shutil.copy2(db_path, backup_path)

    # Keep only last 7 backups
    backups = sorted(BACKUP_DIR.glob("fundscan_*.db"))
    for old in backups[:-7]:
        old.unlink()

    _last_backup_date = today
    log.info("DB backed up to %s", backup_path)


def run_seo_check() -> None:
    """
    Agent: weekly check of approximate Google ranking for FundScan keywords.
    Uses a free search via SerpAPI-style approach — just checks if fundscan.uk
    appears in top results for key terms and emails you a summary.
    """
    global _last_seo_week

    now = datetime.now(timezone.utc)
    week = now.strftime("%Y-W%W")
    if _last_seo_week == week or now.weekday() != 0:  # Only on Mondays
        return

    try:
        import httpx
        keywords = [
            "funding rate scanner",
            "crypto funding rate tracker",
            "bybit binance funding rates",
            "cash and carry crypto yield",
        ]
        results = []
        for kw in keywords:
            try:
                r = httpx.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={
                        "key": os.getenv("GOOGLE_SEARCH_API_KEY", ""),
                        "cx": os.getenv("GOOGLE_SEARCH_CX", ""),
                        "q": kw,
                        "num": 10,
                    },
                    timeout=10,
                )
                items = r.json().get("items", [])
                rank = next(
                    (i + 1 for i, item in enumerate(items) if "fundscan" in item.get("link", "").lower()),
                    None
                )
                results.append(f"  '{kw}': {'#' + str(rank) if rank else 'not in top 10'}")
            except Exception:
                results.append(f"  '{kw}': error")

        body = f"📊 FundScan SEO Report — {now.strftime('%d %b %Y')}\n\nGoogle rankings:\n" + "\n".join(results)
        send_email(OWNER_EMAIL, f"FundScan SEO Report — {now.strftime('%d %b %Y')}", body)
        _last_seo_week = week
        log.info("SEO report sent")
    except Exception as e:
        log.error("SEO check failed: %s", e)
