"""Tests for src/github/fetch_sponsorships.py — outbound sponsoring extraction."""

from src.github.fetch_sponsorships import sponsoring_count, _is_fresh


def test_sponsoring_count_from_org():
    data = {"organization": {"sponsorshipsAsSponsor": {"totalCount": 39}}, "user": None}
    assert sponsoring_count(data) == 39


def test_sponsoring_count_from_user():
    data = {"user": {"sponsorshipsAsSponsor": {"totalCount": 12}}, "organization": None}
    assert sponsoring_count(data) == 12


def test_sponsoring_count_zero_and_missing():
    assert sponsoring_count({"user": None, "organization": None}) == 0
    assert sponsoring_count({}) == 0


def test_error_rows_never_fresh():
    recent = "2999-01-01T00:00:00+00:00"
    assert _is_fresh({"sponsoring_status": "error", "fetched_at": recent}, 90) is False
    assert _is_fresh({"sponsoring_status": "ok", "fetched_at": recent}, 90) is True
