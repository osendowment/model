"""Tests for contributor metrics modules."""

import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from src.github.models import Contributor, DateRange
from src.github.contributors import _sanitize, calculate_bus_factor, parse_repo
from src.github.api import fetch_contributor_stats, _fetch_stats_once, _Deferred, _NoStats, _AsyncRateLimiter
from src.risk import concentration_class


class TestParseRepo:
    def test_full_url(self):
        assert parse_repo("https://github.com/pixijs/pixijs") == "pixijs/pixijs"

    def test_url_with_git_suffix(self):
        assert parse_repo("https://github.com/pixijs/pixijs.git") == "pixijs/pixijs"

    def test_slug(self):
        assert parse_repo("pixijs/pixijs") == "pixijs/pixijs"

    def test_trailing_slash(self):
        assert parse_repo("https://github.com/pixijs/pixijs/") == "pixijs/pixijs"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_repo("not-a-repo")


class TestCalculateBusFactor:
    @staticmethod
    def _make_stats(contributions: list[tuple[str, int, int]]) -> list[dict]:
        """Helper: list of (login, commits, lines) -> GitHub stats format."""
        return [
            {
                "author": {"login": login},
                "total": commits,
                "weeks": [{"a": lines, "d": 0, "c": commits}],
            }
            for login, commits, lines in contributions
        ]

    def test_single_contributor(self):
        stats = self._make_stats([("alice", 100, 1000)])
        bf, contribs, _ = calculate_bus_factor(stats)
        assert bf == 1
        assert contribs[0].pct == 1.0

    def test_equal_contributors(self):
        stats = self._make_stats([
            ("alice", 100, 500),
            ("bob", 100, 500),
            ("charlie", 100, 500),
        ])
        bf, contribs, _ = calculate_bus_factor(stats)
        assert bf == 2

    def test_dominant_contributor(self):
        stats = self._make_stats([
            ("alice", 900, 9000),
            ("bob", 50, 500),
            ("charlie", 50, 500),
        ])
        bf, contribs, _ = calculate_bus_factor(stats)
        assert bf == 1
        assert contribs[0].login == "alice"

    def test_gradual_distribution(self):
        stats = self._make_stats([
            ("a", 40, 400),
            ("b", 30, 300),
            ("c", 20, 200),
            ("d", 10, 100),
        ])
        bf, contribs, _ = calculate_bus_factor(stats)
        assert bf == 2

    def test_empty_stats(self):
        bf, contribs, hhi = calculate_bus_factor([])
        assert bf == 0
        assert contribs == []
        assert hhi == 0.0

    def test_custom_threshold(self):
        stats = self._make_stats([
            ("a", 40, 400),
            ("b", 30, 300),
            ("c", 20, 200),
            ("d", 10, 100),
        ])
        bf, _, _ = calculate_bus_factor(stats, threshold=0.89)
        assert bf == 3

    def test_locs_base(self):
        stats = self._make_stats([
            ("alice", 10, 1000),
            ("bob", 10, 200),
        ])
        bf, contribs, _ = calculate_bus_factor(stats, base="locs")
        assert bf == 1
        assert contribs[0].login == "alice"
        assert contribs[0].lines_changed == 1000
        assert contribs[1].lines_changed == 200

    def test_zero_commits(self):
        stats = [
            {"author": {"login": "alice"}, "total": 0, "weeks": [{"a": 0, "d": 0, "c": 0}]},
            {"author": {"login": "bob"}, "total": 0, "weeks": [{"a": 0, "d": 0, "c": 0}]},
        ]
        bf, contribs, _ = calculate_bus_factor(stats)
        assert bf == 0

    def test_logins_lowercased(self):
        stats = self._make_stats([("AlIcE", 100, 1000)])
        _, contribs, _ = calculate_bus_factor(stats)
        assert contribs[0].login == "alice"

    def test_year_filter(self):
        jan_2024 = int(datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc).timestamp())
        jun_2023 = int(datetime.datetime(2023, 6, 15, tzinfo=datetime.timezone.utc).timestamp())
        stats = [
            {
                "author": {"login": "alice"},
                "total": 20,
                "weeks": [
                    {"w": jan_2024, "a": 500, "d": 100, "c": 10},
                    {"w": jun_2023, "a": 200, "d": 50, "c": 5},
                ],
            }
        ]
        bf_all, contribs_all, _ = calculate_bus_factor(stats)
        assert contribs_all[0].commits == 15
        assert contribs_all[0].lines_changed == 850

        bf_2024, contribs_2024, _ = calculate_bus_factor(stats, year=2024)
        assert contribs_2024[0].commits == 10
        assert contribs_2024[0].lines_changed == 600

        _, contribs_2023, _ = calculate_bus_factor(stats, year=2023)
        assert contribs_2023[0].commits == 5
        assert contribs_2023[0].lines_changed == 250

    def test_year_filter_excludes_inactive(self):
        ts_2024 = int(datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        ts_2023 = int(datetime.datetime(2023, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        stats = [
            {"author": {"login": "alice"}, "total": 10, "weeks": [{"w": ts_2024, "a": 500, "d": 0, "c": 10}]},
            {"author": {"login": "bob"}, "total": 5, "weeks": [{"w": ts_2023, "a": 300, "d": 0, "c": 5}]},
        ]
        _, contribs, _ = calculate_bus_factor(stats, year=2024)
        assert len(contribs) == 1
        assert contribs[0].login == "alice"

    def test_year_range(self):
        ts_2022 = int(datetime.datetime(2022, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        ts_2023 = int(datetime.datetime(2023, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        ts_2024 = int(datetime.datetime(2024, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        ts_2025 = int(datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc).timestamp())
        stats = [
            {
                "author": {"login": "alice"},
                "total": 40,
                "weeks": [
                    {"w": ts_2022, "a": 100, "d": 0, "c": 10},
                    {"w": ts_2023, "a": 200, "d": 0, "c": 10},
                    {"w": ts_2024, "a": 300, "d": 0, "c": 10},
                    {"w": ts_2025, "a": 400, "d": 0, "c": 10},
                ],
            }
        ]
        _, contribs, _ = calculate_bus_factor(stats, year=2023, year_end=2024)
        assert contribs[0].lines_changed == 500
        assert contribs[0].commits == 20

        _, contribs, _ = calculate_bus_factor(stats, year=2022, year_end=2025)
        assert contribs[0].lines_changed == 1000

    def test_week_start_in_year(self):
        dec_29_2023 = int(datetime.datetime(2023, 12, 29, tzinfo=datetime.timezone.utc).timestamp())
        jan_5_2024 = int(datetime.datetime(2024, 1, 5, tzinfo=datetime.timezone.utc).timestamp())
        stats = [
            {
                "author": {"login": "alice"},
                "total": 20,
                "weeks": [
                    {"w": dec_29_2023, "a": 100, "d": 0, "c": 5},
                    {"w": jan_5_2024, "a": 200, "d": 0, "c": 10},
                ],
            }
        ]
        _, contribs, _ = calculate_bus_factor(stats, year=2024)
        assert contribs[0].commits == 10
        assert contribs[0].lines_changed == 200

        _, contribs, _ = calculate_bus_factor(stats, year=2023)
        assert contribs[0].commits == 5
        assert contribs[0].lines_changed == 100


class TestHHI:
    @staticmethod
    def _make_stats(contributions: list[tuple[str, int]]) -> list[dict]:
        return [
            {
                "author": {"login": login},
                "total": commits,
                "weeks": [{"a": 0, "d": 0, "c": commits}],
            }
            for login, commits in contributions
        ]

    def test_monopoly(self):
        stats = self._make_stats([("alice", 100)])
        _, _, hhi = calculate_bus_factor(stats)
        assert hhi == pytest.approx(1.0)

    def test_equal_two(self):
        stats = self._make_stats([("alice", 50), ("bob", 50)])
        _, _, hhi = calculate_bus_factor(stats)
        assert hhi == pytest.approx(0.5)

    def test_equal_four(self):
        stats = self._make_stats([("a", 25), ("b", 25), ("c", 25), ("d", 25)])
        _, _, hhi = calculate_bus_factor(stats)
        assert hhi == pytest.approx(0.25)

    def test_skewed(self):
        stats = self._make_stats([("alice", 90), ("bob", 10)])
        _, _, hhi = calculate_bus_factor(stats)
        assert hhi == pytest.approx(0.82)

    def test_empty(self):
        _, _, hhi = calculate_bus_factor([])
        assert hhi == 0.0


class TestDateRange:
    def test_from_years_single(self):
        dr = DateRange.from_years(2024)
        assert dr.since == datetime.date(2024, 1, 1)
        assert dr.until == datetime.date(2024, 12, 31)
        assert dr.label == "2024"

    def test_from_years_range(self):
        dr = DateRange.from_years(2022, 2024)
        assert dr.since == datetime.date(2022, 1, 1)
        assert dr.until == datetime.date(2024, 12, 31)
        assert dr.label == "2022–2024"

    def test_from_dates(self):
        dr = DateRange.from_dates("2024-06-01", "2025-01-31")
        assert dr.since == datetime.date(2024, 6, 1)
        assert dr.until == datetime.date(2025, 1, 31)
        assert dr.label == "2024-06-01 to 2025-01-31"

    def test_from_dates_since_only(self):
        dr = DateRange.from_dates("2024-06-01", None)
        assert dr.since == datetime.date(2024, 6, 1)
        assert dr.until is None
        assert dr.label == "since 2024-06-01"

    def test_from_dates_until_only(self):
        dr = DateRange.from_dates(None, "2025-01-31")
        assert dr.since is None
        assert dr.until == datetime.date(2025, 1, 31)
        assert dr.label == "until 2025-01-31"

    def test_empty(self):
        dr = DateRange()
        assert dr.is_empty
        assert dr.label == ""

    def test_date_range_filter(self):
        ts_jan = int(datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc).timestamp())
        ts_jul = int(datetime.datetime(2024, 7, 15, tzinfo=datetime.timezone.utc).timestamp())
        ts_dec = int(datetime.datetime(2024, 12, 15, tzinfo=datetime.timezone.utc).timestamp())
        stats = [
            {
                "author": {"login": "alice"},
                "total": 30,
                "weeks": [
                    {"w": ts_jan, "a": 100, "d": 0, "c": 10},
                    {"w": ts_jul, "a": 200, "d": 0, "c": 10},
                    {"w": ts_dec, "a": 300, "d": 0, "c": 10},
                ],
            }
        ]
        dr = DateRange.from_dates("2024-06-01", "2024-12-31")
        _, contribs, _ = calculate_bus_factor(stats, date_range=dr)
        assert contribs[0].lines_changed == 500
        assert contribs[0].commits == 20


class TestSanitize:
    def test_clean_name(self):
        assert _sanitize("Alice") == "Alice"

    def test_replacement_chars_stripped(self):
        assert _sanitize("Jos\ufffd Garc\ufffda") == "Jos Garca"

    def test_whitespace_normalized(self):
        assert _sanitize("  Alice   Bob  ") == "Alice Bob"

    def test_empty_after_strip(self):
        assert _sanitize("\ufffd\ufffd") == ""

    def test_mixed(self):
        assert _sanitize("  \ufffdAlice\ufffd  Bob  ") == "Alice Bob"


class TestConcentrationRiskClass:
    def test_class_a_bf1_hhi_10000(self):
        assert concentration_class(1, 10000) == "A"

    def test_class_a_bf1_hhi_9000(self):
        assert concentration_class(1, 9000) == "A"

    def test_class_a_bf1_hhi_8000(self):
        assert concentration_class(1, 8000) == "A"

    def test_class_b_bf1_hhi_7999(self):
        assert concentration_class(1, 7999) == "B"

    def test_class_b_bf2_hhi_5000(self):
        assert concentration_class(2, 5000) == "B"

    def test_class_c_bf2_hhi_4999(self):
        assert concentration_class(2, 4999) == "C"

    def test_class_c_bf3_hhi_2500(self):
        assert concentration_class(3, 2500) == "C"

    def test_class_c_bf4_hhi_3000(self):
        assert concentration_class(4, 3000) == "C"

    def test_class_d_bf4_hhi_2499(self):
        assert concentration_class(4, 2499) == "D"

    def test_class_d_bf5_hhi_5000(self):
        assert concentration_class(5, 5000) == "D"

    def test_class_d_bf10_hhi_1000(self):
        assert concentration_class(10, 1000) == "D"


class TestFetchContributorStats:
    @patch("src.github.api._session")
    @patch("src.github.api.time.sleep")
    def test_204_retries_then_succeeds(self, mock_sleep, mock_session):
        resp_204 = MagicMock(status_code=204)
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = [{"author": {"login": "alice"}, "total": 10, "weeks": []}]
        mock_session.get.side_effect = [resp_204, resp_204, resp_200]

        result = fetch_contributor_stats("owner/repo", token="fake-token", retries=5)

        assert len(result) == 1
        assert result[0]["author"]["login"] == "alice"
        assert mock_session.get.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("src.github.api._session")
    @patch("src.github.api.time.sleep")
    def test_202_and_204_mixed(self, mock_sleep, mock_session):
        resp_202 = MagicMock(status_code=202)
        resp_204 = MagicMock(status_code=204)
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = [{"author": {"login": "bob"}, "total": 5, "weeks": []}]
        mock_session.get.side_effect = [resp_202, resp_204, resp_202, resp_200]

        result = fetch_contributor_stats("owner/repo", token="fake-token", retries=5)

        assert len(result) == 1
        assert mock_session.get.call_count == 4

    @patch("src.github.api._session")
    @patch("src.github.api.time.sleep")
    def test_exhausted_retries_returns_empty(self, mock_sleep, mock_session):
        resp_204 = MagicMock(status_code=204)
        mock_session.get.return_value = resp_204

        result = fetch_contributor_stats("owner/repo", token="fake-token", retries=3)

        assert result == []
        assert mock_session.get.call_count == 3

    @patch("src.github.api._session")
    def test_200_empty_returns_empty(self, mock_session):
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = []
        mock_session.get.return_value = resp_200

        result = fetch_contributor_stats("owner/repo", token="fake-token", retries=5)

        assert result == []
        assert mock_session.get.call_count == 1


class TestFetchStatsOnce:
    @staticmethod
    def _make_resp(status, data=None):
        r = AsyncMock()
        r.status = status
        if data is not None:
            r.json = AsyncMock(return_value=data)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    @pytest.mark.asyncio
    async def test_200_returns_data(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        data = [{"author": {"login": "alice"}, "total": 10, "weeks": []}]
        mock_limiter.get = AsyncMock(return_value=self._make_resp(200, data))
        mock_session = AsyncMock(spec=["get"])

        result = await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")
        assert len(result) == 1
        assert result[0]["author"]["login"] == "alice"

    @pytest.mark.asyncio
    async def test_202_raises_deferred(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_limiter.get = AsyncMock(return_value=self._make_resp(202))
        mock_session = AsyncMock(spec=["get"])

        with pytest.raises(_Deferred):
            await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")

    @pytest.mark.asyncio
    async def test_204_raises_deferred(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_limiter.get = AsyncMock(return_value=self._make_resp(204))
        mock_session = AsyncMock(spec=["get"])

        with pytest.raises(_Deferred):
            await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")

    @pytest.mark.asyncio
    async def test_200_empty_raises_no_stats(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_limiter.get = AsyncMock(return_value=self._make_resp(200, []))
        mock_session = AsyncMock(spec=["get"])

        with pytest.raises(_NoStats):
            await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")

    @pytest.mark.asyncio
    async def test_404_raises_runtime_error(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_limiter.get = AsyncMock(return_value=self._make_resp(404))
        mock_session = AsyncMock(spec=["get"])

        with pytest.raises(RuntimeError, match="not found"):
            await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")

    @pytest.mark.asyncio
    async def test_no_stats_repo_like_googlesql(self):
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_session = AsyncMock(spec=["get"])

        mock_limiter.get = AsyncMock(return_value=self._make_resp(202))
        with pytest.raises(_Deferred):
            await _fetch_stats_once(mock_session, mock_limiter, "google/googlesql")

        mock_limiter.get = AsyncMock(return_value=self._make_resp(200, []))
        with pytest.raises(_NoStats):
            await _fetch_stats_once(mock_session, mock_limiter, "google/googlesql")

    @pytest.mark.asyncio
    async def test_connection_error_raises_deferred(self):
        import aiohttp
        mock_limiter = AsyncMock(spec=_AsyncRateLimiter)
        mock_limiter.get = AsyncMock(side_effect=aiohttp.ClientError())
        mock_session = AsyncMock(spec=["get"])

        with pytest.raises(_Deferred):
            await _fetch_stats_once(mock_session, mock_limiter, "owner/repo")
