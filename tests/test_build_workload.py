"""Tests for src/risk/build_workload.py — workload class logic."""

import pytest

from src.risk.build_workload import compute_workload_classes


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
