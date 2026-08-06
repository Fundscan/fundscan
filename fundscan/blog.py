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
<p>Open almost any funding-rate tracker and the number it leads with is the
gross annualised rate — the raw funding payment, extrapolated out to a year,
with nothing subtracted. It's not a fabricated number. It's just not the one
that matters, because nobody earns funding without also paying to hold the
position that earns it.</p>

<h2>The number every scanner leads with</h2>
<p>A funding-rate arbitrage position isn't a single trade — it's a pair. You
open a spot position and an opposite perpetual position (delta-neutral, so
price movement washes out), collect the funding payment between exchanges,
then close both legs later. That's four separate executions: open spot, open
perp, close spot, close perp. Each one pays a taker fee. A tracker that only
shows the gross rate is showing you the payment and skipping the bill.</p>

<h2>What a delta-neutral position actually costs to run</h2>
<p>FundScan prices each of those four legs at the venue's own real published
taker rate — not a single flat guess applied to every exchange. Bybit charges
<b style="color:var(--ivory)">0.055%</b> per leg, Binance/OKX/Kraken
<b style="color:var(--ivory)">0.05%</b>, Hyperliquid
<b style="color:var(--ivory)">0.045%</b>. Four legs at Bybit's rate is
0.22% of notional, plus a 0.05% slippage provision for the round trip —
<b style="color:var(--ivory)">0.27% total</b>. That cost comes out of the
gross rate before anything is called an "opportunity." See
<a href="/#truth">the full walkthrough</a> for how it's applied line by line.</p>

<h2>A worked example, start to finish</h2>
<p>Take a Bybit pair funding at 0.01% per 8-hour period — an unremarkable,
realistic rate, not a headline-grabbing outlier:</p>
<ul>
  <li>Annualised out (3 funding periods a day, 1095 a year): <b style="color:var(--ivory)">10.95% gross</b>.</li>
  <li>Round-trip cost on Bybit — four legs plus slippage: <b style="color:var(--ivory)">−0.27%</b>.</li>
  <li>What's left: <b style="color:var(--ivory)">10.68% net</b>.</li>
</ul>
<p>On this pair the gap is small — that's the real-fee model doing its job,
not overstating cost the way a flat 0.26%-per-leg assumption used to. But the
gap isn't always small. A thinly traded pair advertising 150% gross can look
identical to this one on a gross-only tracker, while actually costing far
more than fees to trade — not in taker percentage, but in slippage from
trying to fill real size against a book that can't absorb it. Fees are a
known, fixed cost. Depth is not, and no gross number tells you which kind of
"expensive" you're looking at.</p>

<h2>What to check before opening a position</h2>
<p>In order: <b style="color:var(--ivory)">net APY</b> (what actually
survives the four legs), <b style="color:var(--ivory)">breakeven cycles</b>
(how many funding settlements it takes to recoup that cost — 27 in the
example above, roughly nine days at three a day), and the
<b style="color:var(--ivory)">liquidity flag</b> (whether the size you'd
actually trade fits the book, or whether the headline rate is a number you
could never realistically capture). A gross rate answers none of these. See
<a href="/rates">the live board</a> for all three side by side, and
<a href="/accuracy">the accuracy track record</a> for how these headline
figures have actually tracked their own recent history.</p>
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
