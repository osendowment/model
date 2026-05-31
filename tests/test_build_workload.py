"""Tests for src/risk/build_workload.py — workload percentile logic."""


def test_scoreercentile_columns():
    from src.common.percentiles import add_percentiles
    rows = [
        {"loc_per_ac": "100000", "cve_per_ac": "5", "nni_per_ac": "50"},
        {"loc_per_ac": "100", "cve_per_ac": "0", "nni_per_ac": "1"},
        {"loc_per_ac": "5000", "cve_per_ac": "1", "nni_per_ac": "10"},
    ]
    add_percentiles(rows,
                    pctl_specs=[("loc_per_ac", True), ("cve_per_ac", True),
                                ("nni_per_ac", True)],
                    composite_cols=["loc_per_ac_p", "cve_per_ac_p", "nni_per_ac_p"],
                    dim_col="score")
    assert rows[0]["score"] == 100.0   # worst on all three axes


def test_workload_blanks_issues_when_repo_not_fetched(monkeypatch):
    """A repo absent from issues.csv was never fetched -> issue columns blank.

    Regression: previously `opened.get(repo, {})` -> sum() == 0, so a repo
    that was never queried got issues_opened_5y=0 / net_new_issues_5y=0,
    feeding a fake 0 into nni_per_ac. A repo PRESENT with all-zero counts is
    a genuine 0 and must stay 0.
    """
    import types
    from src.risk import build_workload as bw

    repos = [types.SimpleNamespace(repo="o/present", repo_id="1"),
             types.SimpleNamespace(repo="o/absent", repo_id="2")]
    issues = {
        "opened_issues": {"o/present": {y: 0 for y in bw.YEARS}},
        "closed_issues": {"o/present": {y: 0 for y in bw.YEARS}},
    }
    monkeypatch.setattr(bw, "load_risk_repos", lambda: repos)
    monkeypatch.setattr(bw, "load_rows_by_repo", lambda *a, **k: {})
    monkeypatch.setattr(bw, "_load_commits_years", lambda: {})
    monkeypatch.setattr(bw, "_load_openssf_maintained", lambda: {})
    monkeypatch.setattr(bw, "_load_issues_long", lambda path: issues)
    # AC, loc, cve present for both so the per-AC guard stays open.
    monkeypatch.setattr(bw, "load_column_by_repo",
                        lambda path, col: {"o/present": "5", "o/absent": "5"})

    rows = {r["repo"]: r for r in bw.build()}
    present, absent = rows["o/present"], rows["o/absent"]

    # Present + fetched -> genuine zeros.
    assert present["issues_opened_5y"] == 0
    assert present["net_new_issues_5y"] == 0
    assert present["nni_per_ac"] == 0.0

    # Absent / never fetched -> blank, NOT 0.
    assert absent["issues_opened_5y"] == ""
    assert absent["issues_closed_5y"] == ""
    assert absent["net_new_issues_5y"] == ""
    assert absent["issue_close_ratio"] == ""
    assert absent["slope_opened"] == ""
    assert absent["nni_per_ac"] == ""
