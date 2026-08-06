"""
Public changelog.

Seeded from this repo's real git history (`git log`), curated into
user-facing summaries of actual shipped changes -- deploy retries, debug
commits, and internal-only tooling are left out. Add a new Entry at the
top of ENTRIES (most recent first) when something user-facing ships.
"""
from dataclasses import dataclass


@dataclass
class Entry:
    date: str  # "YYYY-MM-DD"
    title: str
    detail: str


ENTRIES: list[Entry] = [
    Entry(
        "2026-08-06",
        "Accuracy track record, low-friction alerts, and guides",
        "Added a public page comparing headline vs. realized net APY per "
        "pair, an email-only “notify me” signup that doesn't "
        "require creating an account, and a guides section for methodology "
        "write-ups.",
    ),
    Entry(
        "2026-07-23",
        "Illiquidity flags",
        "Pairs are now flagged LIQUID / THIN / ILLIQUID based on 24h volume, "
        "instead of ranking every pair by headline yield regardless of "
        "whether it's tradeable at size.",
    ),
    Entry(
        "2026-07-23",
        "Real per-venue fee model",
        "Replaced the flat 0.26%-per-leg fee assumption with each exchange's "
        "actual published taker rate (Bybit, Binance, OKX, Hyperliquid, "
        "Kraken) — the old flat number overstated real costs by "
        "roughly 5x.",
    ),
    Entry(
        "2026-07-23",
        "Kraken coverage added",
        "Kraken joined Bybit, Binance, OKX, and Hyperliquid in the scanned "
        "venue list.",
    ),
    Entry(
        "2026-07-21",
        "Position sizing, cross-exchange spreads, realized accuracy",
        "Added yield ranking at a chosen position size, a cross-exchange "
        "spread board, a realized-accuracy comparison per pair, and the "
        "public /rates page.",
    ),
    Entry(
        "2026-07-19",
        "Branded embedded checkout",
        "Replaced the default Stripe redirect with a custom-branded embedded "
        "checkout page.",
    ),
    Entry(
        "2026-07-14",
        "Free tier expanded",
        "Removed the 10-minute delay and expanded the free tier from 5 to "
        "25 live pairs.",
    ),
    Entry(
        "2026-07-14",
        "Hyperliquid, sparklines, watchlist, CSV export",
        "Added Hyperliquid coverage, 24-hour inline sparklines, a starrable "
        "watchlist, and CSV export of the current rate table.",
    ),
    Entry(
        "2026-07-12",
        "Principal tier goes live",
        "Enabled live Stripe checkout for the paid Principal tier.",
    ),
    Entry(
        "2026-07-10",
        "FundScan launches",
        "Net-of-fees funding rate scanner across Bybit, Binance, and OKX.",
    ),
]


def latest_entry() -> Entry:
    return ENTRIES[0]
