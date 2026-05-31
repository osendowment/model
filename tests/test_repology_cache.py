"""Tests for the Repology URL CSV cache (src/sources/cpp/fetch_repology_urls.py)."""

import csv

from src.sources.cpp.fetch_repology_urls import (
    DONE_STATUSES,
    load_cache,
    rows_for,
)


def test_rows_for_with_urls_are_all_ok():
    rows = rows_for("vue", [("https://github.com/vuejs/core", "github")], "ok", "2026-05-31")
    assert rows == [{
        "project": "vue", "candidate_url": "https://github.com/vuejs/core",
        "platform": "github", "status": "ok", "fetched_at": "2026-05-31",
    }]


def test_rows_for_empty_200_is_none_sentinel():
    [row] = rows_for("acl", [], "ok", "2026-05-31")
    assert row["candidate_url"] == "" and row["platform"] == ""
    assert row["status"] == "none"


def test_rows_for_404_is_not_found_sentinel():
    [row] = rows_for("nope", [], "not_found", "2026-05-31")
    assert row["status"] == "not_found"


def test_rows_for_network_failure_is_error_sentinel():
    [row] = rows_for("flaky", [], "error", "2026-05-31")
    assert row["status"] == "error"


def _write(path, header, *rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)


def test_load_cache_missing_file(tmp_path):
    rows, done = load_cache(tmp_path / "nope.csv")
    assert rows == {} and done == set()


def test_load_cache_legacy_rows_count_as_done(tmp_path):
    # Pre-cache file: no status/fetched_at columns, all real URLs.
    p = tmp_path / "project-urls.csv"
    _write(p, ["project", "candidate_url", "platform"],
           ["db", "https://github.com/x/db.git", "github"])
    rows, done = load_cache(p)
    assert done == {"db"}
    assert rows["db"][0]["status"] == "ok"


def test_load_cache_errors_are_retried_not_done(tmp_path):
    p = tmp_path / "project-urls.csv"
    _write(p, ["project", "candidate_url", "platform", "status", "fetched_at"],
           ["good", "https://github.com/a/b.git", "github", "ok", "2026-05-31"],
           ["empty", "", "", "none", "2026-05-31"],
           ["gone", "", "", "not_found", "2026-05-31"],
           ["flaky", "", "", "error", "2026-05-31"])
    _, done = load_cache(p)
    # ok / none / not_found are settled; error is NOT (must be retried).
    assert done == {"good", "empty", "gone"}
    assert "flaky" not in done


def test_done_statuses_excludes_error():
    assert "error" not in DONE_STATUSES
    assert {"ok", "none", "not_found"} <= DONE_STATUSES
