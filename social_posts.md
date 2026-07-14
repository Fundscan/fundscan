# FundScan — X/Twitter Posts

---

## 1. Launch Announcement

most funding rate scanners show you the headline yield.

none of them subtract the fees.

FundScan does.

net APY, ranked. breakeven cycles shown. sub-cost pairs greyed out.

free to use → fundscan.uk

---

## 2. Build in Public

built a funding arb bot for myself. it made money. the hard part wasn't the strategy — it was knowing which pairs were actually worth the entry after fees.

4 legs × 0.26% adds up fast. a 40% APY pair becomes 38%. a 10% pair becomes noise.

so i built the scanner as a product: fundscan.uk

---

## 3. Educational Thread (post as thread)

**Tweet 1**
funding rate arbitrage looks simple on paper:

long spot → short perp → collect the funding rate

here's what actually eats the yield 🧵

**Tweet 2**
to enter delta-neutral you need 4 executions:
• buy spot
• sell perp

to exit:
• sell spot
• close perp

that's 4 taker fills. on most venues: ~0.06% each
total: ~0.24% round trip, before slippage

**Tweet 3**
funding is paid every 8 hours on most exchanges

annualised = rate × 3 × 365

but that gross figure means nothing until you ask:
how many 8-hour cycles does it take to recover entry costs?

a 30% APY pair needs ~10 cycles just to break even

**Tweet 4**
most scanners rank by gross rate.

they'll show you a 12% APY pair as an opportunity.

after 4 legs of fees, you're at 9.7%.
after slippage, 9%.
minus the cost to exit when the rate normalises — you're barely positive.

**Tweet 5**
FundScan runs this math continuously across Bybit, Binance, and OKX.

every pair ranked by net annualised yield.
breakeven cycles shown.
pairs that don't clear fees are greyed out, not promoted.

no API keys. no account connection. just the honest figure.

fundscan.uk

---

## 4. Data-Driven Post (use when there's a notable opportunity)

[SYMBOL] funding rate is running at [X]% APY on [exchange].

net of fees: [X-fees]% APY
breakeven: [N] cycles

most tools won't show you the second two numbers.

fundscan.uk does — free.

---

## 5. Social Proof / Milestone

[N] traders now using FundScan to screen funding arb opportunities.

the most common reaction: "i had no idea how much fees were taking"

the answer, usually, is about 2-3% annualised per trade. small positions, it doesn't matter. real size, it's the difference.

fundscan.uk

---

## 6. Quiet Authority Post (no CTA, just value)

the pairs that look best on a funding scanner usually aren't.

high rates attract flow → rates compress → you exit at a loss on the position you entered for the rate.

the better filter: net yield after fees AND sufficient rate history to suggest persistence.

most scanners don't show either.

---

## Posting Cadence (suggested)

| Week | Post |
|------|------|
| Day 1 | #1 Launch announcement |
| Day 2 | #6 Quiet authority (no CTA — builds credibility first) |
| Day 4 | #3 Educational thread |
| Day 7 | #2 Build in public |
| Ongoing | #4 Data-driven (whenever a notable rate appears) |
| At 100 users | #5 Social proof |
