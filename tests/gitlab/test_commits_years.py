"""Tests for gitlab/commits_years — per-year last-SHA anchor."""
from __future__ import annotations

from src.sources.gitlab.commits_years import _fetch_year


class FakeResp:
    def __init__(self, status, json_body=None, headers=None):
        self.status = status
        self._json = json_body or []
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json


class FakeLimiter:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, session, host, url):
        self.calls.append(url)
        return self._resp


class TestFetchYear:
    async def test_returns_last_sha_and_commit_count(self):
        lim = FakeLimiter(FakeResp(200, json_body=[{"id": "deadbeef"}],
                                   headers={"X-Total": "42"}))
        sha, commits = await _fetch_year(lim, None, "gitlab.com", "g/p", "main", 2024)
        assert sha == "deadbeef"
        assert commits == 42
        assert "since=2024-01-01" in lim.calls[0]
        assert "until=2024-12-31" in lim.calls[0]

    async def test_empty_year_returns_blank(self):
        lim = FakeLimiter(FakeResp(200, json_body=[], headers={"X-Total": "0"}))
        sha, commits = await _fetch_year(lim, None, "gitlab.com", "g/p", "main", 2019)
        assert sha == ""
        assert commits == 0
