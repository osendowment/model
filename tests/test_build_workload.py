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

    repos = [types.SimpleNamespace(repo="o/present", repo_id="o/present"),
             types.SimpleNamespace(repo="o/absent", repo_id="o/absent")]
    issues = {
        "opened_issues": {"o/present": {y: 0 for y in bw.YEARS}},
        "closed_issues": {"o/present": {y: 0 for y in bw.YEARS}},
    }
    monkeypatch.setattr(bw, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bw, "load_rows_by_id", lambda *a, **k: {})
    monkeypatch.setattr(bw, "_load_commits_years", lambda: {})
    monkeypatch.setattr(bw, "_load_openssf_maintained", lambda: {})
    monkeypatch.setattr(bw, "_load_issues_long", lambda path: issues)
    # AC, loc, cve present for both so the per-AC guard stays open.
    monkeypatch.setattr(bw, "load_column_by_id",
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


def test_issue_sums_windowed_to_years(monkeypatch):
    """*_5y issue figures must sum ONLY the YEARS window, not stray years.

    Regression: op_5y/cl_5y summed every year key in the per-repo dict, so a
    wider fetch (out-of-window rows in issues.csv) leaked into issues_opened_5y /
    issues_closed_5y / net_new_issues_5y while the OLS slopes — which iterate
    `for y in YEARS` — correctly ignored them. The two must agree.
    """
    import types
    from src.risk import build_workload as bw

    repos = [types.SimpleNamespace(repo="o/r", repo_id="o/r")]
    in_open = {y: i + 1 for i, y in enumerate(bw.YEARS)}   # 1..5 -> sum 15
    in_close = {y: 1 for y in bw.YEARS}                    # sum 5
    # Inject out-of-window years that must NOT be counted.
    issues = {
        "opened_issues": {"o/r": {bw.YEARS[0] - 2: 1000, **in_open}},
        "closed_issues": {"o/r": {bw.YEARS[-1] + 1: 500, **in_close}},
    }
    monkeypatch.setattr(bw, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bw, "load_rows_by_id", lambda *a, **k: {})
    monkeypatch.setattr(bw, "_load_commits_years", lambda: {})
    monkeypatch.setattr(bw, "_load_openssf_maintained", lambda: {})
    monkeypatch.setattr(bw, "_load_issues_long", lambda path: issues)
    monkeypatch.setattr(bw, "load_column_by_id", lambda path, col: {"o/r": "5"})

    row = {r["repo"]: r for r in bw.build()}["o/r"]
    assert row["issues_opened_5y"] == 15   # not 1015
    assert row["issues_closed_5y"] == 5    # not 505
    assert row["net_new_issues_5y"] == 10  # not 510


def test_empty_nni_neutral_filled_only_when_loc_and_cve_present(monkeypatch):
    """Empty nni_per_ac is neutral-filled to 50 ONLY when the row is otherwise
    scorable (LOC and CVE present); otherwise it stays blank (no lone 50).

    - r0..r2: issues fetched -> real nni percentile (not 50).
    - r3: no issues, but LOC+CVE present -> nni_per_ac_p=50, score present.
    - r4: no issues AND no CVE -> not filled (blank), no score.
    """
    import types
    from src.risk import build_workload as bw

    repos = [types.SimpleNamespace(repo=f"o/r{i}", repo_id=f"o/r{i}") for i in range(5)]
    cols = {
        "loc_eoy": {f"o/r{i}": str(100 * (i + 1)) for i in range(5)},       # all
        "cve_count_5y": {f"o/r{i}": str(i + 1) for i in range(4)},          # r0..r3
        "active_contributors_git_5y": {f"o/r{i}": "2" for i in range(5)},   # all
    }
    issues = {
        "opened_issues": {f"o/r{i}": {y: i for y in bw.YEARS} for i in range(3)},
        "closed_issues": {f"o/r{i}": {y: 0 for y in bw.YEARS} for i in range(3)},
    }
    monkeypatch.setattr(bw, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bw, "load_rows_by_id", lambda *a, **k: {})
    monkeypatch.setattr(bw, "_load_commits_years", lambda: {})
    monkeypatch.setattr(bw, "_load_openssf_maintained", lambda: {})
    monkeypatch.setattr(bw, "_load_issues_long", lambda path: issues)
    monkeypatch.setattr(bw, "load_column_by_id", lambda path, col: cols[col])

    rows = {r["repo"]: r for r in bw.build()}

    # r3: scorable (LOC+CVE present) but no issues -> filled + scored.
    assert rows["o/r3"]["nni_per_ac"] == ""
    assert rows["o/r3"]["nni_per_ac_p"] == 50
    assert rows["o/r3"]["score"] != ""

    # r4: CVE missing too -> NOT filled, stays blank, no score (no lone 50).
    assert rows["o/r4"]["nni_per_ac"] == ""
    assert rows["o/r4"]["nni_per_ac_p"] == ""
    assert rows["o/r4"]["score"] == ""

    # r0: real issues -> keeps its computed percentile, not 50.
    assert rows["o/r0"]["nni_per_ac"] != ""
    assert rows["o/r0"]["nni_per_ac_p"] != 50


def test_dormant_repo_scored_with_ac_one(monkeypatch):
    """A zero-active-contributor repo (AC=0) is flagged `dormant=1` and scored
    with AC=1 (per-AC ratio = the whole raw burden) instead of being left blank;
    a live repo (AC>0) is `dormant=0` and divides by its real AC."""
    import types
    from src.risk import build_workload as bw

    repos = [types.SimpleNamespace(repo="o/dormant", repo_id="o/dormant"),
             types.SimpleNamespace(repo="o/live", repo_id="o/live")]
    cols = {
        "loc_eoy": {"o/dormant": "1000", "o/live": "1000"},
        "cve_count_5y": {"o/dormant": "4", "o/live": "4"},
        "active_contributors_git_5y": {"o/dormant": "0", "o/live": "2"},
    }
    monkeypatch.setattr(bw, "load_top_repos", lambda: repos)
    monkeypatch.setattr(bw, "load_rows_by_id", lambda *a, **k: {})
    monkeypatch.setattr(bw, "_load_commits_years", lambda: {})
    monkeypatch.setattr(bw, "_load_openssf_maintained", lambda: {})
    monkeypatch.setattr(bw, "_load_issues_long",
                        lambda path: {"opened_issues": {}, "closed_issues": {}})
    monkeypatch.setattr(bw, "load_column_by_id", lambda path, col: cols[col])

    rows = {r["repo"]: r for r in bw.build()}

    # dormant: AC=0 -> dormant=1, divided by 1, so per-AC = raw burden, scored.
    assert rows["o/dormant"]["dormant"] == "1"
    assert rows["o/dormant"]["loc_per_ac"] == 1000.0
    assert rows["o/dormant"]["cve_per_ac"] == 4.0
    assert rows["o/dormant"]["score"] != ""
    # live: AC=2 -> dormant=0, divided by 2.
    assert rows["o/live"]["dormant"] == "0"
    assert rows["o/live"]["loc_per_ac"] == 500.0
    assert rows["o/live"]["score"] != ""
