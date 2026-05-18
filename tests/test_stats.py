"""Tests for src/pipeline/common/stats.py — shared geom_mean_quartile helpers."""

import pytest

from src.pipeline.common.stats import (
    geometric_mean,
    hazen_percentiles,
    quartile_classes,
)


class TestHazenPercentiles:
    def test_strictly_between_0_and_100(self):
        # Even the minimum value must be > 0 (so a geometric mean can't collapse).
        pctls = hazen_percentiles([5, 1, 9, 3])
        assert all(0 < p < 100 for p in pctls)

    def test_monotonic_with_value(self):
        # Higher input value -> higher percentile.
        pctls = hazen_percentiles([10, 20, 30, 40])
        assert pctls == sorted(pctls)
        assert pctls[0] < pctls[-1]

    def test_negative_values_rank_low(self):
        # A negative input must rank at the low end.
        pctls = hazen_percentiles([-5.0, 0.0, 10.0, 50.0])
        assert pctls[0] == min(pctls)

    def test_ties_share_average_rank(self):
        pctls = hazen_percentiles([7, 7, 7, 7])
        assert pctls[0] == pctls[1] == pctls[2] == pctls[3]
        assert pctls[0] == pytest.approx(50.0)  # 100*(2.5-0.5)/4

    def test_empty(self):
        assert hazen_percentiles([]) == []


class TestGeometricMean:
    def test_known_value(self):
        assert geometric_mean([4.0, 9.0]) == pytest.approx(6.0)

    def test_three_values(self):
        assert geometric_mean([8.0, 8.0, 8.0]) == pytest.approx(8.0)

    def test_empty(self):
        assert geometric_mean([]) == 0.0


class TestQuartileClasses:
    def test_equal_count_split(self):
        # 8 distinct scores -> 2 per class, A = highest.
        classes = quartile_classes([1, 2, 3, 4, 5, 6, 7, 8])
        assert classes == ["D", "D", "C", "C", "B", "B", "A", "A"]

    def test_remainder_within_one(self):
        # 10 scores -> each class holds 2 or 3.
        from collections import Counter
        classes = quartile_classes(list(range(10)))
        counts = Counter(classes)
        assert all(c in (2, 3) for c in counts.values())
        assert set(counts) == {"A", "B", "C", "D"}

    def test_highest_score_is_class_a(self):
        classes = quartile_classes([10, 99, 50, 1])
        assert classes[1] == "A"  # 99 is the highest

    def test_empty(self):
        assert quartile_classes([]) == []
