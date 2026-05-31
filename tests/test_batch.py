"""Tests for batch module — contributor-commits I/O and repo loading (no API calls)."""

import csv

from src.sources.github.batch_runner import (
    _upsert_contributor_commits,
    _load_repos_from_csv,
    GH_CONTRIB_FIELDS,
    GH_CONTRIB_STATUS_FIELDS,
)


class TestContribFields:
    def test_contrib_fields(self):
        assert GH_CONTRIB_FIELDS == ["repo", "login", "contributions", "account_type"]

    def test_status_fields(self):
        assert GH_CONTRIB_STATUS_FIELDS == [
            "repo", "repo_id", "status", "n_contributors", "fetched_at"
        ]


class TestUpsertContributorCommits:
    def test_writes_long_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_FILE",
                            str(tmp_path / "contributor-commits.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_STATUS_FILE",
                            str(tmp_path / "contributor-commits.status.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner._load_repo_id_map", lambda: {})

        raw = [
            {"login": "Alice", "contributions": 100, "type": "User"},
            {"login": "dependabot[bot]", "contributions": 5, "type": "Bot"},
        ]
        _upsert_contributor_commits([("owner/repo", raw)])

        with open(tmp_path / "contributor-commits.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 2
        assert rows[0]["repo"] == "owner/repo"
        assert rows[0]["login"] == "alice"          # lowercased
        assert rows[0]["contributions"] == "100"
        assert rows[0]["account_type"] == "User"
        assert rows[1]["account_type"] == "Bot"

    def test_status_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_FILE",
                            str(tmp_path / "contributor-commits.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_STATUS_FILE",
                            str(tmp_path / "contributor-commits.status.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_contributor_commits([("owner/repo", [{"login": "a", "contributions": 1, "type": "User"}])])

        with open(tmp_path / "contributor-commits.status.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["status"] == "ok"
        assert rows[0]["n_contributors"] == "1"

    def test_status_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_FILE",
                            str(tmp_path / "contributor-commits.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_STATUS_FILE",
                            str(tmp_path / "contributor-commits.status.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_contributor_commits([("owner/empty", [])])

        with open(tmp_path / "contributor-commits.status.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["status"] == "empty"
        assert rows[0]["n_contributors"] == "0"

    def test_status_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_FILE",
                            str(tmp_path / "contributor-commits.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_STATUS_FILE",
                            str(tmp_path / "contributor-commits.status.csv"))
        monkeypatch.setattr("src.sources.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_contributor_commits([("owner/errored", None)])

        with open(tmp_path / "contributor-commits.status.csv", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["status"] == "error"

        # No long rows written for errored repo
        with open(tmp_path / "contributor-commits.csv", encoding="utf-8") as f:
            long_rows = list(csv.DictReader(f))
        assert long_rows == []

    def test_preserves_other_repos(self, tmp_path, monkeypatch):
        contrib_file = tmp_path / "contributor-commits.csv"
        status_file = tmp_path / "contributor-commits.status.csv"

        # Seed existing data for keep/me
        contrib_file.write_text(
            "repo,login,contributions,account_type\n"
            "keep/me,alice,50,User\n",
            encoding="utf-8",
        )
        status_file.write_text(
            "repo,repo_id,status,n_contributors,fetched_at\n"
            "keep/me,1,ok,1,2026-01-01T00:00:00+00:00\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_FILE", str(contrib_file))
        monkeypatch.setattr("src.sources.github.batch_runner.GH_CONTRIB_STATUS_FILE", str(status_file))
        monkeypatch.setattr("src.sources.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_contributor_commits([("new/repo", [{"login": "bob", "contributions": 10, "type": "User"}])])

        with open(contrib_file, encoding="utf-8") as f:
            repos_in_file = {r["repo"] for r in csv.DictReader(f)}
        assert repos_in_file == {"keep/me", "new/repo"}

        with open(status_file, encoding="utf-8") as f:
            status_repos = {r["repo"] for r in csv.DictReader(f)}
        assert status_repos == {"keep/me", "new/repo"}


class TestLoadReposFromCsv:
    def test_loads_repos_sorted_by_stars_desc(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo,stars\nowner/a,10\nowner/b,50\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/b", "owner/a"]

    def test_skips_archived(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo,archived\nowner/a,false\nowner/b,true\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/a"]

    def test_lowercases_repos(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo\nOwner/Repo\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/repo"]

    def test_no_stars_column(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo\nowner/a\nowner/b\n", encoding="utf-8")
        assert sorted(_load_repos_from_csv(str(f))) == ["owner/a", "owner/b"]
