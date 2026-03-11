"""Tests for contributors module — compute_yearly_breakdown, _parse_api_stats, bot filtering."""

import datetime

import pytest

from src.github.models import Contributor, DateRange, is_bot
from src.github.contributors import (
    compute_yearly_breakdown,
    _parse_api_stats,
    _compute_bus_factor,
)


def _ts(year, month=6, day=1):
    """Helper: unix timestamp for a date."""
    return int(datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc).timestamp())


def _make_stats(contributions, year=None):
    """Helper: list of (login, commits, added, deleted, timestamp) -> GitHub stats format."""
    return [
        {
            "author": {"login": login},
            "total": commits,
            "weeks": [{"w": ts, "a": added, "d": deleted, "c": commits}],
        }
        for login, commits, added, deleted, ts in contributions
    ]


class TestParseApiStats:
    def test_basic_parsing(self):
        stats = _make_stats([("alice", 10, 500, 100, _ts(2024))])
        contribs, first, last = _parse_api_stats(stats)
        assert len(contribs) == 1
        assert contribs[0].login == "alice"
        assert contribs[0].commits == 10
        assert contribs[0].lines_added == 500
        assert contribs[0].lines_deleted == 100
        assert contribs[0].lines_changed == 600

    def test_skips_zero_activity(self):
        stats = [
            {"author": {"login": "ghost"}, "total": 0, "weeks": [{"w": _ts(2024), "a": 0, "d": 0, "c": 0}]}
        ]
        contribs, _, _ = _parse_api_stats(stats)
        assert len(contribs) == 0

    def test_date_range_filtering(self):
        stats = _make_stats([
            ("alice", 10, 500, 0, _ts(2023)),
            ("bob", 5, 200, 0, _ts(2024)),
        ])
        # Merge into single author entry with multiple weeks
        merged = [{
            "author": {"login": "alice"},
            "total": 15,
            "weeks": [
                {"w": _ts(2023), "a": 500, "d": 0, "c": 10},
                {"w": _ts(2024), "a": 300, "d": 0, "c": 5},
            ],
        }]
        dr = DateRange.from_years(2024)
        contribs, _, _ = _parse_api_stats(merged, date_range=dr)
        assert contribs[0].commits == 5
        assert contribs[0].lines_changed == 300

    def test_first_last_week(self):
        stats = [{
            "author": {"login": "alice"},
            "total": 20,
            "weeks": [
                {"w": _ts(2023, 1, 15), "a": 100, "d": 0, "c": 5},
                {"w": _ts(2024, 6, 15), "a": 200, "d": 0, "c": 10},
            ],
        }]
        _, first, last = _parse_api_stats(stats)
        assert first == datetime.date(2023, 1, 15)
        # last_week = week start + 6 days
        assert last == datetime.date(2024, 6, 21)

    def test_missing_author(self):
        stats = [{"author": None, "total": 5, "weeks": [{"w": _ts(2024), "a": 100, "d": 0, "c": 5}]}]
        contribs, _, _ = _parse_api_stats(stats)
        assert contribs[0].login == "unknown"

    def test_empty_stats(self):
        contribs, first, last = _parse_api_stats([])
        assert contribs == []
        assert first is None
        assert last is None


class TestComputeBusFactor:
    def test_bot_filtering(self):
        contribs = [
            Contributor(login="alice", commits=100, lines_changed=1000),
            Contributor(login="dependabot[bot]", commits=50, lines_changed=500),
        ]
        bf, sorted_c, hhi = _compute_bus_factor(contribs)
        assert bf == 1
        bot = next(c for c in sorted_c if c.login == "dependabot[bot]")
        assert bot.is_bot is True

    def test_include_bots_flag(self):
        contribs = [
            Contributor(login="alice", commits=40, lines_changed=400),
            Contributor(login="dependabot[bot]", commits=40, lines_changed=400),
            Contributor(login="bob", commits=20, lines_changed=200),
        ]
        bf, sorted_c, _ = _compute_bus_factor(contribs, include_bots=True)
        assert bf == 2  # alice + dependabot both count as humans
        assert all(not c.is_bot for c in sorted_c)

    def test_hhi_calculation(self):
        contribs = [
            Contributor(login="alice", commits=90, lines_changed=900),
            Contributor(login="bob", commits=10, lines_changed=100),
        ]
        _, _, hhi = _compute_bus_factor(contribs)
        assert hhi == pytest.approx(0.82)

    def test_locs_base(self):
        contribs = [
            Contributor(login="alice", commits=5, lines_changed=1000, lines_added=800, lines_deleted=200),
            Contributor(login="bob", commits=50, lines_changed=100, lines_added=80, lines_deleted=20),
        ]
        bf, sorted_c, _ = _compute_bus_factor(contribs, base="locs")
        assert bf == 1
        assert sorted_c[0].login == "alice"  # more lines changed


class TestComputeYearlyBreakdown:
    def test_basic_breakdown(self):
        stats = [{
            "author": {"login": "alice"},
            "total": 30,
            "weeks": [
                {"w": _ts(2023), "a": 100, "d": 0, "c": 10},
                {"w": _ts(2024), "a": 200, "d": 0, "c": 20},
            ],
        }]
        results = compute_yearly_breakdown(stats, 2023, 2024)

        assert len(results) == 3  # 2023, 2024, 2023-2024
        labels = [label for label, _ in results]
        assert labels == ["2023", "2024", "2023-2024"]

    def test_yearly_isolation(self):
        stats = [{
            "author": {"login": "alice"},
            "total": 30,
            "weeks": [
                {"w": _ts(2023), "a": 100, "d": 0, "c": 10},
                {"w": _ts(2024), "a": 200, "d": 0, "c": 20},
            ],
        }]
        results = compute_yearly_breakdown(stats, 2023, 2024)

        _, r2023 = results[0]
        _, r2024 = results[1]
        _, r_total = results[2]

        assert r2023.contributors[0].commits == 10
        assert r2024.contributors[0].commits == 20
        assert r_total.contributors[0].commits == 30

    def test_empty_stats(self):
        results = compute_yearly_breakdown([], 2023, 2024)
        assert len(results) == 3
        for _, r in results:
            assert r.bus_factor == 0
            assert r.contributors == []

    def test_total_label(self):
        stats = [{
            "author": {"login": "alice"},
            "total": 10,
            "weeks": [{"w": _ts(2024), "a": 100, "d": 0, "c": 10}],
        }]
        results = compute_yearly_breakdown(stats, 2021, 2025)
        total_label = results[-1][0]
        assert total_label == "2021-2025"

    def test_single_year_range(self):
        stats = [{
            "author": {"login": "alice"},
            "total": 10,
            "weeks": [{"w": _ts(2024), "a": 100, "d": 0, "c": 10}],
        }]
        results = compute_yearly_breakdown(stats, 2024, 2024)
        assert len(results) == 2  # 2024 + 2024-2024


class TestIsBotDetection:
    def test_known_bots(self):
        assert is_bot("dependabot[bot]") is True
        assert is_bot("renovate[bot]") is True
        assert is_bot("greenkeeper[bot]") is True

    def test_bot_suffix(self):
        assert is_bot("some-ci[bot]") is True

    def test_human(self):
        assert is_bot("alice") is False
        assert is_bot("bob-dev") is False

    def test_case_insensitive(self):
        assert is_bot("Dependabot[bot]") is True
