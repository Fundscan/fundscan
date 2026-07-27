"""
Realized-vs-advertised accuracy.

A single point-in-time headline net APY hides how volatile funding rates
actually are — a pair showing 45% net APY right now might have averaged
12% over the past week. This compares today's headline number against
what was actually realized (a simple average) over recent history, using
the funding_snapshots data the fetch loop already persists every cycle.
No new data collection or schema change required.

Scope notes:
- The average is a plain arithmetic mean over snapshot rows, not a true
  time-weighted average -- it only approximates one when snapshots are
  evenly spaced (the normal case under a fixed FETCH_INTERVAL). A gap in
  collection (fetcher downtime, a pruned range) will skew it slightly,
  since a sparser period counts the same as a dense one.
- funding_snapshots stores math.net_apy() (rate + flat fee assumption),
  not a position-sized value — order book snapshots were never persisted,
  so this reflects realized rate reality, not realized slippage at a
  specific size. That would need a forward-collecting companion once
  order-book history exists.
"""
from statistics import mean
from typing import Optional


def realized_accuracy(history_rows: list) -> Optional[dict]:
    """
    `history_rows` are chronologically ordered (oldest-first) snapshot rows
    for a single (exchange, symbol), each exposing a 'net_apy' field (dict
    or sqlite3.Row both work). The most recent row is treated as "current".

    Returns None if there's no history yet (nothing to compare against).
    """
    if not history_rows:
        return None
    values = [r["net_apy"] for r in history_rows]
    current_net_apy = values[-1]
    realized_avg_net_apy = mean(values)
    return {
        "samples": len(values),
        "current_net_apy": current_net_apy,
        "realized_avg_net_apy": realized_avg_net_apy,
        "gap": current_net_apy - realized_avg_net_apy,
    }


# Below this many snapshots, a pair hasn't been tracked long enough for its
# realized average to mean anything -- excluded from the public aggregate
# rather than let a freshly-added pair's noise dilute the headline figure.
MIN_SAMPLES_FOR_AGGREGATE = 20


def aggregate_accuracy(accuracy_results: list) -> Optional[dict]:
    """
    Roll up individual realized_accuracy() results (one per exchange/symbol)
    into a single public trust signal: how far the headline net APY has
    typically been from what was actually realized, and how often the
    profitable/not-profitable call itself would have flipped.

    Each element of `accuracy_results` is a realized_accuracy() return value
    (or None, which is skipped). Returns None if no pair has enough history
    yet to clear MIN_SAMPLES_FOR_AGGREGATE.
    """
    eligible = [a for a in accuracy_results if a and a["samples"] >= MIN_SAMPLES_FOR_AGGREGATE]
    if not eligible:
        return None

    abs_gaps_pct = sorted(abs(a["gap"]) * 100 for a in eligible)
    mid = len(abs_gaps_pct) // 2
    median_gap_pct = (
        abs_gaps_pct[mid] if len(abs_gaps_pct) % 2 == 1
        else (abs_gaps_pct[mid - 1] + abs_gaps_pct[mid]) / 2
    )
    sign_agreements = sum(
        1 for a in eligible
        if (a["current_net_apy"] > 0) == (a["realized_avg_net_apy"] > 0)
    )

    return {
        "pairs_measured": len(eligible),
        "mean_abs_gap_pct": mean(abs_gaps_pct),
        "median_abs_gap_pct": median_gap_pct,
        "profitability_sign_agreement_pct": (sign_agreements / len(eligible)) * 100,
    }
