"""Tests for gitlab_client URL/host helpers — pure functions, no network."""
from __future__ import annotations

import re

import pytest

from src.sources.gitlab import gitlab_client as gc


class TestIsGitlabHost:
    def test_known_saas(self):
        assert gc.is_gitlab_host("gitlab.com")

    def test_known_self_hosted(self):
        assert gc.is_gitlab_host("salsa.debian.org")
        assert gc.is_gitlab_host("invent.kde.org")

    def test_gitlab_prefixed_host_is_detected(self):
        # Any gitlab.* subdomain counts as a GitLab instance.
        assert gc.is_gitlab_host("gitlab.gnome.org")
        assert gc.is_gitlab_host("gitlab.freedesktop.org")

    def test_github_is_not_gitlab(self):
        assert not gc.is_gitlab_host("github.com")


class TestParseGitUrl:
    def test_https_dot_git(self):
        assert gc.parse_git_url("https://salsa.debian.org/debian/foo.git") == (
            "salsa.debian.org", "debian/foo")

    def test_multilevel_namespace(self):
        assert gc.parse_git_url("https://gitlab.com/group/sub/proj") == (
            "gitlab.com", "group/sub/proj")

    def test_trailing_slash_and_case(self):
        assert gc.parse_git_url("https://gitlab.com/Group/Proj/") == (
            "gitlab.com", "Group/Proj")

    def test_non_gitlab_returns_none(self):
        assert gc.parse_git_url("https://github.com/npm/cli") is None

    def test_strips_web_suffix(self):
        assert gc.parse_git_url("https://salsa.debian.org/debian/foo/-/tree/master") == (
            "salsa.debian.org", "debian/foo")


class TestUrlBuilders:
    def test_encode_project_path(self):
        assert gc.encode_project_path("group/sub/proj") == "group%2Fsub%2Fproj"

    def test_api_base(self):
        assert gc.api_base("salsa.debian.org") == "https://salsa.debian.org/api/v4"

    def test_clone_url(self):
        assert gc.clone_url("gitlab.com", "group/proj") == "https://gitlab.com/group/proj.git"

    def test_make_repo_id(self):
        # self-hosted instances are namespaced by their HOST_NICKNAMES entry
        assert gc.make_repo_id("salsa.debian.org", 678) == "gl/debian-678"
        assert gc.make_repo_id("Gitlab.Gnome.org", 90) == "gl/gnome-90"  # lowercased
        assert gc.make_repo_id("invent.kde.org", 12) == "gl/kde-12"
        # gitlab.com is the canonical instance → bare gl/{id}
        assert gc.make_repo_id("gitlab.com", "278964") == "gl/278964"
        assert gc.make_repo_id("www.gitlab.com", 5) == "gl/5"

    def test_make_repo_id_unmapped_host_raises(self):
        # an id must never be invented for a host outside HOST_NICKNAMES —
        # it would change once the nickname is registered
        with pytest.raises(ValueError, match="HOST_NICKNAMES"):
            gc.make_repo_id("gitlab.example.org", 1)

    def test_host_nicknames_well_formed(self):
        # nicknames are unique, lowercase alphanumeric, non-numeric (a numeric
        # nickname would collide with bare gitlab.com ids)
        nicks = [n for n in gc.HOST_NICKNAMES.values() if n]
        assert len(nicks) == len(set(nicks))
        assert all(re.fullmatch(r"[a-z][a-z0-9]*", n) for n in nicks)
        assert gc.HOST_NICKNAMES["gitlab.com"] == ""


class FakeResp:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Captures the headers/url of each GET and returns a scripted response."""
    def __init__(self, resp):
        self._resp = resp
        self.last_headers = None
        self.last_url = None

    async def get(self, url, headers=None, timeout=None):
        self.last_url = url
        self.last_headers = headers
        return self._resp


class TestGitLabLimiter:
    def test_token_for_host(self):
        lim = gc.GitLabLimiter(token_map={"salsa.debian.org": "glpat-x"})
        assert lim.token_for("salsa.debian.org") == "glpat-x"
        assert lim.token_for("gitlab.com") is None

    async def test_get_sets_private_token_header_when_token_present(self):
        lim = gc.GitLabLimiter(token_map={"salsa.debian.org": "glpat-x"})
        sess = FakeSession(FakeResp(200, {"RateLimit-Remaining": "1999"}))
        async with await lim.get(sess, "salsa.debian.org", "https://salsa.debian.org/api/v4/version"):
            pass
        assert sess.last_headers["PRIVATE-TOKEN"] == "glpat-x"
        assert lim.n == 1

    async def test_get_omits_token_header_when_anonymous(self):
        lim = gc.GitLabLimiter(token_map={})
        sess = FakeSession(FakeResp(200, {}))
        async with await lim.get(sess, "gitlab.com", "https://gitlab.com/api/v4/version"):
            pass
        assert "PRIVATE-TOKEN" not in sess.last_headers


class TestGitLabLimiterBackoff:
    async def test_exhausted_host_sleeps_until_reset(self, monkeypatch):
        lim = gc.GitLabLimiter(token_map={})
        now = 1_000_000.0
        lim._state["gitlab.com"] = (0, now + 10)

        monkeypatch.setattr(gc.time, "time", lambda: now)
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)

        sess = FakeSession(FakeResp(200, {}))
        async with await lim.get(sess, "gitlab.com", "https://gitlab.com/api/v4/version"):
            pass

        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(10.5, abs=0.01)

    async def test_exhausted_host_with_no_reset_sleeps_the_floor(self, monkeypatch):
        lim = gc.GitLabLimiter(token_map={})
        lim._state["gitlab.com"] = (0, 0.0)

        monkeypatch.setattr(gc.time, "time", lambda: 1_000_000.0)
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)

        sess = FakeSession(FakeResp(200, {}))
        async with await lim.get(sess, "gitlab.com", "https://gitlab.com/api/v4/version"):
            pass

        # reset_at - now + 0.5 would be deeply negative here — the floor must win.
        assert sleeps == [gc.MIN_BACKOFF_S]

    async def test_state_updated_from_response_headers(self):
        lim = gc.GitLabLimiter(token_map={})
        sess = FakeSession(FakeResp(200, {
            "RateLimit-Remaining": "5", "RateLimit-Reset": "2000",
        }))
        async with await lim.get(sess, "gitlab.com", "https://gitlab.com/api/v4/version"):
            pass
        assert lim._state["gitlab.com"] == (5, 2000.0)

    async def test_backoff_sleep_does_not_hold_the_semaphore(self, monkeypatch):
        """Core fix: the exhausted host's sleep must not occupy a concurrency
        slot, or unrelated hosts get throttled behind it (head-of-line
        blocking). Assert the semaphore is fully available (full capacity)
        while the backoff sleep is in flight."""
        lim = gc.GitLabLimiter(token_map={}, max_concurrent=1)
        lim._state["gitlab.com"] = (0, 1_000_010.0)
        monkeypatch.setattr(gc.time, "time", lambda: 1_000_000.0)

        sem_value_during_sleep: list[int] = []

        async def fake_sleep(seconds):
            # If the semaphore were (incorrectly) held across the sleep,
            # its internal value would be 0 (fully acquired) right now.
            sem_value_during_sleep.append(lim._sem._value)

        monkeypatch.setattr(gc.asyncio, "sleep", fake_sleep)

        sess = FakeSession(FakeResp(200, {}))
        async with await lim.get(sess, "gitlab.com", "https://gitlab.com/api/v4/version"):
            pass

        assert sem_value_during_sleep == [1]  # full capacity — sem not held
