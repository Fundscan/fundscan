"""
Unit tests for realized-vs-advertised accuracy (fundscan/backtest.py).
"""
import pytest

from fundscan.backtest import realized_accuracy


def row(net_apy):
    return {"net_apy": net_apy}


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
