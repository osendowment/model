"""Tests for src/value/verify_git_urls.py.

Focus: `verify_urls_in_aggregates` reading back `data/sources/github/repos.csv`
to annotate each value-data row with the GitHub repo's stable numeric id
(`gh_repo_id`) and its *current* name (renamed repos are canonicalised).

The GitHub fetch and the `git ls-remote` pass are both stubbed, so no
network is touched — only the read-back + annotation logic is exercised.
"""

from __future__ import annotations

import csv
from pathlib import Path

import src.sources.github.fetch_repo_owner_data as gh_mod
import src.value.verify_git_urls as vgu
from src.value.verify_git_urls import verify_urls_in_aggregates


def _write_repos_csv(path: Path, rows: list[list[str]]) -> None:
    """Materialise a minimal data/sources/github/repos.csv (the columns the
    read-back actually consults)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["repo", "valid", "repo_id", "full_name"])
        w.writerows(rows)


def _patch(tmp_path, monkeypatch, repo_rows):
    """Point the read-back at a fake repos.csv and stub every network call."""
    repos_csv = tmp_path / "repos.csv"
    _write_repos_csv(repos_csv, repo_rows)
    monkeypatch.setattr(gh_mod, "REPOS_OUT", repos_csv)
    monkeypatch.setattr(gh_mod, "fetch_and_persist", lambda **kw: {})
    monkeypatch.setattr(vgu, "_verify_non_github", lambda urls, **kw: {})


# ── gh_repo_id ───────────────────────────────────────────────────────────────

class TestGhRepoId:
    def test_populates_id_for_valid_repo(self, tmp_path, monkeypatch):
        _patch(tmp_path, monkeypatch,
               [["facebook/react", "True", "10270250", "facebook/react"]])
        aggs = [{"github_repo": "facebook/react", "git_url": "",
                 "top_eco_pkg": "react"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0].get("gh_repo_id") == "10270250"

    def test_invalid_repo_has_blank_id(self, tmp_path, monkeypatch):
        # A 404'd repo has no numeric id.
        _patch(tmp_path, monkeypatch, [["dead/repo", "False", "", ""]])
        aggs = [{"github_repo": "dead/repo", "git_url": "", "top_eco_pkg": "p"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0].get("gh_repo_id") == ""

    def test_no_github_repo_has_blank_id(self, tmp_path, monkeypatch):
        _patch(tmp_path, monkeypatch, [])
        aggs = [{"github_repo": "", "git_url": "", "top_eco_pkg": "p"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0].get("gh_repo_id") == ""


# ── github_repo canonicalisation ─────────────────────────────────────────────

class TestGithubRepoCurrentName:
    def test_rewrites_to_current_name_when_renamed(self, tmp_path, monkeypatch):
        # We queried `old/name`; the GitHub API follows renames, so its
        # `full_name` reports the repo's live slug. value-data.csv must
        # carry that current name, not the stale one we asked for.
        _patch(tmp_path, monkeypatch,
               [["old/name", "True", "777", "new/name"]])
        aggs = [{"github_repo": "old/name", "git_url": "", "top_eco_pkg": "p"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0]["github_repo"] == "new/name"
        assert out[0].get("gh_repo_id") == "777"

    def test_unrenamed_repo_keeps_its_name(self, tmp_path, monkeypatch):
        _patch(tmp_path, monkeypatch,
               [["torvalds/linux", "True", "2325298", "torvalds/linux"]])
        aggs = [{"github_repo": "torvalds/linux", "git_url": "",
                 "top_eco_pkg": "p"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0]["github_repo"] == "torvalds/linux"

    def test_invalid_repo_is_not_renamed(self, tmp_path, monkeypatch):
        # A repo that failed validation keeps the slug we asked for —
        # there is no trustworthy current name to rewrite it to.
        _patch(tmp_path, monkeypatch, [["dead/repo", "False", "", ""]])
        aggs = [{"github_repo": "dead/repo", "git_url": "", "top_eco_pkg": "p"}]
        out, _ = verify_urls_in_aggregates(aggs)
        assert out[0]["github_repo"] == "dead/repo"
