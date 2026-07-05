"""Regression tests: risk-source fetchers must never strip the backfilled
`repo_id` column on rewrite.

Builders join the long source files by the stable repo_id
(`load_rows_by_id` / `_load_long_grouped`), so a fetcher whose FIELDS list
predates the column silently blanks an entire risk dimension on its next
incremental run — this happened once with contributor-commits.csv,
commits-years.csv, and github/contributor-commits.csv simultaneously.
"""

import csv


def test_commits_years_write_round_trips_and_stamps_repo_id(tmp_path, monkeypatch):
    from src.sources.git import commits_years as cy
    monkeypatch.setattr(cy, "_repo_id_map", lambda: {"b/b": "222"})
    path = tmp_path / "commits-years.csv"
    data = {
        ("a/a", "2025"): {"repo_id": "111", "first_sha": "f", "last_sha": "l",
                          "commits": "3", "fetched_at": "t"},
        ("b/b", "2025"): {"first_sha": "f2", "last_sha": "l2",
                          "commits": "1", "fetched_at": "t"},  # no id: resolve
    }
    cy.write_sha_data(str(path), data)
    rows = {r["repo"]: r for r in csv.DictReader(open(path))}
    assert rows["a/a"]["repo_id"] == "gh/111"  # kept from the row (gh/-normalised)
    assert rows["b/b"]["repo_id"] == "gh/222"  # stamped from the map (gh/-normalised)
    # and the loader carries it back (full round trip, e.g. via resolve_head)
    again = cy.load_sha_data(str(path))
    assert again[("a/a", "2025")]["repo_id"] == "gh/111"


def test_git_contributors_long_rows_carry_and_heal_repo_id(tmp_path):
    from src.sources.git.contributors import LONG_FIELDS, _write_long
    assert "repo_id" in LONG_FIELDS
    path = tmp_path / "contributor-commits.csv"
    long_by_repo = {
        "a/a": [{"repo": "a/a", "repo_id": "111", "author_name": "x",
                 "author_email": "x@x", "year": "2025", "commits": "2"}],
        "b/b": [{"repo": "b/b", "author_name": "y",  # legacy row, no id
                 "author_email": "y@y", "year": "2025", "commits": "1"}],
    }
    _write_long(path, long_by_repo, repo_ids={"b/b": "222"})
    rows = {r["repo"]: r for r in csv.DictReader(open(path))}
    assert rows["a/a"]["repo_id"] == "111"
    assert rows["b/b"]["repo_id"] == "222"     # healed, not dropped


def test_github_contrib_upsert_stamps_and_heals_repo_id(tmp_path, monkeypatch):
    from src.sources.github import batch_runner as br
    contrib = tmp_path / "contributor-commits.csv"
    status = tmp_path / "contributor-commits.status.csv"
    # legacy long file: pre-repo_id schema
    contrib.write_text(
        "repo,login,contributions,account_type\n"
        "old/repo,alice,10,User\n"
    )
    monkeypatch.setattr(br, "GH_CONTRIB_FILE", str(contrib))
    monkeypatch.setattr(br, "GH_CONTRIB_STATUS_FILE", str(status))
    monkeypatch.setattr(br, "_load_repo_id_map",
                        lambda: {"old/repo": "111", "new/repo": "222"})
    br._upsert_contributor_commits([("new/repo", [
        {"login": "Bob", "contributions": 5, "type": "User"},
    ])])
    rows = {r["repo"]: r for r in csv.DictReader(open(contrib))}
    assert rows["new/repo"]["repo_id"] == "222"   # stamped on new rows
    assert rows["old/repo"]["repo_id"] == "111"   # healed on round-trip
