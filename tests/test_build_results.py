"""Tests for src/build_results.py — the final fundable-candidate table."""
import csv

from src import build_results as br


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _patch(monkeypatch, elig, val, risk):
    monkeypatch.setattr(br, "ELIGIBILITY_FILE", elig)
    monkeypatch.setattr(br, "VALUE_FILE", val)
    monkeypatch.setattr(br, "RISK_FILE", risk)


def test_filters_eligible_and_joins(tmp_path, monkeypatch):
    """Only eligible==True rows are kept; value_score/ecosystem join on the repo
    slug (value.csv), risk_score joins on the stable repo_id (risk.csv)."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "oss", "intent", "nonprofit", "active", "eligible"],
           [["a/keep", "1", "True", "True", "True", "True", "True"],
            ["b/drop", "2", "True", "False", "True", "True", "False"]])
    # value.csv keys on the `repo` slug (its repo_id is the gh/<n> platform form).
    _write(val, ["repo", "platform", "repo_id", "top_eco", "top_eco_pct"],
           [["a/keep", "github", "gh/1", "npm", "55.5"],
            ["b/drop", "github", "gh/2", "pypi", "10.0"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [["a/keep", "1", "80"], ["b/drop", "2", "20"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    assert [r["repo"] for r in rows] == ["a/keep"]          # ineligible dropped
    r = rows[0]
    assert r["repo_id"] == "1"
    assert r["ecosystem"] == "npm"
    assert r["value_score"] == "55.5"
    assert r["risk_score"] == "80"
    assert list(r.keys()) == br.FIELDS


def test_sorted_by_risk_then_value_desc(tmp_path, monkeypatch):
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"],
           [["a/lo", "1", "True"], ["b/hi", "2", "True"], ["c/mid", "3", "True"]])
    _write(val, ["repo", "top_eco", "top_eco_pct"],
           [["a/lo", "npm", "1"], ["b/hi", "npm", "2"], ["c/mid", "npm", "9"]])
    _write(risk, ["repo", "repo_id", "risk_score"],
           [["a/lo", "1", "30"], ["b/hi", "2", "90"], ["c/mid", "3", "90"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    # risk desc; ties broken by value desc (c/mid 9 > b/hi 2).
    assert [r["repo"] for r in rows] == ["c/mid", "b/hi", "a/lo"]


def test_missing_risk_score_left_blank(tmp_path, monkeypatch):
    """An eligible repo with no risk.csv row keeps a blank risk_score (honest,
    not a fake 0) and still appears."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["x/norisk", "9", "True"]])
    _write(val, ["repo", "top_eco", "top_eco_pct"], [["x/norisk", "crates", "42"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    assert len(rows) == 1
    assert rows[0]["risk_score"] == ""
    assert rows[0]["value_score"] == "42"
