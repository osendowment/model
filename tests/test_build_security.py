"""Tests for src/pipeline/risk/build_security.py — security class logic."""

import pytest

from src.pipeline.risk.build_security import compute_security_classes


def _metric(repo, openssf_score, cve):
    return {"repo": repo, "openssf_score": openssf_score, "cve": cve}


class TestComputeSecurityClasses:
    def test_classifies_repos_with_both_inputs(self):
        # 8 repos worsening on both axes -> r/8 worst, r/1 best.
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i, cve=float(i))
            for i in range(1, 9)
        ]
        out = compute_security_classes(metrics)
        assert set(out) == {f"r/{i}" for i in range(1, 9)}
        assert out["r/8"]["security_class"] == "A"   # lowest score + most CVEs
        assert out["r/1"]["security_class"] == "D"   # highest score + fewest CVEs

    def test_openssf_score_is_inverted(self):
        # The repo with the LOWEST openssf_score gets the HIGHEST risk pctl.
        metrics = [
            _metric("r/low", openssf_score=1.0, cve=0.0),
            _metric("r/mid", openssf_score=5.0, cve=0.0),
            _metric("r/high", openssf_score=9.0, cve=0.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/low"]["openssf_risk_pctl"] > out["r/high"]["openssf_risk_pctl"]

    def test_cve_zero_mass_shares_one_percentile(self):
        # Every repo with cve == 0 must receive an identical cve_risk_pctl;
        # a repo carrying CVEs ranks strictly higher-risk than that block.
        metrics = [_metric(f"r/{i}", openssf_score=5.0, cve=0.0) for i in range(6)]
        metrics.append(_metric("r/withcve", openssf_score=5.0, cve=10.0))
        out = compute_security_classes(metrics)
        zero_pctls = {out[f"r/{i}"]["cve_risk_pctl"] for i in range(6)}
        assert len(zero_pctls) == 1
        assert out["r/withcve"]["cve_risk_pctl"] > zero_pctls.pop()

    def test_needs_both_axes_weak_for_class_a(self):
        # A repo terrible on only ONE axis (great score, many CVEs) must
        # NOT land in A — the geometric mean keeps it out of the worst
        # quartile.
        metrics = [_metric("r/mixed", openssf_score=10.0, cve=150.0)] + [
            _metric(f"r/{i}", openssf_score=10.0 - i * 0.5, cve=float(i))
            for i in range(1, 12)
        ]
        out = compute_security_classes(metrics)
        assert out["r/mixed"]["security_class"] != "A"

    def test_missing_openssf_score_yields_empty(self):
        metrics = [
            _metric("r/a", openssf_score=None, cve=3.0),
            _metric("r/b", openssf_score=5.0, cve=3.0),
            _metric("r/c", openssf_score=6.0, cve=4.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/a"]["security_class"] == ""
        assert out["r/a"]["openssf_risk_pctl"] == ""
        assert out["r/a"]["cve_risk_pctl"] == ""
        assert out["r/a"]["security_risk_percentile"] == ""

    def test_missing_cve_yields_empty(self):
        metrics = [
            _metric("r/a", openssf_score=5.0, cve=None),
            _metric("r/b", openssf_score=5.0, cve=3.0),
            _metric("r/c", openssf_score=6.0, cve=4.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/a"]["security_class"] == ""

    def test_equal_count_quartiles(self):
        from collections import Counter
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i * 0.2, cve=float(i))
            for i in range(20)
        ]
        out = compute_security_classes(metrics)
        counts = Counter(out[f"r/{i}"]["security_class"] for i in range(20))
        assert counts == {"A": 5, "B": 5, "C": 5, "D": 5}

    def test_composite_is_geometric_mean_of_the_two_pctls(self):
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i, cve=float(i))
            for i in range(1, 9)
        ]
        out = compute_security_classes(metrics)
        r = out["r/4"]
        expected = (r["openssf_risk_pctl"] * r["cve_risk_pctl"]) ** 0.5
        assert r["security_risk_percentile"] == pytest.approx(expected, abs=0.01)
