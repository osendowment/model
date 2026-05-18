"""Tests for src/github/fetch_repo_owner_data.py.

Focus: `_fetch_repo`'s handling of GitHub's 301/302 rename redirects.
A repo can be renamed more than once, so the redirect may be a *chain* —
each hop must be followed until a terminal 200/404, not just one.

The GitHub API is faked: `FakeLimiter` serves a scripted sequence of
`FakeResponse` objects, so no network is touched.
"""

from __future__ import annotations

from src.github.fetch_repo_owner_data import _fetch_repo


class FakeResponse:
    """Minimal stand-in for an aiohttp response (async context manager)."""

    def __init__(self, status: int, *, location: str | None = None,
                 json_body: dict | None = None, text_body: str = "") -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        if location is not None:
            self.headers["Location"] = location
        self._json = json_body or {}
        self._text = text_body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def json(self) -> dict:
        return self._json

    async def text(self) -> str:
        return self._text


class FakeLimiter:
    """Serves a scripted queue of responses, one per `get` call."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.n = 0

    async def get(self, session, url: str) -> FakeResponse:
        self.calls.append(url)
        self.n += 1
        return self._responses.pop(0)


def _ok_body(repo_id: int, full_name: str) -> dict:
    return {"id": repo_id, "full_name": full_name, "owner": {}}


# ── single-hop behaviour (regression guards for existing behaviour) ──────────

class TestSingleRedirect:
    async def test_single_redirect_resolves_to_200(self):
        limiter = FakeLimiter([
            FakeResponse(301, location="https://api.github.com/repositories/1"),
            FakeResponse(200, json_body=_ok_body(1, "new/name")),
        ])
        slug, row, status = await _fetch_repo(limiter, None, "old/name")
        assert status == "ok"
        assert slug == "old/name"
        assert row is not None
        assert row["repo"] == "old/name"        # row stays keyed by what we asked
        assert row["full_name"] == "new/name"   # current name from the payload

    async def test_single_redirect_to_deleted_repo_is_404(self):
        limiter = FakeLimiter([
            FakeResponse(301, location="https://api.github.com/repositories/1"),
            FakeResponse(404),
        ])
        slug, row, status = await _fetch_repo(limiter, None, "old/x")
        assert status == "404"
        assert row == {"repo": "old/x", "valid": False, "fetched_at": row["fetched_at"]}


# ── multi-hop chains (the new behaviour) ─────────────────────────────────────

class TestRedirectChain:
    async def test_follows_multi_hop_chain_to_final_repo(self):
        # Renamed twice: old/name → mid/name → new/name.
        limiter = FakeLimiter([
            FakeResponse(301, location="https://api.github.com/repositories/1"),
            FakeResponse(302, location="https://api.github.com/repositories/2"),
            FakeResponse(200, json_body=_ok_body(999, "new/name")),
        ])
        slug, row, status = await _fetch_repo(limiter, None, "old/name")
        assert status == "ok"
        assert row is not None
        assert row["repo"] == "old/name"
        assert row["full_name"] == "new/name"
        assert row["repo_id"] == 999
        assert len(limiter.calls) == 3   # all three hops were followed

    async def test_multi_hop_chain_ending_in_404(self):
        limiter = FakeLimiter([
            FakeResponse(301, location="a"),
            FakeResponse(301, location="b"),
            FakeResponse(404),
        ])
        slug, row, status = await _fetch_repo(limiter, None, "old/y")
        assert status == "404"
        assert row["valid"] is False

    async def test_redirect_chain_beyond_cap_gives_up(self):
        # An unbounded chain (or a redirect loop) must not hang — it is
        # capped and reported as `redirect_loop`, not retried forever.
        limiter = FakeLimiter([
            FakeResponse(301, location=f"hop{i}") for i in range(12)
        ])
        slug, row, status = await _fetch_repo(limiter, None, "loop/repo")
        assert row is None
        assert status == "redirect_loop"
