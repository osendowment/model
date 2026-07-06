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
    assert rows[0]["score"] == 100.0


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
