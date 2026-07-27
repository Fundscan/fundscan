"""
Unit tests for realized-vs-advertised accuracy (fundscan/backtest.py).
"""
import pytest

from fundscan.backtest import MIN_SAMPLES_FOR_AGGREGATE, aggregate_accuracy, realized_accuracy


def row(net_apy):
    return {"net_apy": net_apy}


def acc_result(current, realized_avg, samples=MIN_SAMPLES_FOR_AGGREGATE):
    """Build a realized_accuracy()-shaped dict directly, for aggregate tests."""
    return {
        "samples": samples,
        "current_net_apy": current,
        "realized_avg_net_apy": realized_avg,
        "gap": current - realized_avg,
    }


def test_no_history_returns_none():
    assert realized_accuracy([]) is None


def test_flat_history_has_zero_gap():
    rows = [row(0.10)] * 5
    out = realized_accuracy(rows)
    assert out["samples"] == 5
    assert out["current_net_apy"] == pytest.approx(0.10)
    assert out["realized_avg_net_apy"] == pytest.approx(0.10)
    assert out["gap"] == pytest.approx(0.0)


def test_current_is_the_most_recent_row_not_the_max():
    # Oldest-first ordering: current must be the LAST row, even if an
    # earlier row had a higher value.
    rows = [row(0.80), row(0.05)]
    out = realized_accuracy(rows)
    assert out["current_net_apy"] == pytest.approx(0.05)


def test_volatile_history_headline_overstates_realized_yield():
    # A pair that spiked to 600% once and has mostly sat near zero since --
    # today's headline (last row) reads high, but the realized average
    # over the window is far lower.
    rows = [row(6.0)] + [row(0.01)] * 19  # 1 spike + 19 flat samples
    out = realized_accuracy(rows)
    assert out["current_net_apy"] == pytest.approx(0.01)
    assert out["realized_avg_net_apy"] == pytest.approx((6.0 + 0.01 * 19) / 20)
    assert out["realized_avg_net_apy"] > out["current_net_apy"]


def test_gap_positive_when_current_exceeds_realized_average():
    rows = [row(0.01)] * 19 + [row(6.0)]  # headline just spiked
    out = realized_accuracy(rows)
    assert out["gap"] > 0
    assert out["current_net_apy"] == pytest.approx(6.0)


def test_gap_negative_when_current_below_realized_average():
    rows = [row(6.0)] + [row(0.01)] * 19  # headline has since cooled off
    out = realized_accuracy(rows)
    assert out["gap"] < 0


def test_single_sample_is_its_own_realized_average():
    out = realized_accuracy([row(0.25)])
    assert out["samples"] == 1
    assert out["realized_avg_net_apy"] == pytest.approx(0.25)
    assert out["gap"] == pytest.approx(0.0)


def test_works_with_sqlite_row_like_mapping_access():
    # query_history() returns sqlite3.Row objects; anything supporting
    # ['net_apy'] indexing must work, not just plain dicts.
    class FakeRow(dict):
        pass
    rows = [FakeRow(net_apy=0.1), FakeRow(net_apy=0.2)]
    out = realized_accuracy(rows)
    assert out["realized_avg_net_apy"] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# aggregate_accuracy -- public trust-page rollup
# ---------------------------------------------------------------------------

def test_aggregate_empty_input_returns_none():
    assert aggregate_accuracy([]) is None


def test_aggregate_ignores_none_results():
    # realized_accuracy() returns None for pairs with zero history --
    # the aggregate must skip those rather than crash on them.
    results = [None, None, acc_result(0.10, 0.10)]
    out = aggregate_accuracy(results)
    assert out["pairs_measured"] == 1


def test_aggregate_excludes_pairs_below_min_samples():
    # A pair tracked for only a few cycles shouldn't count toward the
    # headline figure -- its average is too noisy to mean anything yet.
    freshly_tracked = acc_result(0.50, 0.10, samples=MIN_SAMPLES_FOR_AGGREGATE - 1)
    established = acc_result(0.10, 0.10, samples=MIN_SAMPLES_FOR_AGGREGATE)
    out = aggregate_accuracy([freshly_tracked, established])
    assert out["pairs_measured"] == 1


def test_aggregate_returns_none_when_nothing_clears_min_samples():
    freshly_tracked = acc_result(0.50, 0.10, samples=1)
    assert aggregate_accuracy([freshly_tracked]) is None


def test_aggregate_mean_and_median_gap_in_percentage_points():
    results = [
        acc_result(0.10, 0.10),   # gap = 0.00 -> 0pp
        acc_result(0.15, 0.10),   # gap = 0.05 -> 5pp
        acc_result(0.20, 0.10),   # gap = 0.10 -> 10pp
    ]
    out = aggregate_accuracy(results)
    assert out["mean_abs_gap_pct"] == pytest.approx(5.0)
    assert out["median_abs_gap_pct"] == pytest.approx(5.0)


def test_aggregate_median_of_even_count_averages_middle_two():
    results = [
        acc_result(0.10, 0.10),  # 0pp
        acc_result(0.14, 0.10),  # 4pp
        acc_result(0.16, 0.10),  # 6pp
        acc_result(0.30, 0.10),  # 20pp
    ]
    out = aggregate_accuracy(results)
    assert out["median_abs_gap_pct"] == pytest.approx((4.0 + 6.0) / 2)


def test_aggregate_sign_agreement_counts_profitable_call_flips():
    results = [
        acc_result(0.10, 0.10),    # both positive -> agree
        acc_result(0.05, -0.02),   # current positive, realized negative -> disagree
        acc_result(-0.03, -0.01),  # both negative -> agree
        acc_result(-0.01, 0.02),   # current negative, realized positive -> disagree
    ]
    out = aggregate_accuracy(results)
    assert out["profitability_sign_agreement_pct"] == pytest.approx(50.0)


def test_aggregate_full_sign_agreement():
    results = [acc_result(0.10, 0.12), acc_result(0.20, 0.18)]
    out = aggregate_accuracy(results)
    assert out["profitability_sign_agreement_pct"] == pytest.approx(100.0)
