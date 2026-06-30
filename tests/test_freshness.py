"""Tests for src/common/freshness.py — the shared funding-intent TTL gates."""

import datetime
import os

from src.common.freshness import (
    FUNDING_EMPTY_RECHECK_DAYS,
    FUNDING_TTL_DAYS,
    file_is_fresh,
    funding_ttl_for,
    row_is_fresh,
)


def _iso(days_ago: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return dt.isoformat(timespec="seconds")


def test_ttl_is_365():
    assert FUNDING_TTL_DAYS == 365


class TestRowIsFresh:
    def test_recent_is_fresh(self):
        assert row_is_fresh({"fetched_at": _iso(10)}) is True

    def test_just_inside_window(self):
        assert row_is_fresh({"fetched_at": _iso(364)}) is True

    def test_beyond_window_is_stale(self):
        assert row_is_fresh({"fetched_at": _iso(400)}) is False

    def test_custom_ttl(self):
        assert row_is_fresh({"fetched_at": _iso(45)}, ttl_days=30) is False
        assert row_is_fresh({"fetched_at": _iso(10)}, ttl_days=30) is True

    def test_missing_or_blank_timestamp_is_stale(self):
        assert row_is_fresh({}) is False
        assert row_is_fresh({"fetched_at": ""}) is False

    def test_malformed_timestamp_is_stale(self):
        assert row_is_fresh({"fetched_at": "not-a-date"}) is False

    def test_naive_timestamp_treated_as_utc(self):
        naive = (datetime.datetime.now() - datetime.timedelta(days=5)).isoformat()
        assert row_is_fresh({"fetched_at": naive}) is True

    def test_error_status_is_never_fresh(self):
        row = {"fetched_at": _iso(1), "oc_status": "error"}
        assert row_is_fresh(row, status_key="oc_status") is False
        # without the status_key it would be considered fresh
        assert row_is_fresh(row) is True

    def test_ok_status_stays_fresh(self):
        row = {"fetched_at": _iso(1), "oc_status": "ok"}
        assert row_is_fresh(row, status_key="oc_status") is True


class TestFundingTtlFor:
    def test_signal_gets_full_window(self):
        assert funding_ttl_for(True) == FUNDING_TTL_DAYS

    def test_empty_gets_short_window(self):
        assert funding_ttl_for(False) == FUNDING_EMPTY_RECHECK_DAYS
        assert FUNDING_EMPTY_RECHECK_DAYS < FUNDING_TTL_DAYS

    def test_empty_row_rechecked_sooner_than_signal_row(self):
        """At an age between the two windows, an empty result is stale (refetched)
        while a row carrying a signal is still fresh — the self-healing property."""
        age = (FUNDING_EMPTY_RECHECK_DAYS + FUNDING_TTL_DAYS) / 2  # e.g. ~197 days
        ts = {"fetched_at": _iso(age)}
        assert row_is_fresh(ts, funding_ttl_for(False)) is False   # empty → recheck
        assert row_is_fresh(ts, funding_ttl_for(True)) is True     # signal → cached


class TestFileIsFresh:
    def test_recent_file_is_fresh(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_text("x", encoding="utf-8")
        assert file_is_fresh(p) is True

    def test_missing_file_is_stale(self, tmp_path):
        assert file_is_fresh(tmp_path / "nope.csv") is False

    def test_zero_ttl_forces_refresh(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_text("x", encoding="utf-8")
        assert file_is_fresh(p, ttl_days=0) is False

    def test_old_file_is_stale(self, tmp_path):
        p = tmp_path / "out.csv"
        p.write_text("x", encoding="utf-8")
        old = time_now() - 400 * 86400
        os.utime(p, (old, old))
        assert file_is_fresh(p) is False


def time_now() -> float:
    import time
    return time.time()
