"""
Static site content: changelog entries and guide posts.

No CMS in this codebase -- copy lives in code, same as every other page
(landing.html, rates.html). Kept in its own module rather than app.py so
routing logic and content don't bloat the same file, and so a non-engineer
edit to a guide's body doesn't touch anything that affects request handling.
"""
from typing import Optional

# ---------------------------------------------------------------------------
# Changelog -- newest first. Real, dated entries only; corrections to our
# own numbers are logged here too, not quietly fixed.
# ---------------------------------------------------------------------------

CHANGELOG: list[dict] = [
    {
        "date": "23 Jul 2026",
        "tag": "fix",
        "title": "Fee model corrected to real per-venue rates",
        "body": (
            "We were applying a single flat 0.26%-per-leg fee assumption to every "
            "exchange. It didn't match any of them. Fees are now sourced from each "
            "exchange's own published taker rate (Bybit 0.055%, Binance/OKX/Kraken "
            "0.05%, Hyperliquid 0.045%), which raises every net APY and lowers every "
            "breakeven figure that was previously overstated by roughly 5x."
        ),
    },
    {
        "date": "23 Jul 2026",
        "tag": "fix",
        "title": "Illiquid, thin-volume pairs no longer shown",
        "body": (
            "Pairs under $10M in 24h notional volume are now filtered from the board "
            "entirely, instead of showing a headline number that looked great and "
            "wasn't tradeable at any real size."
        ),
    },
    {
        "date": "23 Jul 2026",
        "tag": "coverage",
        "title": "Kraken funding rates added",
        "body": "Kraken joins Bybit, Binance, OKX and Hyperliquid in the live scan.",
    },
    {
        "date": "21 Jul 2026",
        "tag": "feature",
        "title": "Position size, cross-exchange spreads, realized accuracy",
        "body": (
            "Added a position-size selector that re-ranks every opportunity by what "
            "you'd actually net after slippage from live order book depth, a board for "
            "cross-exchange funding spreads (short the rich venue, long the cheap one), "
            "and a comparison of each pair's current headline number against what it "
            "actually delivered over the past week."
        ),
    },
    {
        "date": "19 Jul 2026",
        "tag": "feature",
        "title": "Stripe billing live",
        "body": "Upgrading to Pro now goes through a proper checkout flow.",
    },
    {
        "date": "14 Jul 2026",
        "tag": "feature",
        "title": "Free tier expanded: top 25 pairs, live, no delay",
        "body": (
            "The free tier previously showed 5 pairs delayed by 10 minutes. It now "
            "shows the top 25 pairs live, with no delay."
        ),
    },
    {
        "date": "14 Jul 2026",
        "tag": "feature",
        "title": "Dashboard redesign, watchlist, CSV export, Telegram alerts",
        "body": (
            "Rebuilt the scanner dashboard, added a starred watchlist, CSV export of "
            "the live board, and Telegram threshold alerts."
        ),
    },
]


# ---------------------------------------------------------------------------
# Guides -- newest first. Each is its own page at /guides/{slug}.
# ---------------------------------------------------------------------------

GUIDES: list[dict] = [
    {
        "slug": "why-gross-apy-lies-to-you",
        "title": "Why gross APY lies to you",
        "description": (
            "Gross funding rate is not a return. It's a headline number that ignores "
            "execution cost entirely — here's what actually happens when you try to "
            "trade it."
        ),
        "published": "20 Jul 2026",
        "body": [
            (
                "Open any funding-rate tracker and you'll see numbers like 400%, 600%, "
                "sometimes over 1,000% — annualised. These are real, in the narrow "
                "sense that the exchange's API really did return that rate for that "
                "8-hour window. But a gross funding rate is not a return, and treating "
                "it like one is how paper gains turn into real losses."
            ),
            (
                "A funding-rate arbitrage position isn't free to enter or exit. To "
                "capture the funding payment without taking directional risk, you "
                "open two positions (typically long spot, short perpetual, or the "
                "reverse) and close both later. That's four separate executions, each "
                "with its own fee, plus whatever the order book costs you in slippage "
                "on the way in and out."
            ),
            (
                "Extreme headline rates concentrate almost entirely in illiquid, "
                "low-volume instruments — not because those assets are secretly "
                "lucrative, but because a thin order book lets funding rates swing "
                "wildly with very little capital behind them. The same thinness means "
                "your entry and exit slippage can consume the entire advertised edge, "
                "sometimes several times over."
            ),
            (
                "This is exactly why FundScan ranks by net annualised yield after "
                "execution cost, not the gross figure — and, at position size, after "
                "an estimate of slippage from the venue's real order book depth. A "
                "628% headline rate on a pair with a near-empty book can collapse to "
                "single digits, or negative, once you size a real position into it. "
                "That collapse isn't a bug in the calculation. It's the point."
            ),
            (
                "The honest version of this pitch: a rate that can't be traded at "
                "reasonable size isn't an opportunity. It's an advertisement."
            ),
        ],
    },
    {
        "slug": "real-cost-of-a-funding-trade",
        "title": "The real cost of a funding-rate trade, leg by leg",
        "description": (
            "Four separate executions make up a single funding-rate arbitrage trade. "
            "Here's what each one actually costs, exchange by exchange."
        ),
        "published": "20 Jul 2026",
        "body": [
            (
                "A delta-neutral funding-rate position has four legs: open the spot "
                "leg, open the perpetual leg, then later close both. Each leg is a "
                "real trade with a real taker fee, and each exchange charges its own "
                "rate — there's no universal number that applies to all of them."
            ),
            (
                "FundScan's fee model uses each exchange's own published base-tier "
                "taker rate: Bybit at 0.055%, Binance/OKX/Kraken at 0.05%, and "
                "Hyperliquid at 0.045% per leg. Four legs at those rates, plus a "
                "slippage provision, gives a round-trip cost of roughly 0.23–0.27% of "
                "notional depending on the venue — a meaningfully different number "
                "from treating every exchange the same way."
            ),
            (
                "Fees alone aren't the whole cost. Even at the correct per-venue fee "
                "rate, a position large enough to move the order book will pay more "
                "than the quoted top-of-book price on the way in, and receive less on "
                "the way out. That's slippage, and it scales with your position size "
                "and the venue's actual depth — which is why FundScan estimates it "
                "from live order book data for whatever size you're actually trading, "
                "rather than assuming a flat percentage regardless of size."
            ),
            (
                "Add the fee model and the size-aware slippage estimate together and "
                "you get net yield at size — the number that should actually inform a "
                "decision, instead of the gross rate that ignores all of this "
                "entirely."
            ),
        ],
    },
    {
        "slug": "how-fundscan-calculates-net-yield",
        "title": "How FundScan actually calculates net yield",
        "description": (
            "A methodology walkthrough: gross rate, fee model, slippage-adjusted "
            "sizing, liquidity flags, and realized-vs-advertised accuracy."
        ),
        "published": "20 Jul 2026",
        "body": [
            (
                "Every number on the board starts from the same raw input: the "
                "current 8-hour (or hourly, normalised to an 8-hour equivalent) "
                "funding rate, pulled directly from each exchange's public API. "
                "Annualised naively, that's the gross APY — the number most other "
                "trackers stop at."
            ),
            (
                "From there, FundScan subtracts the real round-trip execution cost "
                "for that specific venue: four legs at that exchange's own taker fee, "
                "plus a slippage provision. That gives net APY — fee-adjusted, but "
                "still size-agnostic."
            ),
            (
                "Pick a position size on the dashboard and the picture gets more "
                "specific: FundScan walks the venue's live order book to estimate how "
                "much of that size could actually be filled, and at what average "
                "price, on both the entry and the exit. That slippage estimate "
                "replaces the flat provision, producing net yield at size — which is "
                "why the same pair can rank very differently at £250 versus £25,000."
            ),
            (
                "Alongside net yield, a liquidity badge flags position size as a "
                "percentage of the pair's 24-hour volume: green under 0.25%, amber up "
                "to 1%, red above it. And on each pair's history view, a realized-vs-"
                "advertised comparison shows what the pair's net APY actually averaged "
                "over the past week, next to what it's claiming right now — so a "
                "number that just spiked, and a number that's been consistently "
                "strong, don't look identical."
            ),
            (
                "None of this is a projection. It's a description of what the "
                "numbers say right now, computed the same way for every pair, so the "
                "ranking reflects what's actually tradeable rather than what looks "
                "best on a headline."
            ),
        ],
    },
]


def find_guide(slug: str) -> Optional[dict]:
    return next((g for g in GUIDES if g["slug"] == slug), None)
