"""Tests for the shared risk-percentile statistics helpers."""

from src.pipeline.common.stats import (
    geom_mean_composite,
    geometric_mean,
    risk_percentiles,
    risk_percentiles_aligned,
)


class TestRiskPercentiles:
    def test_higher_is_worse_max_is_100(self):
        p = risk_percentiles([1.0, 2.0, 3.0, 4.0], higher_is_worse=True)
        assert p[3] == 100.0
        assert p[0] == 25.0

    def test_lower_is_worse_min_is_100(self):
        p = risk_percentiles([1.0, 2.0, 3.0, 4.0], higher_is_worse=False)
        assert p[0] == 100.0
        assert p[3] == 25.0

    def test_bus_factor_1_is_100(self):
        p = risk_percentiles([1.0, 1.0, 2.0, 3.0], higher_is_worse=False)
        assert p[0] == 100.0 and p[1] == 100.0
        assert p[2] == 50.0
        assert p[3] == 25.0

    def test_ties_counted_for_common_best(self):
        p = risk_percentiles([0.0, 0.0, 0.0, 1.0], higher_is_worse=True)
        assert p[0] == 75.0
        assert p[3] == 100.0

    def test_best_value_is_above_zero(self):
        p = risk_percentiles([10.0, 20.0, 30.0], higher_is_worse=True)
        assert p[0] > 0.0

    def test_constant_axis_returns_none(self):
        assert risk_percentiles([5.0, 5.0, 5.0], higher_is_worse=True) == [None, None, None]

    def test_empty(self):
        assert risk_percentiles([], higher_is_worse=True) == []


class TestRiskPercentilesAligned:
    def test_none_passthrough_and_rank_among_present(self):
        p = risk_percentiles_aligned([1.0, None, 2.0], higher_is_worse=True)
        assert p[1] is None
        assert p[0] == 50.0 and p[2] == 100.0

    def test_all_none(self):
        assert risk_percentiles_aligned([None, None], higher_is_worse=True) == [None, None]


class TestGeomMeanComposite:
    def test_geom_mean_of_present(self):
        assert geom_mean_composite([[25.0, 100.0]]) == [50.0]

    def test_none_if_any_missing(self):
        assert geom_mean_composite([[100.0, None]]) == [None]

    def test_empty_row_is_none(self):
        assert geom_mean_composite([[]]) == [None]


class TestGeometricMean:
    def test_basic(self):
        assert geometric_mean([4.0, 9.0]) == 6.0

    def test_empty(self):
        assert geometric_mean([]) == 0.0
