"""Tests for src/build_results.py — the final cross-stage rollup (all top repos)."""
import csv

from src import build_results as br


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _patch(monkeypatch, elig, val, risk, repos=None):
    monkeypatch.setattr(br, "ELIGIBILITY_FILE", elig)
    monkeypatch.setattr(br, "VALUE_FILE", val)
    monkeypatch.setattr(br, "RISK_FILE", risk)
    if repos is not None:
        monkeypatch.setattr(br, "GITHUB_REPOS_FILE", repos)


def test_includes_every_top_repo_and_joins_on_repo_id(tmp_path, monkeypatch):
    """Every eligibility row survives (ineligible NOT dropped); value/risk/
    language all join on the stable `repo_id`."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    repos = tmp_path / "repos.csv"
    _write(elig, ["repo", "repo_id", "oss", "intent", "nonprofit", "active", "eligible"],
           [["a/keep", "gh/1", "True", "True", "True", "True", "True"],
            ["b/drop", "gh/2", "True", "False", "True", "True", "False"]])
    _write(val, ["repo", "repo_id", "top_eco", "openssf_crit", "eco_crit",
                 "top_eco_pct", "value_score"],
           [["a/keep", "gh/1", "npm", "0.71", "1", "55.5", "72.3"],
            ["b/drop", "gh/2", "pypi", "0.2", "0", "10.0", "8.1"]])
    _write(risk, ["repo", "repo_id", "concentration", "complexity", "security",
                  "workload", "risk_score"],
           [["a/keep", "gh/1", "10", "20", "30", "40", "80"],
            ["b/drop", "gh/2", "1", "2", "3", "4", "20"]])
    _write(repos, ["repo_id", "language"], [["gh/1", "TypeScript"], ["gh/2", "Python"]])
    _patch(monkeypatch, elig, val, risk, repos)

    rows = br.build()
    assert {r["repo"] for r in rows} == {"a/keep", "b/drop"}   # nothing dropped
    keep = next(r for r in rows if r["repo"] == "a/keep")
    assert keep["repo_id"] == "gh/1"
    assert keep["language"] == "typescript"        # lowercased
    assert keep["ecosystem"] == "npm"
    assert keep["openssf_crit"] == "0.71"
    assert keep["eco_crit"] == "1"
    assert keep["top_eco_pct"] == "55.5"
    assert keep["value_score"] == "72.3"
    assert keep["concentration"] == "10" and keep["workload"] == "40"
    assert keep["risk_score"] == "80"
    assert keep["eligible"] == "True"
    assert list(keep.keys()) == br.FIELDS


def test_score_rescaled_and_priority_dense_rank_over_eligible(tmp_path, monkeypatch):
    """score = value_score*risk_score rescaled so the max row = 100 (over ALL
    rows with both inputs); priority = dense rank by score desc, eligible only."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"],
           [["a/top", "gh/1", "True"], ["b/mid", "gh/2", "True"],
            ["c/inelig", "gh/3", "False"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["a/top", "gh/1", "npm", "10"], ["b/mid", "gh/2", "npm", "5"],
            ["c/inelig", "gh/3", "npm", "20"]])
    _write(risk, ["repo", "repo_id", "risk_score"],
           [["a/top", "gh/1", "10"], ["b/mid", "gh/2", "10"],
            ["c/inelig", "gh/3", "10"]])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    by = {r["repo"]: r for r in rows}
    # raw products: a=100, b=50, c=200 → max=200 → scores 50.00 / 25.00 / 100.00
    assert by["c/inelig"]["score"] == "100.00"
    assert by["a/top"]["score"] == "50.00"
    assert by["b/mid"]["score"] == "25.00"
    # priority only over eligible rows: a (50) rank1, b (25) rank2; c blank.
    assert by["a/top"]["priority"] == "1"
    assert by["b/mid"]["priority"] == "2"
    assert by["c/inelig"]["priority"] == ""
    # row order: scored desc → c, a, b
    assert [r["repo"] for r in rows] == ["c/inelig", "a/top", "b/mid"]


def test_missing_risk_leaves_score_blank(tmp_path, monkeypatch):
    """An eligible repo with no risk.csv row keeps blank risk_score AND blank
    score (honest, not a fake 0) yet still appears in the table."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["x/norisk", "gh/9", "True"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["x/norisk", "gh/9", "crates", "63.7"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [])
    _patch(monkeypatch, elig, val, risk)

    rows = br.build()
    assert len(rows) == 1
    assert rows[0]["risk_score"] == ""
    assert rows[0]["value_score"] == "63.7"
    assert rows[0]["score"] == ""
    assert rows[0]["priority"] == ""


def test_missing_language_row_leaves_blank(tmp_path, monkeypatch):
    """A repo absent from github/repos.csv (e.g. GitLab) keeps a blank language
    rather than dropping the row."""
    elig = tmp_path / "eligibility.csv"
    val = tmp_path / "value.csv"
    risk = tmp_path / "risk.csv"
    repos = tmp_path / "repos.csv"
    _write(elig, ["repo", "repo_id", "eligible"], [["gl/thing", "gl/x-1", "True"]])
    _write(val, ["repo", "repo_id", "top_eco", "value_score"],
           [["gl/thing", "gl/x-1", "npm", "5"]])
    _write(risk, ["repo", "repo_id", "risk_score"], [["gl/thing", "gl/x-1", "5"]])
    _write(repos, ["repo_id", "language"], [])
    _patch(monkeypatch, elig, val, risk, repos)

    rows = br.build()
    assert rows[0]["language"] == ""
