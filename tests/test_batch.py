"""Tests for batch module — concentration-data I/O and repo loading (no API calls)."""

import csv

from src.github.models import Contributor, RunResult, PerfStats
from src.github.batch_runner import (
    _upsert_concentration_data,
    _load_repos_from_csv,
    CONCENTRATION_FIELDS,
)


def _make_result(contributors, bus_factor=1, hhi=0.5, total_commits=None,
                 total_contributors=None):
    return RunResult(
        bus_factor=bus_factor,
        contributors=contributors,
        hhi=hhi,
        perf=PerfStats(),
        total_commits=total_commits,
        total_contributors=total_contributors,
    )


def _make_contribs(specs):
    """specs: list of (login, commits, lines_changed, is_bot)."""
    contribs = []
    for login, commits, lines, is_bot in specs:
        c = Contributor(login=login, commits=commits, lines_changed=lines)
        c.is_bot = is_bot
        contribs.append(c)
    return contribs


class TestConcentrationFields:
    def test_active_contributors_present(self):
        assert "active_contributors" in CONCENTRATION_FIELDS


class TestUpsertConcentrationData:
    def test_writes_active_contributors_count(self, tmp_path, monkeypatch):
        # 2 humans + 1 bot → active_contributors == "2".
        contribs = _make_contribs([
            ("alice", 100, 500, False),
            ("bob", 50, 200, False),
            ("dependabot[bot]", 30, 100, True),
        ])
        result = _make_result(contribs, bus_factor=1, hhi=0.5,
                              total_commits=180, total_contributors=3)

        conc_file = tmp_path / "concentration-data.csv"
        monkeypatch.setattr("src.github.batch_runner.CONCENTRATION_FILE", str(conc_file))
        monkeypatch.setattr("src.github.batch_runner._load_repo_id_map", lambda: {})

        _upsert_concentration_data([("owner/repo", [("2021-2025", result)])], ["2021-2025"])

        with open(conc_file, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["repo"] == "owner/repo"
        assert rows[0]["active_contributors"] == "2"
        assert rows[0]["bus_factor"] == "1"

    def test_preserves_other_repos(self, tmp_path, monkeypatch):
        conc_file = tmp_path / "concentration-data.csv"
        conc_file.write_text(
            "repo,repo_id,total_commits,total_contributors,active_contributors,"
            "bus_factor,hhi,fetched_at\n"
            "keep/me,1,10,2,2,1,5000,2026-01-01T00:00:00+00:00\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("src.github.batch_runner.CONCENTRATION_FILE", str(conc_file))
        monkeypatch.setattr("src.github.batch_runner._load_repo_id_map", lambda: {})

        contribs = _make_contribs([("alice", 100, 500, False)])
        result = _make_result(contribs, bus_factor=1, hhi=1.0,
                              total_commits=100, total_contributors=1)
        _upsert_concentration_data([("new/repo", [("2021-2025", result)])], ["2021-2025"])

        with open(conc_file, encoding="utf-8") as f:
            rows = {r["repo"]: r for r in csv.DictReader(f)}
        assert set(rows) == {"keep/me", "new/repo"}
        assert rows["keep/me"]["active_contributors"] == "2"


class TestLoadReposFromCsv:
    def test_loads_repos(self, tmp_path):
        f = tmp_path / "repos.csv"
        f.write_text("repo,stars\nowner/a,10\nowner/b,5\n", encoding="utf-8")
        assert _load_repos_from_csv(str(f)) == ["owner/a", "owner/b"]

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
