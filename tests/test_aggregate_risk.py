"""Tests for the narrow risk aggregator — component scores → overall score."""

import math

from src.risk.aggregate_risk import (
    COMPONENTS,
    FIELDS,
    _funding_flags,
    overall_score,
)


def test_funding_flags_read(tmp_path):
    """intent/nonprofit are read per repo from funding.csv; missing → safe defaults."""
    f = tmp_path / "funding.csv"
    # Flags join on the stable repo_id, not the (renameable) name.
    f.write_text("repo,repo_id,intent,nonprofit\n"
                 "a/co,1,True,False\n"
                 "c/found,2,True,True\n"
                 "d/none,3,False,True\n")
    flags = _funding_flags(f)
    assert flags["1"] == {"intent": "True", "nonprofit": "False"}
    assert flags["3"] == {"intent": "False", "nonprofit": "True"}


def test_risk_csv_is_narrow():
    assert FIELDS == ["repo", "repo_id", "concentration", "complexity",
                      "security", "workload", "score", "intent", "nonprofit"]
    assert "funding" not in COMPONENTS


def test_overall_score_geom_mean():
    assert overall_score([50, 50, 50]) == "50"
    # geom mean of 100 and 1 = 10 — one low component pulls the total down
    assert overall_score([100, 1]) == str(round(math.sqrt(100 * 1)))


def test_overall_score_floored_and_empty():
    assert overall_score([1, 1]) == "1"
    assert overall_score([]) == ""        # no component scores → blank


def test_aggregate_requires_all_components(monkeypatch):
    """Completeness rule: the overall score is blank unless ALL four component
    scores are present — a repo missing any component gets no partial score."""
    from pathlib import Path

    from src.risk import aggregate_risk as ar

    class _Entry:
        def __init__(self, repo):
            self.repo, self.repo_id = repo, repo

    monkeypatch.setattr(ar, "load_top_repos",
                        lambda: [_Entry("o/full"), _Entry("o/partial")])
    monkeypatch.setattr(ar, "_funding_flags", lambda p: {})
    # `o/full` has all four; `o/partial` is missing workload.
    by_name = {
        "concentration": {"o/full": 50, "o/partial": 50},
        "complexity":    {"o/full": 50, "o/partial": 50},
        "security":      {"o/full": 50, "o/partial": 50},
        "workload":      {"o/full": 50},
    }
    monkeypatch.setattr(ar, "_scores_by_repo", lambda p: by_name[Path(p).stem])

    rows = {r["repo"]: r for r in ar.aggregate()}
    assert rows["o/full"]["score"] == "50"        # all present → scored
    assert rows["o/partial"]["workload"] == ""    # the missing input
    assert rows["o/partial"]["score"] == ""       # …so no overall score
