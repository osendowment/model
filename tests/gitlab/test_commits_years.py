"""Tests for gitlab/commits_years — per-year last-SHA anchor."""
from __future__ import annotations

import csv

from src.sources.gitlab.commits_years import YEARS, _fetch_year, _pairs_to_fetch


class FakeResp:
    def __init__(self, status, json_body=None, headers=None):
        self.status = status
        self._json = json_body or []
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json


class FakeLimiter:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, session, host, url):
        self.calls.append(url)
        return self._resp


class TestFetchYear:
    async def test_returns_last_sha_and_commit_count(self):
        lim = FakeLimiter(FakeResp(200, json_body=[{"id": "deadbeef"}],
                                   headers={"X-Total": "42"}))
        sha, commits = await _fetch_year(lim, None, "gitlab.com", "g/p", "main", 2024)
        assert sha == "deadbeef"
        assert commits == 42
        assert "since=2024-01-01" in lim.calls[0]
        assert "until=2024-12-31" in lim.calls[0]

    async def test_empty_year_returns_blank(self):
        lim = FakeLimiter(FakeResp(200, json_body=[], headers={"X-Total": "0"}))
        sha, commits = await _fetch_year(lim, None, "gitlab.com", "g/p", "main", 2019)
        assert sha == ""
        assert commits == 0


class TestPairsToFetch:
    """Regression coverage for the per-(repo_id, year) selection bug: a repo
    with ANY existing row must not be skipped wholesale — only years already
    anchored for that repo_id should be skipped."""

    PROJECT = {"repo_id": "gl:123", "project": "g/p", "git_url": "https://gitlab.com/g/p.git",
               "host": "gitlab.com", "path": "g/p", "branch": "main"}

    def test_new_project_all_years(self):
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=set(), force=False)
        assert pairs == [(self.PROJECT, year) for year in YEARS]

    def test_skips_existing_year_keeps_missing(self):
        # Only 2021 is anchored for this repo_id — every other YEAR (including
        # ones added later, e.g. a new calendar year) must still be fetched.
        existing = {("gl:123", "2021")}
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=existing, force=False)
        years_selected = [year for _, year in pairs]
        assert 2021 not in years_selected
        assert years_selected == [y for y in YEARS if y != 2021]

    def test_force_selects_all(self):
        # Even with every year already present, --force must re-fetch all.
        existing = {("gl:123", str(y)) for y in YEARS}
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=existing, force=True)
        assert [year for _, year in pairs] == YEARS


class TestLoadExistingPairs:
    def test_roundtrip(self, tmp_path, monkeypatch):
        import src.sources.gitlab.commits_years as mod

        out = tmp_path / "commits-years.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=mod.COMMITS_FIELDS)
            w.writeheader()
            w.writerow({"repo_id": "gl:1", "git_url": "u", "project": "g/p",
                        "year": "2021", "first_sha": "", "last_sha": "abc",
                        "commits": "5", "fetched_at": "2026-01-01T00:00:00Z"})
            w.writerow({"repo_id": "gl:1", "git_url": "u", "project": "g/p",
                        "year": "2022", "first_sha": "", "last_sha": "def",
                        "commits": "3", "fetched_at": "2026-01-01T00:00:00Z"})
            w.writerow({"repo_id": "gl:2", "git_url": "u2", "project": "g/q",
                        "year": "2021", "first_sha": "", "last_sha": "xyz",
                        "commits": "1", "fetched_at": "2026-01-01T00:00:00Z"})
        monkeypatch.setattr(mod, "COMMITS_OUT", out)
        assert mod._load_existing_pairs() == {
            ("gl:1", "2021"), ("gl:1", "2022"), ("gl:2", "2021"),
        }
