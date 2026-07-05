"""repo_id + git_url columns in the GitHub commits-years anchor (Task 9)."""
from __future__ import annotations

import csv

from src.sources.git import commits_years as cy


def test_sha_fields_include_repo_id_and_git_url():
    assert cy.SHA_FIELDS[:3] == ["repo", "repo_id", "git_url"]


def test_write_emits_prefixed_repo_id_and_git_url(tmp_path):
    out = tmp_path / "commits-years.csv"
    data = {("npm/cli", "2024"): {"first_sha": "a", "last_sha": "b",
                                  "commits": "3", "fetched_at": "2026-01-01T00:00:00Z"}}
    cy.write_sha_data(str(out), data, repo_ids={"npm/cli": "12345"})
    row = next(csv.DictReader(open(out)))
    assert row["repo_id"] == "gh/12345"
    assert row["git_url"] == "https://github.com/npm/cli.git"
    assert row["last_sha"] == "b"


def test_write_blank_repo_id_when_unknown(tmp_path):
    out = tmp_path / "c.csv"
    cy.write_sha_data(str(out), {("a/b", "2024"): {"last_sha": "x"}}, repo_ids={})
    row = next(csv.DictReader(open(out)))
    assert row["repo_id"] == ""              # unknown → blank, never "gh/"
    assert row["git_url"] == "https://github.com/a/b.git"


def test_backfill_adds_columns_preserving_rows(tmp_path):
    legacy = tmp_path / "commits-years.csv"
    with open(legacy, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "year", "first_sha", "last_sha",
                                          "commits", "fetched_at"])
        w.writeheader()
        w.writerow({"repo": "npm/cli", "year": "2024", "first_sha": "a",
                    "last_sha": "b", "commits": "3", "fetched_at": "2026-01-01T00:00:00Z"})
    n = cy.backfill(str(legacy), repo_ids={"npm/cli": "999"})
    rows = list(csv.DictReader(open(legacy)))
    assert n == 1 and len(rows) == 1
    assert rows[0]["repo_id"] == "gh/999"
    assert rows[0]["git_url"] == "https://github.com/npm/cli.git"
    assert rows[0]["last_sha"] == "b"        # original data preserved
