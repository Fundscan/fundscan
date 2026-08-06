"""
Blog / guides content registry.

No CMS or markdown pipeline -- each post is a plain dict with its body as a
literal HTML string, rendered directly into blog_post.html via Jinja's
`| safe`. To write a post: fill in `body_html` (and tighten `description`
to <=160 chars for the meta tag), then flip `published` to True and set
`published_at` to today's date.

Unpublished posts 404 on their own URL and are excluded from /blog's index
and from sitemap.xml -- shipping a thin placeholder page would cost more
in search-console "low value content" terms than having no page at all, so
nothing here goes live until you deliberately publish it.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Post:
    slug: str
    title: str
    description: str  # meta description + index teaser, keep under ~160 chars
    published: bool
    published_at: Optional[str]  # "YYYY-MM-DD" -- set this when you publish
    body_html: str


POSTS: list[Post] = [
    Post(
        slug="why-gross-apy-lies-to-you",
        title="Why the gross APY on your funding-rate dashboard lies to you",
        description=(
            "Gross funding rates ignore the four executions a delta-neutral "
            "position actually pays for. Here's what survives after costs."
        ),
        published=False,
        published_at=None,
        body_html="""
<p><em>Draft outline — replace this with the finished piece, then set
<code>published = True</code> and <code>published_at</code> in
fundscan/blog.py.</em></p>
<h2>The number every scanner leads with</h2>
<p>[Open with a real screenshot-style example: a rate page advertising a big
headline number. What does a trader assume that number means?]</p>
<h2>What a delta-neutral position actually costs to run</h2>
<p>Four executions — open spot, open perp, close spot, close perp — each
paying a taker fee, plus a slippage provision on the round trip. Point to
<a href="/#truth">the fee walkthrough</a> and the real per-venue rates in
<code>fundscan/math.py</code>.</p>
<h2>A worked example, start to finish</h2>
<p>Pull one real pair from <a href="/rates">/rates</a> and walk gross → net
line by line, the same sequence the homepage animates.</p>
<h2>What to check before opening a position</h2>
<p>Net APY, breakeven cycles, liquidity flag — not the headline number. Close
with a link to <a href="/accuracy">the accuracy track record</a>.</p>
""",
    ),
    Post(
        slug="funding-rate-arbitrage-fee-breakdown",
        title="The full fee breakdown behind a funding-rate arbitrage trade",
        description=(
            "Every fee a delta-neutral funding arbitrage position pays, leg "
            "by leg and venue by venue."
        ),
        published=False,
        published_at=None,
        body_html="""
<p><em>Draft outline — replace this with the finished piece, then set
<code>published = True</code> and <code>published_at</code>.</em></p>
<h2>Four legs, four fees</h2>
<p>Open spot, open perp, close spot, close perp — walk through
<code>PER_VENUE_FEE_PER_LEG</code> venue by venue (Bybit 0.055%, Binance/OKX/
Kraken 0.05%, Hyperliquid 0.045%).</p>
<h2>Why a flat per-leg assumption misprices most venues</h2>
<p>Explain the difference between a single flat estimate and each exchange's
real published taker rate, and why it matters for breakeven accuracy.</p>
<h2>Slippage on top</h2>
<p>The one-way slippage provision baked into every net figure — a provision,
not a promise.</p>
<h2>What "breakeven cycles" means in practice</h2>
<p>Tie back to the breakeven column on <a href="/rates">/rates</a> — how many
funding settlements it takes to recover the round trip.</p>
""",
    ),
    Post(
        slug="fundscan-methodology",
        title="FundScan's methodology: net APY, breakeven cycles, and liquidity flags",
        description=(
            "A full walkthrough of FundScan's cost model — fee assumptions, "
            "breakeven math, illiquidity flags, and where the numbers come from."
        ),
        published=False,
        published_at=None,
        body_html="""
<p><em>Draft outline — replace this with the finished piece, then set
<code>published = True</code> and <code>published_at</code>.</em></p>
<h2>What FundScan measures</h2>
<p>Net annualised yield after the round-trip cost of a delta-neutral funding
position, refreshed every 60 seconds from public exchange data only.</p>
<h2>The cost model</h2>
<p>Per-venue taker fees, four legs, a slippage provision — link to
<a href="/#truth">the methodology section</a> and
<a href="/accuracy">the accuracy track record</a> for the live version of
this math.</p>
<h2>Breakeven cycles</h2>
<p>How many funding settlements it takes to recoup the round-trip cost.</p>
<h2>Illiquidity flags</h2>
<p>How the LIQUID / THIN / ILLIQUID badge is derived from 24h volume, and why
an extreme headline rate on a thin pair usually isn't a real opportunity.</p>
<h2>Where this can be wrong</h2>
<p>Link straight to <a href="/accuracy">/accuracy</a> — the honest
limitations, not just the pitch.</p>
""",
    ),
]


def get_post(slug: str) -> Optional[Post]:
    for p in POSTS:
        if p.slug == slug and p.published:
            return p
    return None


def published_posts() -> list[Post]:
    return sorted(
        (p for p in POSTS if p.published),
        key=lambda p: p.published_at or "",
        reverse=True,
    )
