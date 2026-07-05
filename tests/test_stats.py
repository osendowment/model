"""Tests for the shared risk-percentile statistics helpers."""

from src.common.stats import (
    floor_anchored_risk,
    geom_mean_composite,
    geometric_mean,
    max_composite,
    max_composite_any,
    risk_percentiles,
    risk_percentiles_aligned,
)


class TestFloorAnchoredRisk:
    def test_floor_value_pinned_to_anchor(self):
        # 0 CVEs -> 50 (neutral), regardless of how many repos share it.
        out = floor_anchored_risk([0.0, 0.0, 0.0, 1.0, 5.0, 88.0])
        assert out[0] == out[1] == out[2] == 50.0

    def test_worst_above_floor_maps_to_100(self):
        out = floor_anchored_risk([0.0, 1.0, 5.0, 88.0])
        assert out[3] == 100.0

    def test_strictly_increasing_above_floor(self):
        out = floor_anchored_risk([0.0, 1.0, 5.0, 88.0])
        assert 50.0 < out[1] < out[2] < out[3]

    def test_zero_to_fifty_not_seventy_eight(self):
        # The whole point: a large zero-tie group sits at 50, not at its CDF
        # position (here 80% of repos have 0 CVEs).
        vals = [0.0] * 8 + [3.0, 10.0]
        out = floor_anchored_risk(vals, floor=0.0, anchor=50.0)
        assert all(v == 50.0 for v in out[:8])
        assert out[9] == 100.0

    def test_missing_stays_none(self):
        out = floor_anchored_risk([None, 0.0, 5.0])
        assert out[0] is None and out[1] == 50.0

    def test_all_floor_no_above(self):
        # No value exceeds the floor -> everyone neutral, nobody pinned to 100.
        out = floor_anchored_risk([0.0, 0.0, 0.0])
        assert out == [50.0, 50.0, 50.0]


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


class TestMaxCompositeAny:
    def test_scores_off_present_axis(self):
        # openssf missing (None), cve present → score off cve alone.
        assert max_composite_any([[None, 73.0]]) == [73.0]

    def test_identical_to_max_when_all_present(self):
        assert max_composite_any([[25.0, 100.0]]) == max_composite([[25.0, 100.0]])

    def test_none_only_when_all_missing(self):
        assert max_composite_any([[None, None]]) == [None]
        assert max_composite_any([[]]) == [None]


class TestGeomMeanComposite:
    def test_geom_mean_of_present(self):
        assert geom_mean_composite([[25.0, 100.0]]) == [50.0]

    def test_none_if_any_missing(self):
        assert geom_mean_composite([[100.0, None]]) == [None]

    def test_empty_row_is_none(self):
        assert geom_mean_composite([[]]) == [None]


class TestMaxComposite:
    def test_max_of_present(self):
        # worst-of: the higher axis wins, no dilution by the lower one
        assert max_composite([[25.0, 100.0]]) == [100.0]

    def test_worst_of_not_geom_mean(self):
        # a high axis is NOT pulled down by a neutral sibling (geom mean would
        # give sqrt(100*50)=70.7); max keeps the worst axis intact
        assert max_composite([[100.0, 50.0]]) == [100.0]

    def test_none_if_any_missing(self):
        assert max_composite([[100.0, None]]) == [None]

    def test_empty_row_is_none(self):
        assert max_composite([[]]) == [None]


class TestGeometricMean:
    def test_basic(self):
        assert geometric_mean([4.0, 9.0]) == 6.0

    def test_empty(self):
        assert geometric_mean([]) == 0.0
