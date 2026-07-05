"""Tests for the GitHub identity pass in src/value/apply_ecosystems_authority.py.

`_resolve_github` stamps `repo_id`, `github_repo`, `git`, and `mirror_url` onto
per-package rows (in-place) using a repos.csv lookup after a TTL-gated fetch.

Tests use a fake repos.csv under `tmp_path` and stub `fetch_and_persist` so no
network calls occur — only the read-back + annotation logic is exercised.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import src.value.apply_ecosystems_authority as aea
from src.value.apply_ecosystems_authority import _resolve_github


def _write_repos_csv(tmp_path: Path, rows: list[list]) -> Path:
    """Write a minimal repos.csv with columns the identity pass needs."""
    csv_path = tmp_path / "repos.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["repo", "valid", "repo_id", "full_name", "mirror_url"])
        w.writerows(rows)
    return csv_path


def _patch(tmp_path: Path, monkeypatch, repo_rows: list[list]) -> Path:
    """Write a fake repos.csv and stub fetch_and_persist to a no-op."""
    repos_csv = _write_repos_csv(tmp_path, repo_rows)
    monkeypatch.setattr(aea, "fetch_and_persist", lambda **kw: {})
    return repos_csv


# ── repo_id ──────────────────────────────────────────────────────────────────

class TestRepoId:
    def test_populates_id_for_valid_repo(self, tmp_path, monkeypatch):
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["react/react", "True", "10270250", "react/react", ""]])
        rows = [{"git": "", "github_repo": "react/react", "eco_guess": ""}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["repo_id"] == "gh/10270250"

    def test_invalid_repo_has_blank_id(self, tmp_path, monkeypatch):
        # A 404'd repo (valid=False) gets no numeric id.
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["dead/repo", "False", "", "", ""]])
        rows = [{"git": "", "github_repo": "dead/repo"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["repo_id"] == ""

    def test_no_github_slug_has_blank_id(self, tmp_path, monkeypatch):
        # A row with no github_repo and no GitHub git URL carries no repo_id.
        repos_csv = _patch(tmp_path, monkeypatch, [])
        rows = [{"git": "", "github_repo": ""}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["repo_id"] == ""

    def test_slug_from_github_git_url(self, tmp_path, monkeypatch):
        # git URL is a GitHub URL: after canonicalize it becomes ""; slug falls
        # back to github_repo.  Verify repo_id is still set correctly.
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["torvalds/linux", "True", "2325298", "torvalds/linux", ""]])
        rows = [{"git": "https://github.com/torvalds/linux.git",
                 "github_repo": "torvalds/linux"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["repo_id"] == "gh/2325298"


# ── github_repo canonicalisation (post-rename name) ──────────────────────────

class TestRepoCurrentName:
    def test_rewrites_to_current_name_when_renamed(self, tmp_path, monkeypatch):
        # repos.csv recorded the repo under the old slug but the current
        # full_name is the renamed form — stamp must use full_name.
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["old/name", "True", "777", "new/name", ""]])
        rows = [{"git": "", "github_repo": "old/name"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["github_repo"] == "new/name"
        assert rows[0]["repo_id"] == "gh/777"
        assert rows[0]["git"] == "https://github.com/new/name.git"

    def test_unrenamed_repo_keeps_its_name(self, tmp_path, monkeypatch):
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["torvalds/linux", "True", "2325298", "torvalds/linux", ""]])
        rows = [{"git": "", "github_repo": "torvalds/linux"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["github_repo"] == "torvalds/linux"

    def test_invalid_repo_keeps_original_github_repo(self, tmp_path, monkeypatch):
        # A 404'd repo: github_repo is NOT rewritten (no trustworthy name).
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["dead/repo", "False", "", "", ""]])
        rows = [{"git": "", "github_repo": "dead/repo"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["github_repo"] == "dead/repo"

    def test_git_restored_as_canonical_github_url(self, tmp_path, monkeypatch):
        # After stamp, git = "https://github.com/{full_name}.git" even if the
        # original was the old slug URL (step 1 cleared it; step 5 restores).
        repos_csv = _patch(tmp_path, monkeypatch,
                           [["facebook/react", "True", "10270250", "react/react", ""]])
        rows = [{"git": "https://github.com/facebook/react.git",
                 "github_repo": "facebook/react"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["git"] == "https://github.com/react/react.git"
        assert rows[0]["github_repo"] == "react/react"


# ── mirror_url ───────────────────────────────────────────────────────────────

class TestMirrorUrl:
    def test_populates_mirror_url_for_github_mirror(self, tmp_path, monkeypatch):
        # GitHub records the upstream a mirror syncs from; stamp must copy it.
        repos_csv = _patch(tmp_path, monkeypatch, [
            ["gcc-mirror/gcc", "True", "22711503", "gcc-mirror/gcc",
             "git://gcc.gnu.org/git/gcc.git"]])
        rows = [{"git": "", "github_repo": "gcc-mirror/gcc"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["mirror_url"] == "git://gcc.gnu.org/git/gcc.git"
        assert rows[0]["git"] == "https://github.com/gcc-mirror/gcc.git"

    def test_non_mirror_repo_has_blank_mirror_url(self, tmp_path, monkeypatch):
        # An ordinary GitHub repo gets an empty mirror_url.
        repos_csv = _patch(tmp_path, monkeypatch, [
            ["facebook/react", "True", "10270250", "facebook/react", ""]])
        rows = [{"git": "", "github_repo": "facebook/react"}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["mirror_url"] == ""

    def test_unresolved_row_has_blank_mirror_url(self, tmp_path, monkeypatch):
        # A row with no GitHub slug gets an empty mirror_url (not stamped).
        repos_csv = _patch(tmp_path, monkeypatch, [])
        rows = [{"git": "git://sourceware.org/git/glibc.git", "github_repo": ""}]
        _resolve_github(rows, repos_file=str(repos_csv))
        assert rows[0]["mirror_url"] == ""

    def test_non_github_git_url_preserved_when_no_slug(self, tmp_path, monkeypatch):
        # A non-GitHub URL is canonicalised in step 1 and kept when no slug resolves.
        repos_csv = _patch(tmp_path, monkeypatch, [])
        rows = [{"git": "https://sourceware.org/git/glibc.git", "github_repo": ""}]
        _resolve_github(rows, repos_file=str(repos_csv))
        # canonicalize converts https:// → git:// for sourceware
        assert rows[0]["git"] == "git://sourceware.org/git/glibc.git"
        assert rows[0]["repo_id"] == ""
