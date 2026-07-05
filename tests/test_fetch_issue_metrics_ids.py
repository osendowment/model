"""Regression tests: issues.csv rewrites must never wipe repo_ids.

_write_long re-derives every row's repo_id from its map on each FULL-file
rewrite. When the map came only from repos.csv (which lags renames), any
in-scope repo missing there had its ids wiped across ALL rows — dropping
its issue data from the id-keyed workload join. The map is now layered:
ids already on disk ∪ repos.csv ∪ value.csv.
"""

import csv


def test_disk_ids_survive_rewrite_even_if_maps_forget_the_slug(tmp_path):
    from src.sources.github.fetch_issue_metrics import (
        _load_disk_repo_ids,
        _load_long,
        _write_long,
    )
    path = str(tmp_path / "issues.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "repo_id", "year", "metric", "value"])
        w.writeheader()
        w.writerow({"repo": "renamed/away", "repo_id": "111", "year": "2024",
                    "metric": "opened_issues", "value": "7"})

    # the caller's layered map: disk ids first, overlays know nothing
    repo_ids = {**_load_disk_repo_ids(path)}
    rows_by_state, _, cell_dates = _load_long(path)
    _write_long(path, rows_by_state, repo_ids, cell_dates=cell_dates)

    rows = list(csv.DictReader(open(path)))
    assert rows[0]["repo_id"] == "111"   # preserved, not wiped


def test_write_long_is_atomic_no_tmp_left_behind(tmp_path):
    from src.sources.github.fetch_issue_metrics import _write_long
    path = str(tmp_path / "issues.csv")
    n = _write_long(path, {"opened": {"a/a": {"2024": "3"}}, "closed": {}},
                    {"a/a": "42"},
                    cell_dates={("opened", "a/a", "2024"): "2026-07-05T00:00:00+00:00"})
    assert n == 1
    rows = list(csv.DictReader(open(path)))
    assert rows[0]["repo_id"] == "42"
    assert rows[0]["fetched_at"] == "2026-07-05T00:00:00+00:00"
    assert not (tmp_path / "issues.csv.tmp").exists()


def test_cell_fetched_at_round_trips(tmp_path):
    """A cell's fetched_at survives load->write; undated legacy cells stay
    blank (honest 'pre-tracking', never invented)."""
    from src.sources.github.fetch_issue_metrics import _load_long, _write_long
    path = str(tmp_path / "issues.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "repo_id", "year", "metric",
                                          "value", "fetched_at"])
        w.writeheader()
        w.writerow({"repo": "a/a", "repo_id": "1", "year": "2024",
                    "metric": "opened_issues", "value": "5",
                    "fetched_at": "2026-01-01T00:00:00+00:00"})
        w.writerow({"repo": "a/a", "repo_id": "1", "year": "2023",
                    "metric": "opened_issues", "value": "2", "fetched_at": ""})
    rows_by_state, _, cell_dates = _load_long(path)
    _write_long(path, rows_by_state, {"a/a": "1"}, cell_dates=cell_dates)
    out = {(r["year"]): r["fetched_at"] for r in csv.DictReader(open(path))}
    assert out["2024"] == "2026-01-01T00:00:00+00:00"
    assert out["2023"] == ""


def test_load_value_repo_ids_canonical_form_and_filters_platform(tmp_path):
    from src.common.repos import load_value_repo_ids
    f = tmp_path / "value.csv"
    f.write_text(
        'repo,platform,repo_id\n'
        'react/react,github,gh/10270250\n'
        'gl/thing,gitlab,gl/9\n'
    )
    assert load_value_repo_ids(str(f)) == {"react/react": "gh/10270250"}
