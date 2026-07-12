"""Tests for src/risk/build_complexity.py."""


def test_scoreercentile_columns():
    from src.common.percentiles import add_percentiles
    rows = [
        {"loc_eoy": "2000000", "cyclomatic_max": "90"},
        {"loc_eoy": "5000", "cyclomatic_max": "8"},
        {"loc_eoy": "50000", "cyclomatic_max": "30"},
    ]
    add_percentiles(rows,
                    pctl_specs=[("loc_eoy", True), ("cyclomatic_max", True)],
                    composite_cols=["loc_eoy_p", "cyclomatic_max_p"],
                    dim_col="score")
    assert rows[0]["loc_eoy_p"] == 100.0      # biggest codebase = worst
    assert rows[0]["score"] == "100.00"


def test_per_year_shas_keeps_prewindow_dated_fallback(tmp_path):
    """A dormant repo's pre-window dated snapshot (e.g. 2020) survives — it is
    NOT clamped to the settings window. The index is keyed on the stable
    `repo_id`, not the (rename-prone) repo name.

    Regression for the dated-fallback rule: resolve_head records a dormant
    repo's snapshot under its real commit year so the complexity walk has an
    auditable dated snapshot, never an opaque HEAD.
    """
    import csv
    from pathlib import Path
    from src.risk.build_complexity import _per_year_shas

    p = Path(tmp_path) / "commits-years.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "repo", "repo_id", "year", "first_sha", "last_sha", "commits", "fetched_at"])
        w.writeheader()
        w.writerow({"repo": "dorm/repo", "repo_id": "111", "year": "2020", "last_sha": "deadbeef", "commits": "1"})
        w.writerow({"repo": "act/repo", "repo_id": "222", "year": "2025", "last_sha": "cafe", "commits": "40"})

    out = _per_year_shas(p)
    assert out["111"][2020] == "deadbeef"   # pre-window year kept (keyed by repo_id)
    assert sorted(out["111"], reverse=True)[0] == 2020
    assert out["222"][2025] == "cafe"


def test_per_year_shas_ignores_stale_head_rows(tmp_path):
    """A leftover legacy `year="HEAD"` row (from a previous version of
    resolve_head) is silently ignored, not bucketed as a fallback year — this
    module no longer has any concept of an undated snapshot."""
    import csv
    from pathlib import Path
    from src.risk.build_complexity import _per_year_shas

    p = Path(tmp_path) / "commits-years.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "repo", "repo_id", "year", "first_sha", "last_sha", "commits", "fetched_at"])
        w.writeheader()
        w.writerow({"repo": "head-only/repo", "repo_id": "999", "year": "HEAD", "last_sha": "deadbeef", "commits": "1"})

    out = _per_year_shas(p)
    assert "999" not in out


def test_build_scores_empty_tree_repos_as_measured_zeros(tmp_path, monkeypatch):
    """A repo with any code-bearing snapshot uses it — a bogus zero row must
    not shadow real code. A repo whose EVERY measured snapshot is loc=0 (an
    archived stub stripped to a README) takes the newest zero snapshot and
    scores at the floor instead of blanking the dimension; absent lizard
    metrics read as measured zeros (an empty tree has zero functions).

    Successor to the retired pipeline_health `_zero_loc_repos` exemption
    test: the coverage gate now demands 100% on complexity/workload, and
    this builder behaviour is what satisfies it.
    """
    import csv

    import src.risk.build_complexity as bc
    from src.common.repos import RepoEntry

    cy = tmp_path / "commits-years.csv"
    with open(cy, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "repo", "repo_id", "year", "first_sha", "last_sha", "commits", "fetched_at"])
        w.writeheader()
        for repo, rid, year, sha in [("own/grew", "gh/1", "2021", "g21"),
                                     ("own/grew", "gh/1", "2025", "g25"),
                                     ("own/stub", "gh/2", "2021", "s21"),
                                     ("own/stub", "gh/2", "2025", "s25")]:
            w.writerow({"repo": repo, "repo_id": rid, "year": year,
                        "last_sha": sha, "commits": "1"})

    scc = tmp_path / "scc.csv"
    with open(scc, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "repo_id", "commit_sha", "metric", "value", "checked_at"])
        w.writerow(["own/grew", "gh/1", "g21", "loc", "0", "t"])    # bogus early zero
        w.writerow(["own/grew", "gh/1", "g25", "loc", "500", "t"])  # real code, newest
        w.writerow(["own/stub", "gh/2", "s21", "loc", "0", "t"])    # empty at every
        w.writerow(["own/stub", "gh/2", "s25", "loc", "0", "t"])    # snapshot

    lizard = tmp_path / "lizard.csv"
    with open(lizard, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "repo_id", "commit_sha", "metric", "value", "checked_at"])
        w.writerow(["own/grew", "gh/1", "g25", "cyclomatic_max", "7", "t"])


    monkeypatch.setattr(bc, "COMMITS_YEARS_FILE", cy)
    monkeypatch.setattr(bc, "SCC_FILE", scc)
    monkeypatch.setattr(bc, "LIZARD_FILE", lizard)
    monkeypatch.setattr(bc, "load_top_repos", lambda: [
        RepoEntry(repo="own/grew", repo_id="gh/1"),
        RepoEntry(repo="own/stub", repo_id="gh/2"),
    ])

    rows = {r["repo"]: r for r in bc.build()}
    grew, stub = rows["own/grew"], rows["own/stub"]
    assert grew["loc_eoy"] == "500" and grew["loc_year"] == "2025"
    assert stub["loc_eoy"] == "0" and stub["loc_year"] == "2025"
    assert stub["cyclomatic_max"] == "0"
    assert stub["score"] != ""


def test_complexity_has_no_churn_or_hotspot_columns():
    """churn / hotspot are gone from complexity.csv. They fed no score
    (composite is loc_eoy_p + cyclomatic_max_p), were GitHub-only — so blank
    for most GitLab repos — and were derived from a churn.csv the pipeline
    never refreshed, making them stale as well as unused."""
    from src.risk.build_complexity import FIELDS
    assert not [c for c in FIELDS if "churn" in c or "hotspot" in c]
    # the axes that DO feed the score are still there
    assert {"loc_eoy_p", "cyclomatic_max_p"} <= set(FIELDS)
