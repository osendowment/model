"""Tests for src/sources/github/fetch_churn.py CSV writing."""

import csv


def _result(repo):
    from src.sources.github.fetch_churn import ChurnResult
    return ChurnResult(repo=repo, commits=5, added=100, deleted=50,
                       files_count=3)


def test_write_csv_round_trips_repo_id_column(tmp_path):
    """Regression: churn.csv on disk carries a backfilled `repo_id` column.
    The writer must (a) not crash on existing rows that have it — the old
    fieldnames list raised ValueError mid-write and TRUNCATED the file —
    and (b) keep the column populated, since builders join churn by repo_id.
    """
    from src.sources.github.fetch_churn import _write_csv
    out = tmp_path / "churn.csv"
    existing = {
        "old/repo": {"repo": "old/repo", "repo_id": "111",
                     "analyzed_through_year": "2025", "commits_5y_examined": "9",
                     "churn_5y_added": "1", "churn_5y_deleted": "1",
                     "churn_5y_total": "2", "churn_files_count": "1",
                     "top_file_path": "a.py", "top_file_churn": "2",
                     "elapsed_s": "0.1", "fetched_at": "2026-01-01T00:00:00Z"},
    }
    _write_csv(str(out), [_result("new/repo")], existing,
               repo_ids={"new/repo": "222"})
    rows = {r["repo"]: r for r in csv.DictReader(open(out))}
    assert rows["old/repo"]["repo_id"] == "111"   # preserved, not dropped
    assert rows["new/repo"]["repo_id"] == "222"   # stamped on new rows


def test_write_csv_backfills_blank_repo_id_and_survives_extra_columns(tmp_path):
    """Existing rows with a blank repo_id get backfilled when resolvable;
    unknown extra columns in old rows are ignored instead of crashing."""
    from src.sources.github.fetch_churn import _write_csv
    out = tmp_path / "churn.csv"
    existing = {
        "a/a": {"repo": "a/a", "repo_id": "", "churn_5y_total": "5",
                "legacy_extra_col": "x"},
        "b/b": {"repo": "b/b", "churn_5y_total": "3"},  # no repo_id key at all
    }
    _write_csv(str(out), [], existing, repo_ids={"a/a": "42"})
    rows = {r["repo"]: r for r in csv.DictReader(open(out))}
    assert rows["a/a"]["repo_id"] == "42"         # backfilled
    assert rows["b/b"]["repo_id"] == ""           # unresolvable stays blank
    assert "legacy_extra_col" not in rows["a/a"]  # ignored, no ValueError
