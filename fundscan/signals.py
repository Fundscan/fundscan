"""
KKB integration Phase 3 -- signal generation for /api/signals/kraken.

Turns FundScan's own board data into structured, expiring trade signals
for the specific Kraken pairs KKB can currently execute. This module does
not invent a new opportunity-detection model: every field is derived from
scanner output, sizing.py, and backtest.py's realized-vs-current accuracy
check that already power the dashboard -- confidence is a transparent,
explainable heuristic built from those three real signals, not a
fabricated ML-style score.
"""
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import sizing as sz
from .backtest import realized_accuracy
from .db import query_history

SIGNAL_TTL_SECONDS = 7 * 60  # signals go stale after ~7 minutes

# The GBP spot pair + capital allocation for each Kraken perp KKB can
# currently trade. Mirrors kkb_config.json's "pairs" section on the KKB
# side (BTC/GBP 1787.5, ETH/GBP 162.5, SOL/GBP 650) -- duplicated here
# deliberately, not imported from KKB, since the two systems are meant to
# stay independently deployable. Override via KRAKEN_TRADEABLE_PAIRS env
# var (JSON object, same shape) without touching this file or KKB's config.
_DEFAULT_TRADEABLE_PAIRS = {
    "PF_XBTUSD": {"pair": "BTC/GBP", "suggested_capital_gbp": 1787.5},
    "PF_ETHUSD": {"pair": "ETH/GBP", "suggested_capital_gbp": 162.5},
    "PF_SOLUSD": {"pair": "SOL/GBP", "suggested_capital_gbp": 650.0},
}


def _tradeable_pairs() -> dict:
    raw = os.getenv("KRAKEN_TRADEABLE_PAIRS")
    if not raw:
        return _DEFAULT_TRADEABLE_PAIRS
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("KRAKEN_TRADEABLE_PAIRS must be a non-empty JSON object")
        return parsed
    except (json.JSONDecodeError, ValueError):
        return _DEFAULT_TRADEABLE_PAIRS


def _confidence_and_reason(row: dict, sized: dict, accuracy: Optional[dict]) -> tuple[float, str]:
    """
    Transparent 0-1 confidence built from three things FundScan already
    knows, not an invented score:
      - is it actually profitable after real per-venue fees + slippage
        at the suggested capital (sizing.py)?
      - is the position size liquid against 24h volume (sizing.py)?
      - is the current rate near its realized 7-day average, or is it a
        spike likely to compress before the position can earn much
        (backtest.realized_accuracy)?
    Starts at a neutral 0.5, not 1.0 -- confidence should never claim
    certainty, and there's always a reason string attached explaining
    exactly which factors moved it.
    """
    confidence = 0.5
    reasons = []

    net_at_size = sized.get("net_apy_at_size")
    if net_at_size is not None and net_at_size > 0:
        confidence += 0.2
        reasons.append("net positive after fees+slippage at suggested size")
    else:
        confidence -= 0.2
        reasons.append("not clearing costs at suggested size")

    liq_flag = sized.get("liquidity_flag")
    if liq_flag == "green":
        confidence += 0.15
        reasons.append("liquid at suggested size")
    elif liq_flag == "red":
        confidence -= 0.15
        reasons.append("thin relative to suggested size")

    if accuracy and accuracy["samples"] >= 3:
        gap = accuracy["gap"]
        # Current running meaningfully above its own 7-day realized
        # average suggests a spike that's likely to compress -- lower
        # confidence rather than chase the headline number.
        if gap > 0.05:
            confidence -= 0.15
            reasons.append(f"current rate running {gap*100:.1f}pp above 7d realized avg, possible compression")
        else:
            confidence += 0.1
            reasons.append("current rate consistent with 7d realized average")
    else:
        reasons.append("not enough history yet for a realized-accuracy check")

    confidence = max(0.1, min(0.95, round(confidence, 2)))
    reason = "; ".join(reasons)
    return confidence, reason


def build_kraken_signals(results: list[dict]) -> list[dict]:
    """
    Given the scanner's current results, emit one signal per tradeable
    Kraken pair that FundScan currently has fresh data for. Only rates
    the direction FundScan/KKB's model actually trades: hold spot long,
    short the perp, collect positive funding. A pair with a negative rate
    right now (the profitable trade would require shorting spot, which
    this model doesn't support) is omitted rather than emitting a signal
    nobody can act on.
    """
    tradeable = _tradeable_pairs()
    by_symbol = {r["symbol"]: r for r in results if r["exchange"] == "kraken"}
    generated_at = datetime.now(timezone.utc)
    expires_at = generated_at + timedelta(seconds=SIGNAL_TTL_SECONDS)

    signals = []
    for perp_symbol, meta in tradeable.items():
        row = by_symbol.get(perp_symbol)
        if not row or row["rate_8h"] <= 0:
            continue

        capital = meta["suggested_capital_gbp"]
        sized = sz.size_opportunity(row, capital)
        history = query_history(perp_symbol, days=7)
        accuracy = realized_accuracy(history)
        confidence, reason = _confidence_and_reason(row, sized, accuracy)

        signals.append({
            "signal_id": str(uuid.uuid4()),
            "pair": meta["pair"],
            "perp_symbol": perp_symbol,
            "action": "enter_long_spot_short_perp",
            "funding_rate_pct": round(row["rate_8h"] * 100, 4),
            "confidence": confidence,
            "suggested_capital_gbp": capital,
            "reason": reason,
            "expires_at": expires_at.isoformat(),
        })

    return {
        "generated_at": generated_at.isoformat(),
        "signals": signals,
    }
