"""GitHub API interaction — sync and async HTTP fetching with rate limiting."""

import asyncio
import logging
import time

import aiohttp
import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Reuse a single requests session for connection pooling + keep-alive
_session = requests.Session()
_session.headers.update({"Accept": "application/vnd.github+json"})


def _init_session(token: str | None = None) -> None:
    """Set auth token on the shared session."""
    if token:
        _session.headers["Authorization"] = f"Bearer {token}"


def fetch_repo_info(repo: str, token: str | None = None) -> tuple[str, int]:
    """Fetch default branch and repo size (KB) from GitHub API."""
    _init_session(token)
    url = f"{GITHUB_API}/repos/{repo}"
    resp = _session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    branch = data.get("default_branch", "main")
    size_kb = data.get("size", 0)
    log.debug("Default branch for %s: %s, size: %d KB", repo, branch, size_kb)
    return branch, size_kb


def fetch_contributor_stats(
    repo: str, token: str | None = None, retries: int = 6,
) -> list[dict]:
    """Fetch contributor stats from GitHub API. Handles 202/204 (computing) responses.

    Returns [] for repos with no contributor stats (e.g. no commits with emails).
    """
    _init_session(token)
    url = f"{GITHUB_API}/repos/{repo}/stats/contributors"

    for attempt in range(retries):
        log.debug("Fetching stats for %s (attempt %d)", repo, attempt + 1)
        resp = _session.get(url, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data
            log.debug("%s: no contributor stats available", repo)
            return []

        if resp.status_code in (202, 204):
            wait = min(2.0 + attempt * 2, 10.0)
            log.debug("GitHub is computing stats (%d), retrying in %.1fs...", resp.status_code, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(
                f"Rate limited (remaining: {remaining}). Set GITHUB_TOKEN."
            )

        if resp.status_code == 404:
            raise RuntimeError(f"Repo not found: {repo}")

        resp.raise_for_status()

    log.warning("%s: no stats after %d retries, treating as empty", repo, retries)
    return []


# --- Async API ---


class _AsyncRateLimiter:
    """Rate limiter for async GitHub API calls, driven by X-RateLimit-* headers."""

    def __init__(self) -> None:
        self._remaining: int | None = None
        self._reset_at: float = 0.0
        self._lock = asyncio.Lock()
        self._request_count = 0

    async def get(self, session: aiohttp.ClientSession, url: str, **kwargs) -> aiohttp.ClientResponse:
        async with self._lock:
            if self._remaining is not None and self._remaining <= 1:
                wait = self._reset_at - time.time() + 0.5
                if wait > 0:
                    log.info("Rate limit: sleeping %.1fs", wait)
                    await asyncio.sleep(wait)
            resp = await session.get(url, **kwargs)
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            if remaining is not None:
                self._remaining = int(remaining)
            if reset is not None:
                self._reset_at = float(reset)
            self._request_count += 1
            return resp

    @property
    def requests_made(self) -> int:
        return self._request_count


class _Deferred(Exception):
    """Raised when GitHub returns 202/204 — repo needs retry later."""


class _NoStats(Exception):
    """Raised when GitHub confirms no contributor stats exist (200 with empty body)."""


async def _fetch_stats_once(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter,
    repo: str,
) -> list[dict]:
    """Single attempt to fetch stats. Raises _Deferred on 202/204, _NoStats on empty 200."""
    url = f"{GITHUB_API}/repos/{repo}/stats/contributors"
    try:
        resp = await limiter.get(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise _Deferred() from e
    async with resp:
        if resp.status == 200:
            data = await resp.json()
            if data:
                return data
            raise _NoStats()
        if resp.status in (202, 204):
            raise _Deferred()
        if resp.status == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(f"Rate limited ({repo}, remaining: {remaining})")
        if resp.status == 404:
            raise RuntimeError(f"Repo not found: {repo}")
        raise RuntimeError(f"{repo}: HTTP {resp.status}")
