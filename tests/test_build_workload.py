"""Tests for src/pipeline/risk/build_workload.py — workload class logic."""

import pytest

from src.pipeline.risk.build_workload import (
    _geometric_mean,
    _hazen_percentiles,
    _quartile_classes,
    compute_workload_classes,
)


class TestHazenPercentiles:
    def test_strictly_between_0_and_100(self):
        # Even the minimum value must be > 0 (so a geometric mean can't collapse).
        pctls = _hazen_percentiles([5, 1, 9, 3])
        assert all(0 < p < 100 for p in pctls)

    def test_monotonic_with_value(self):
        # Higher input value → higher percentile.
        pctls = _hazen_percentiles([10, 20, 30, 40])
        assert pctls == sorted(pctls)
        assert pctls[0] < pctls[-1]

    def test_negative_values_rank_low(self):
        # A negative ratio (e.g. negative NNI) must rank at the low end.
        vals = [-5.0, 0.0, 10.0, 50.0]
        pctls = _hazen_percentiles(vals)
        assert pctls[0] == min(pctls)

    def test_ties_share_average_rank(self):
        pctls = _hazen_percentiles([7, 7, 7, 7])
        assert pctls[0] == pctls[1] == pctls[2] == pctls[3]
        assert pctls[0] == pytest.approx(50.0)  # 100*(2.5-0.5)/4

    def test_empty(self):
        assert _hazen_percentiles([]) == []


class TestGeometricMean:
    def test_known_value(self):
        assert _geometric_mean([4.0, 9.0]) == pytest.approx(6.0)

    def test_three_values(self):
        assert _geometric_mean([8.0, 8.0, 8.0]) == pytest.approx(8.0)

    def test_empty(self):
        assert _geometric_mean([]) == 0.0


class TestQuartileClasses:
    def test_equal_count_split(self):
        # 8 distinct scores → 2 per class, A = highest.
        classes = _quartile_classes([1, 2, 3, 4, 5, 6, 7, 8])
        assert classes == ["D", "D", "C", "C", "B", "B", "A", "A"]

    def test_remainder_within_one(self):
        # 10 scores → each class holds 2 or 3.
        from collections import Counter
        classes = _quartile_classes(list(range(10)))
        counts = Counter(classes)
        assert all(c in (2, 3) for c in counts.values())
        assert set(counts) == {"A", "B", "C", "D"}

    def test_highest_score_is_class_a(self):
        classes = _quartile_classes([10, 99, 50, 1])
        assert classes[1] == "A"  # 99 is the highest

    def test_empty(self):
        assert _quartile_classes([]) == []


class TestComputeWorkloadClasses:
    @staticmethod
    def _metric(repo, loc, cve, nni, ac):
        return {"repo": repo, "loc": loc, "cve": cve, "nni": nni, "ac": ac}

    def test_classifies_repos_with_all_inputs(self):
        metrics = [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i), ac=2.0)
            for i in range(1, 9)
        ]
        out = compute_workload_classes(metrics)
        assert set(out) == {f"r/{i}" for i in range(1, 9)}
        # r/8 carries the most burden per contributor → class A.
        assert out["r/8"]["workload_class"] == "A"
        assert out["r/1"]["workload_class"] == "D"

    def test_missing_cve_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=None, nni=5.0, ac=2.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
            self._metric("r/c", loc=3000.0, cve=4.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""
        assert out["r/a"]["loc_per_ac"] == ""

    def test_zero_ac_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=3.0, nni=5.0, ac=0.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""

    def test_negative_nni_still_classified(self):
        # A repo closing issues faster than it opens them (negative NNI)
        # must still receive a class — it just lands in the low-burden tail.
        metrics = [
            self._metric("r/neg", loc=500.0, cve=0.0, nni=-40.0, ac=4.0),
        ] + [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i * 10), ac=4.0)
            for i in range(1, 8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/neg"]["workload_class"] != ""
        assert out["r/neg"]["nni_per_ac"] == pytest.approx(-10.0)

    def test_ratios_rounded(self):
        metrics = [
            self._metric(f"r/{i}", loc=1000.0, cve=2.0, nni=4.0, ac=3.0)
            for i in range(8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/0"]["loc_per_ac"] == pytest.approx(333.3333, abs=1e-3)
