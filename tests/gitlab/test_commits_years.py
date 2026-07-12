"""Tests for gitlab/commits_years — per-year last-SHA anchor."""
from __future__ import annotations

import csv

from src.sources.gitlab.commits_years import YEARS, _fetch_year, _pairs_to_fetch


class FakeResp:
    def __init__(self, status, json_body=None, headers=None):
        self.status = status
        self._json = json_body if json_body is not None else []
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json


class FakeLimiter:
    """Serves a queued sequence of responses, one per GET."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = []

    async def get(self, session, host, url):
        self.calls.append(url)
        return self._queue.pop(0) if self._queue else FakeResp(200, [])


def _page(ids, next_page=0):
    """A 200 page of commits. GitLab serves NO X-Total on this endpoint — only
    X-Next-Page — which is the whole reason the count must be walked."""
    headers = {"X-Next-Page": str(next_page)} if next_page else {"X-Next-Page": ""}
    return FakeResp(200, [{"id": i} for i in ids], headers=headers)


class TestFetchYear:
    """The count must come from walking pages, never from X-Total.

    Regression: the fetcher used per_page=1 and read the `X-Total` header for
    the count. GitLab's commits endpoint does not send X-Total, so the count
    silently fell back to len(data) == 1 for EVERY repo-year — gnome/glib read
    as 1 commit/year instead of ~1288.
    """

    async def test_counts_across_pages_and_pins_boundary_shas(self):
        lim = FakeLimiter(
            _page(["newest", "b", "c"], next_page=2),
            _page(["d", "e", "oldest"]),
        )
        first, last, commits, ok = await _fetch_year(
            lim, None, "gitlab.com", "g/p", "main", 2024)
        assert ok is True
        assert commits == 6            # summed over BOTH pages, not 1
        assert last == "newest"        # page 1, item 0 → year's newest commit
        assert first == "oldest"       # final page, last item → year's oldest
        assert len(lim.calls) == 2
        assert "since=2024-01-01" in lim.calls[0]
        assert "until=2024-12-31" in lim.calls[0]
        assert "per_page=100" in lim.calls[0]

    async def test_ignores_x_total_header(self):
        # Even if a host DID send X-Total, the walked count is authoritative.
        resp = _page(["a", "b"])
        resp.headers["X-Total"] = "9999"
        _, _, commits, ok = await _fetch_year(
            FakeLimiter(resp), None, "gitlab.com", "g/p", "main", 2024)
        assert ok is True and commits == 2

    async def test_empty_year_is_a_real_zero(self):
        first, last, commits, ok = await _fetch_year(
            FakeLimiter(_page([])), None, "gitlab.com", "g/p", "main", 2019)
        assert (first, last, commits, ok) == ("", "", 0, True)

    async def test_retries_transient_500_then_succeeds(self, monkeypatch):
        # gitlab.gnome.org answers a tripped unauthenticated throttle with 500.
        import src.sources.gitlab.commits_years as mod
        monkeypatch.setattr(mod, "RETRY_BASE_DELAY", 0)
        lim = FakeLimiter(FakeResp(500), FakeResp(429), _page(["a", "b", "c"]))
        first, last, commits, ok = await _fetch_year(
            lim, None, "gitlab.gnome.org", "gnome/glib", "main", 2025)
        assert ok is True and commits == 3
        assert last == "a" and first == "c"
        assert len(lim.calls) == 3     # two retries, then the good page

    async def test_persistent_failure_writes_nothing(self, monkeypatch):
        # Never persist a truncated count — a wrong number is worse than a gap.
        import src.sources.gitlab.commits_years as mod
        monkeypatch.setattr(mod, "RETRY_BASE_DELAY", 0)
        lim = FakeLimiter(*[FakeResp(500) for _ in range(mod.MAX_RETRIES)])
        assert await _fetch_year(lim, None, "h", "g/p", "main", 2025) == ("", "", 0, False)

    async def test_mid_walk_failure_discards_partial_count(self, monkeypatch):
        # Page 1 OK, page 2 dies → the 3 commits already seen must NOT be kept.
        import src.sources.gitlab.commits_years as mod
        monkeypatch.setattr(mod, "RETRY_BASE_DELAY", 0)
        lim = FakeLimiter(_page(["a", "b", "c"], next_page=2),
                          *[FakeResp(503) for _ in range(mod.MAX_RETRIES)])
        assert await _fetch_year(lim, None, "h", "g/p", "main", 2025) == ("", "", 0, False)

    async def test_404_is_final_not_retried(self, monkeypatch):
        import src.sources.gitlab.commits_years as mod
        monkeypatch.setattr(mod, "RETRY_BASE_DELAY", 0)
        lim = FakeLimiter(FakeResp(404))
        assert await _fetch_year(lim, None, "h", "g/p", "main", 2025) == ("", "", 0, False)
        assert len(lim.calls) == 1     # no retry burnt on a real 404


class TestPairsToFetch:
    """Regression coverage for the per-(repo_id, year) selection bug: a repo
    with ANY existing row must not be skipped wholesale — only years already
    anchored for that repo_id should be skipped."""

    PROJECT = {"repo_id": "gl:123", "project": "g/p", "git_url": "https://gitlab.com/g/p.git",
               "host": "gitlab.com", "path": "g/p", "branch": "main"}

    def test_new_project_all_years(self):
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=set(), force=False)
        assert pairs == [(self.PROJECT, year) for year in YEARS]

    def test_skips_existing_year_keeps_missing(self):
        # Only 2021 is anchored for this repo_id — every other YEAR (including
        # ones added later, e.g. a new calendar year) must still be fetched.
        existing = {("gl:123", "2021")}
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=existing, force=False)
        years_selected = [year for _, year in pairs]
        assert 2021 not in years_selected
        assert years_selected == [y for y in YEARS if y != 2021]

    def test_force_selects_all(self):
        # Even with every year already present, --force must re-fetch all.
        existing = {("gl:123", str(y)) for y in YEARS}
        pairs = _pairs_to_fetch([self.PROJECT], existing_pairs=existing, force=True)
        assert [year for _, year in pairs] == YEARS


class TestLoadExistingPairs:
    def test_roundtrip(self, tmp_path, monkeypatch):
        import src.sources.gitlab.commits_years as mod

        out = tmp_path / "commits-years.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=mod.COMMITS_FIELDS)
            w.writeheader()
            w.writerow({"repo": "g/p", "repo_id": "gl:1", "git_url": "u",
                        "year": "2021", "first_sha": "", "last_sha": "abc",
                        "commits": "5", "fetched_at": "2026-01-01T00:00:00Z"})
            w.writerow({"repo": "g/p", "repo_id": "gl:1", "git_url": "u",
                        "year": "2022", "first_sha": "", "last_sha": "def",
                        "commits": "3", "fetched_at": "2026-01-01T00:00:00Z"})
            w.writerow({"repo": "g/q", "repo_id": "gl:2", "git_url": "u2",
                        "year": "2021", "first_sha": "", "last_sha": "xyz",
                        "commits": "1", "fetched_at": "2026-01-01T00:00:00Z"})
        monkeypatch.setattr(mod, "COMMITS_OUT", out)
        assert mod._load_existing_pairs() == {
            ("gl:1", "2021"), ("gl:1", "2022"), ("gl:2", "2021"),
        }
