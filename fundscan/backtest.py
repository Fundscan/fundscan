"""
Realized-vs-advertised accuracy.

A single point-in-time headline net APY hides how volatile funding rates
actually are — a pair showing 45% net APY right now might have averaged
12% over the past week. This compares today's headline number against
what was actually realized (time-weighted average) over recent history,
using the funding_snapshots data the fetch loop already persists every
cycle. No new data collection or schema change required.

Scope note: funding_snapshots stores math.net_apy() (rate + flat fee
assumption), not a position-sized value — order book snapshots were never
persisted, so this reflects realized rate reality, not realized slippage
at a specific size. That would need a forward-collecting companion once
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
