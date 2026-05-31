"""Tests for the narrow risk aggregator — component scores → overall score."""

import math

from src.pipeline.risk.aggregate_risk import overall_score, FIELDS, COMPONENTS


def test_risk_csv_is_narrow():
    assert FIELDS == ["repo", "repo_id", "concentration", "complexity",
                      "security", "funding", "workload", "score"]
    assert "visibility" not in COMPONENTS


def test_overall_score_geom_mean():
    assert overall_score([50, 50, 50]) == "50"
    # geom mean of 100 and 1 = 10 — one low component pulls the total down
    assert overall_score([100, 1]) == str(round(math.sqrt(100 * 1)))


def test_overall_score_floored_and_empty():
    assert overall_score([1, 1]) == "1"
    assert overall_score([]) == ""        # no component scores → blank
