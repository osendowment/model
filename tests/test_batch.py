"""Tests for batch module — CSV I/O and metrics extraction (no API calls)."""

import csv
import os

import pytest

from src.github.models import Contributor, RunResult, PerfStats
from src.github.batch_runner import (
    _build_metrics_extractors,
    _upsert_yearly_csv,
    _upsert_yearly_csv_batch,
    _read_existing_periods,
    _load_repos_from_csv,
)


def _make_result(contributors, bus_factor=1, hhi=0.5):
    return RunResult(
        bus_factor=bus_factor,
        contributors=contributors,
        hhi=hhi,
        perf=PerfStats(),
    )


def _make_contribs(specs):
    """specs: list of (login, commits, lines_changed, is_bot)."""
    contribs = []
    for login, commits, lines, is_bot in specs:
        c = Contributor(login=login, commits=commits, lines_changed=lines)
        c.is_bot = is_bot
        contribs.append(c)
    return contribs


class TestBuildMetricsExtractors:
    def test_all_keys_present(self):
        extractors = _build_metrics_extractors()
        expected = {"bus_factor", "hhi", "contributors", "bots", "commits", "first_date", "last_date"}
        assert set(extractors.keys()) == expected

    def test_bus_factor_extraction(self):
        extractors = _build_metrics_extractors()
        contribs = _make_contribs([("alice", 100, 500, False)])
        result = _make_result(contribs, bus_factor=1, hhi=1.0)
        assert extractors["bus_factor"](result) == "1"

    def test_hhi_extraction(self):
        extractors = _build_metrics_extractors()
        contribs = _make_contribs([("alice", 50, 500, False), ("bob", 50, 500, False)])
        result = _make_result(contribs, hhi=0.5)
        assert extractors["hhi"](result) == "5000"

    def test_contributors_counts_humans_only(self):
        extractors = _build_metrics_extractors()
        contribs = _make_contribs([
            ("alice", 50, 500, False),
            ("dependabot", 10, 100, True),
        ])
        result = _make_result(contribs)
        assert extractors["contributors"](result) == "1"

    def test_bots_count(self):
        extractors = _build_metrics_extractors()
        contribs = _make_contribs([
            ("alice", 50, 500, False),
            ("dependabot", 10, 100, True),
            ("renovate", 5, 50, True),
        ])
        result = _make_result(contribs)
        assert extractors["bots"](result) == "2"

    def test_commits_sums_human_only(self):
        extractors = _build_metrics_extractors()
        contribs = _make_contribs([
            ("alice", 80, 500, False),
            ("bob", 20, 200, False),
            ("bot", 50, 100, True),
        ])
        result = _make_result(contribs)
        assert extractors["commits"](result) == "100"

    def test_empty_result_returns_empty_strings(self):
        extractors = _build_metrics_extractors()
        result = _make_result([], bus_factor=0, hhi=0.0)
        assert extractors["bus_factor"](result) == ""
        assert extractors["hhi"](result) == ""
        assert extractors["commits"](result) == "0"


def _read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestUpsertYearlyCsv:
    def test_creates_split_files(self, tmp_path):
        dirpath = str(tmp_path)
        alice = Contributor(login="alice", commits=100, lines_changed=500)
        alice.is_bot = False
        import datetime
        rr = _make_result([alice], bus_factor=1, hhi=1.0)
        rr.first_week = datetime.date(2024, 3, 1)
        rr.last_week = datetime.date(2024, 11, 15)
        year_results = [("2024", rr)]

        _upsert_yearly_csv(dirpath, "owner/repo", year_results, quiet=True)

        bf_rows = _read_csv(tmp_path / "bus-factor.csv")
        assert len(bf_rows) == 1
        assert bf_rows[0]["repo"] == "owner/repo"
        assert bf_rows[0]["2024"] == "1"

        hhi_rows = _read_csv(tmp_path / "hhi.csv")
        assert hhi_rows[0]["2024"] == "10000"

        commits_rows = _read_csv(tmp_path / "commits.csv")
        assert commits_rows[0]["2024"] == "100"

        years_rows = _read_csv(tmp_path / "years.csv")
        assert years_rows == [{
            "repo": "owner/repo", "year": "2024",
            "first_date": "2024-03-01", "last_date": "2024-11-15",
        }]

    def test_updates_existing_repo(self, tmp_path):
        dirpath = str(tmp_path)
        contribs = _make_contribs([("alice", 50, 500, False)])
        contribs2 = _make_contribs([("alice", 80, 800, False), ("bob", 20, 200, False)])
        year_results = [
            ("2023", _make_result(contribs, bus_factor=1, hhi=1.0)),
            ("2024", _make_result(contribs2, bus_factor=1, hhi=0.68)),
        ]
        _upsert_yearly_csv(dirpath, "owner/repo", year_results, quiet=True)

        bf_rows = _read_csv(tmp_path / "bus-factor.csv")
        assert bf_rows[0]["2023"] == "1"
        assert bf_rows[0]["2024"] == "1"

    def test_preserves_other_repos(self, tmp_path):
        dirpath = str(tmp_path)
        contribs = _make_contribs([("alice", 100, 500, False)])

        _upsert_yearly_csv(dirpath, "repo/a", [("2024", _make_result(contribs))], quiet=True)
        _upsert_yearly_csv(dirpath, "repo/b", [("2024", _make_result(contribs))], quiet=True)

        bf_rows = _read_csv(tmp_path / "bus-factor.csv")
        assert {r["repo"] for r in bf_rows} == {"repo/a", "repo/b"}


class TestUpsertYearlyCsvBatch:
    def test_batch_writes_multiple_repos(self, tmp_path):
        dirpath = str(tmp_path)
        contribs = _make_contribs([("alice", 100, 500, False)])
        batch = [
            ("repo/a", [("2024", _make_result(contribs))]),
            ("repo/b", [("2024", _make_result(contribs))]),
        ]
        _upsert_yearly_csv_batch(dirpath, batch)

        bf_rows = _read_csv(tmp_path / "bus-factor.csv")
        assert {r["repo"] for r in bf_rows} == {"repo/a", "repo/b"}

    def test_empty_batch_noop(self, tmp_path):
        dirpath = str(tmp_path)
        _upsert_yearly_csv_batch(dirpath, [])
        assert not os.path.exists(tmp_path / "bus-factor.csv")

    def test_years_file_excludes_aggregate(self, tmp_path):
        dirpath = str(tmp_path)
        import datetime
        contribs = _make_contribs([("alice", 100, 500, False)])
        single = _make_result(contribs)
        single.first_week = datetime.date(2024, 1, 1)
        single.last_week = datetime.date(2024, 12, 31)
        agg = _make_result(contribs)
        agg.first_week = datetime.date(2021, 1, 1)
        agg.last_week = datetime.date(2025, 12, 31)
        batch = [("repo/a", [("2024", single), ("2021-2025", agg)])]
        _upsert_yearly_csv_batch(dirpath, batch)

        years_rows = _read_csv(tmp_path / "years.csv")
        assert [r["year"] for r in years_rows] == ["2024"]

        bf_rows = _read_csv(tmp_path / "bus-factor.csv")
        assert "2021-2025" in bf_rows[0] and bf_rows[0]["2021-2025"] == "1"


class TestReadExistingPeriods:
    def test_empty_dir(self, tmp_path):
        assert _read_existing_periods(str(tmp_path)) == {}

    def test_reads_periods(self, tmp_path):
        dirpath = str(tmp_path)
        contribs = _make_contribs([("alice", 100, 500, False)])
        year_results = [
            ("2023", _make_result(contribs, bus_factor=1)),
            ("2024", _make_result(contribs, bus_factor=1)),
        ]
        _upsert_yearly_csv(dirpath, "owner/repo", year_results, quiet=True)

        periods = _read_existing_periods(dirpath)
        assert "owner/repo" in periods
        assert {"2023", "2024"}.issubset(periods["owner/repo"])


class TestLoadReposFromCsv:
    def _write_csv(self, path, rows, fields=None):
        fields = fields or list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_loads_repos(self, tmp_path):
        filepath = str(tmp_path / "repos.csv")
        self._write_csv(filepath, [
            {"repo": "a/b", "stars": "100"},
            {"repo": "c/d", "stars": "200"},
        ])
        repos = _load_repos_from_csv(filepath)
        assert repos == ["c/d", "a/b"]  # sorted by stars desc

    def test_skips_archived(self, tmp_path):
        filepath = str(tmp_path / "repos.csv")
        self._write_csv(filepath, [
            {"repo": "a/b", "stars": "100", "archived": "False"},
            {"repo": "c/d", "stars": "200", "archived": "True"},
        ])
        repos = _load_repos_from_csv(filepath)
        assert repos == ["a/b"]

    def test_loads_all_repos(self, tmp_path):
        filepath = str(tmp_path / "repos.csv")
        self._write_csv(filepath, [
            {"repo": "a/b", "stars": "300"},
            {"repo": "c/d", "stars": "200"},
            {"repo": "e/f", "stars": "100"},
        ])
        repos = _load_repos_from_csv(filepath)
        assert len(repos) == 3
        assert repos == ["a/b", "c/d", "e/f"]

    def test_lowercases_repos(self, tmp_path):
        filepath = str(tmp_path / "repos.csv")
        self._write_csv(filepath, [{"repo": "Facebook/React", "stars": "100"}])
        repos = _load_repos_from_csv(filepath)
        assert repos == ["facebook/react"]

    def test_no_stars_column(self, tmp_path):
        filepath = str(tmp_path / "repos.csv")
        self._write_csv(filepath, [
            {"repo": "a/b"},
            {"repo": "c/d"},
        ])
        repos = _load_repos_from_csv(filepath)
        assert set(repos) == {"a/b", "c/d"}
