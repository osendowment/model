"""Tests for src/sources/git/commits_years.py — resolve_snapshot_sha."""

from src.sources.git.commits_years import resolve_snapshot_sha, SNAPSHOT_WALKBACK_YEARS


def test_resolve_snapshot_sha_walks_back_within_window():
    sha_data = {("o/r", "2023"): {"last_sha": "abc123"}}
    assert resolve_snapshot_sha(sha_data, "o/r", 2025) == "abc123"


def test_resolve_snapshot_sha_reaches_old_dated_fallback_no_head_needed():
    """A dormant repo's dated fallback year can be far older than the risk
    window (e.g. 2007) — the walk-back must reach it without any opaque
    "HEAD" pseudo-row."""
    sha_data = {("old/repo", "2007"): {"last_sha": "deadbeef"}}
    assert resolve_snapshot_sha(sha_data, "old/repo", 2025) == "deadbeef"


def test_resolve_snapshot_sha_returns_empty_beyond_walkback_cap():
    sha_data = {("ancient/repo", "1980"): {"last_sha": "toolold"}}
    assert resolve_snapshot_sha(sha_data, "ancient/repo", 2025) == ""


def test_resolve_snapshot_sha_ignores_stale_head_row():
    """A leftover legacy `year="HEAD"` row is never consulted — there is no
    fallback to an undated snapshot."""
    sha_data = {
        ("dorm/repo", "HEAD"): {"last_sha": "deadbeef"},
    }
    assert resolve_snapshot_sha(sha_data, "dorm/repo", 2025) == ""


def test_walkback_cap_covers_three_decades():
    assert SNAPSHOT_WALKBACK_YEARS >= 30
