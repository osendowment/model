"""Tests for search module — pure logic functions (no API calls)."""

import csv
import os
import tempfile

from src.sources.github.fetch_top_repos import _parse_item, _cache_key, _initial_periods, _upsert_repos, FIELDS


class TestParseItem:
    def test_basic_item(self):
        item = {
            "full_name": "Facebook/React",
            "id": 12345,
            "node_id": "MDEwOlJlcG9z",
            "description": "A JS library",
            "homepage": "https://react.dev",
            "default_branch": "Main",
            "language": "JavaScript",
            "stargazers_count": 200000,
            "forks_count": 40000,
            "open_issues_count": 1000,
            "size": 300000,
            "fork": False,
            "has_downloads": True,
            "archived": False,
            "disabled": False,
            "created_at": "2013-05-24T16:15:54Z",
            "updated_at": "2025-03-11T00:00:00Z",
            "pushed_at": "2025-03-10T12:00:00Z",
            "topics": ["react", "javascript", "frontend"],
            "owner": {"login": "Facebook", "id": 69631, "node_id": "MDQ6", "type": "Organization"},
            "license": {"key": "mit", "name": "MIT License"},
        }
        slug, row = _parse_item(item, "2025-03-11T00:00:00Z")
        assert slug == "facebook/react"
        assert row["repo"] == "facebook/react"
        assert row["repo_id"] == "gh/12345"  # namespaced at fetch time
        assert row["user_name"] == "facebook"
        assert row["user_type"] == "Organization"
        assert row["language"] == "javascript"
        assert row["default_branch"] == "main"
        assert row["license"] == "mit"
        assert row["stars"] == 200000
        assert row["topics"] == "react,javascript,frontend"
        assert row["fetched_at"] == "2025-03-11T00:00:00Z"

    def test_missing_optional_fields(self):
        item = {
            "full_name": "user/repo",
            "id": 1,
            "stargazers_count": 100,
            "forks_count": 10,
        }
        slug, row = _parse_item(item, "2025-01-01T00:00:00Z")
        assert slug == "user/repo"
        assert row["license"] == ""
        assert row["user_name"] == ""
        assert row["user_id"] == 0
        assert row["description"] == ""
        assert row["topics"] == ""
        assert row["language"] == ""

    def test_description_newlines_stripped(self):
        item = {
            "full_name": "a/b",
            "id": 2,
            "description": "Line1\nLine2\rLine3",
            "stargazers_count": 50,
            "forks_count": 5,
        }
        _, row = _parse_item(item, "2025-01-01T00:00:00Z")
        assert row["description"] == "Line1 Line2 Line3"

    def test_null_description(self):
        item = {
            "full_name": "a/b",
            "id": 3,
            "description": None,
            "stargazers_count": 10,
            "forks_count": 1,
        }
        _, row = _parse_item(item, "2025-01-01T00:00:00Z")
        assert row["description"] == ""


class TestCacheKey:
    def test_format(self):
        assert _cache_key("Python", 1000, "2020-01-01", "2023-12-31") == "Python|1000|2020-01-01|2023-12-31"

    def test_different_params(self):
        k1 = _cache_key("C", 500, "2020-01-01", "2020-12-31")
        k2 = _cache_key("C++", 500, "2020-01-01", "2020-12-31")
        assert k1 != k2


class TestInitialPeriods:
    def test_returns_list_of_tuples(self):
        periods = _initial_periods()
        assert isinstance(periods, list)
        assert all(isinstance(p, tuple) and len(p) == 2 for p in periods)

    def test_starts_at_2008(self):
        periods = _initial_periods()
        assert periods[0][0] == "2008-01-01"

    def test_periods_are_4_years(self):
        periods = _initial_periods()
        # First period should end at 2011-12-31 (unless current year <= 2011)
        assert periods[0][1] == "2011-12-31"

    def test_last_period_ends_at_today(self):
        import datetime
        periods = _initial_periods()
        last_end = periods[-1][1]
        assert last_end == datetime.date.today().isoformat()


class TestUpsertRepos:
    def _write_csv(self, path, rows):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _read_csv(self, path):
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _make_row(self, repo_id, repo, stars, **extra):
        row = {f: "" for f in FIELDS}
        row.update(repo=repo, repo_id=str(repo_id), stars=str(stars), **extra)
        return row

    def test_adds_new_repos(self, tmp_path, monkeypatch):
        repos_file = str(tmp_path / "repos.csv")
        monkeypatch.setattr("src.sources.github.fetch_top_repos.REPOS_FILE", repos_file)

        new = {
            "a/b": self._make_row(1, "a/b", 500),
            "c/d": self._make_row(2, "c/d", 1000),
        }
        total, added, updated = _upsert_repos(new)
        assert total == 2
        assert added == 2
        assert updated == 0

        rows = self._read_csv(repos_file)
        assert rows[0]["repo"] == "c/d"  # sorted by stars desc
        assert rows[1]["repo"] == "a/b"

    def test_updates_existing_repos(self, tmp_path, monkeypatch):
        repos_file = str(tmp_path / "repos.csv")
        monkeypatch.setattr("src.sources.github.fetch_top_repos.REPOS_FILE", repos_file)

        self._write_csv(repos_file, [self._make_row(1, "a/b", 500)])

        new = {"a/b": self._make_row(1, "a/b", 600)}
        total, added, updated = _upsert_repos(new)
        assert total == 1
        assert added == 0
        assert updated == 1

        rows = self._read_csv(repos_file)
        assert rows[0]["stars"] == "600"

    def test_preserves_existing_and_adds_new(self, tmp_path, monkeypatch):
        repos_file = str(tmp_path / "repos.csv")
        monkeypatch.setattr("src.sources.github.fetch_top_repos.REPOS_FILE", repos_file)

        self._write_csv(repos_file, [self._make_row(1, "a/b", 500)])

        new = {"c/d": self._make_row(2, "c/d", 1000)}
        total, added, updated = _upsert_repos(new)
        assert total == 2
        assert added == 1
        assert updated == 0

    def test_sorted_by_stars_descending(self, tmp_path, monkeypatch):
        repos_file = str(tmp_path / "repos.csv")
        monkeypatch.setattr("src.sources.github.fetch_top_repos.REPOS_FILE", repos_file)

        new = {
            "low/stars": self._make_row(1, "low/stars", 100),
            "mid/stars": self._make_row(2, "mid/stars", 500),
            "high/stars": self._make_row(3, "high/stars", 2000),
        }
        _upsert_repos(new)
        rows = self._read_csv(repos_file)
        stars = [int(r["stars"]) for r in rows]
        assert stars == [2000, 500, 100]
