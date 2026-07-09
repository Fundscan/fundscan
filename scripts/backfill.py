"""
Backfill 30 days of historical funding rates from Bybit for the top pairs.
Run once on fresh deployment:
    python -m scripts.backfill

Idempotent: safe to run multiple times (rows with duplicate ts/exchange/symbol
will be inserted again — deduplicate with a UNIQUE constraint if needed).
"""
import logging
import sys
from pathlib import Path

# allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from fundscan.db import init_db, get_conn
from fundscan.fetchers.bybit import fetch as fetch_current, fetch_history
from fundscan import math as fm

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DAYS = 30


def backfill():
    init_db()

    # Get current top symbols from Bybit
    log.info("Fetching current top symbols…")
    current = fetch_current()
    symbols = list({r["symbol"] for r in current})
    log.info("Found %d symbols", len(symbols))

    total = 0
    with get_conn() as conn:
        for symbol in symbols:
            log.info("Backfilling %s…", symbol)
            points = fetch_history(symbol, days=DAYS)
            if not points:
                log.warning("  No history for %s", symbol)
                continue

            rows = [
                (
                    # Convert ms epoch → ISO string
                    __import__("datetime").datetime.fromtimestamp(
                        p["timestamp_ms"] / 1000,
                        tz=__import__("datetime").timezone.utc,
                    ).isoformat(),
                    "bybit",
                    symbol,
                    p["rate_8h"],
                    fm.net_apy(p["rate_8h"]),
                )
                for p in points
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO funding_snapshots (ts, exchange, symbol, rate_8h, net_apy)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            total += len(rows)
            log.info("  Inserted %d rows for %s", len(rows), symbol)

    log.info("Backfill complete. Total rows inserted: %d", total)


if __name__ == "__main__":
    backfill()
