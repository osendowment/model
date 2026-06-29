"""Tests for the narrow risk aggregator — component scores → overall score."""

import math

from src.risk.aggregate_risk import (
    COMPONENTS,
    FIELDS,
    _company_backed_repos,
    overall_score,
)


def test_company_backed_repos_detected(tmp_path):
    """A repo whose funding host_type OR owner_type is 'company' is flagged for
    exclusion from the final risk ranking; nonprofit/none are not."""
    f = tmp_path / "funding.csv"
    f.write_text("repo,host_type,owner_type\n"
                 "a/co,,company\n"
                 "b/host-co,company,\n"
                 "c/found,nonprofit,\n"
                 "d/none,,\n")
    assert _company_backed_repos(f) == {"a/co", "b/host-co"}


def test_risk_csv_is_narrow():
    assert FIELDS == ["repo", "repo_id", "concentration", "complexity",
                      "security", "funding", "workload", "score",
                      "dims_scored", "concentration_imputed"]
    assert "visibility" not in COMPONENTS


def test_overall_score_geom_mean():
    assert overall_score([50, 50, 50]) == "50"
    # geom mean of 100 and 1 = 10 — one low component pulls the total down
    assert overall_score([100, 1]) == str(round(math.sqrt(100 * 1)))


def test_overall_score_floored_and_empty():
    assert overall_score([1, 1]) == "1"
    assert overall_score([]) == ""        # no component scores → blank


def test_aggregate_requires_all_components(monkeypatch):
    """Completeness rule: the overall score is blank unless ALL five component
    scores are present — a repo missing any component gets no partial score."""
    from pathlib import Path

    from src.risk import aggregate_risk as ar

    class _Entry:
        def __init__(self, repo):
            self.repo, self.repo_id = repo, repo

    monkeypatch.setattr(ar, "load_top_repos",
                        lambda: [_Entry("o/full"), _Entry("o/partial")])
    # `o/full` has all five; `o/partial` is missing workload.
    by_name = {
        "concentration": {"o/full": 50, "o/partial": 50},
        "complexity":    {"o/full": 50, "o/partial": 50},
        "security":      {"o/full": 50, "o/partial": 50},
        "funding":       {"o/full": 50, "o/partial": 50},
        "workload":      {"o/full": 50},
    }
    monkeypatch.setattr(ar, "_scores_by_repo", lambda p: by_name[Path(p).stem])

    rows = {r["repo"]: r for r in ar.aggregate()}
    assert rows["o/full"]["score"] == "50"        # all present → scored
    assert rows["o/partial"]["workload"] == ""    # the missing input
    assert rows["o/partial"]["score"] == ""       # …so no overall score
