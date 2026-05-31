"""Regression tests for src/sources/git/fetch_scc.py skip logic."""

import csv

from src.sources.git.fetch_scc import SCC_METRICS, _already_done

_HEADER = ["repo", "repo_id", "commit_sha", "metric", "value", "checked_at"]


def _write_long(path, repo, sha, value_for):
    """Write a long-format scc.csv with one (repo, sha) snapshot."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_HEADER)
        w.writeheader()
        for m in SCC_METRICS:
            w.writerow({"repo": repo, "repo_id": "1", "commit_sha": sha,
                        "metric": m, "value": value_for(m),
                        "checked_at": "2025-01-01T00:00:00Z"})


def test_already_done_skips_complete_nonzero_snapshot(tmp_path):
    p = tmp_path / "scc.csv"
    _write_long(p, "o/good", "abc", lambda m: "100" if m == "loc" else "5")
    assert _already_done(p, {"o/good": "abc"}) == {"o/good"}


def test_already_done_reanalyses_all_zero_snapshot(tmp_path):
    """An all-zero snapshot is a failed/empty checkout — must NOT be skipped."""
    p = tmp_path / "scc.csv"
    _write_long(p, "o/zero", "abc", lambda m: "0")
    assert _already_done(p, {"o/zero": "abc"}) == set()
