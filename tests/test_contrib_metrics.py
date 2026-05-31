"""Tests for contributor metrics — repo parsing + DateRange model."""

import datetime

import pytest

from src.sources.github.fetch_contributors_metrics import parse_repo
from src.sources.github.models import DateRange


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
