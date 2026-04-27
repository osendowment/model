"""GitHub API interaction — sync and async HTTP fetching with token rotation."""

import asyncio
import logging
import os
import time

import aiohttp
import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def load_tokens() -> list[str]:
    """Load GitHub tokens from environment. Supports GITHUB_TOKENS (comma-separated) or GITHUB_TOKEN."""
    load_dotenv()
    tokens_str = os.environ.get("GITHUB_TOKENS", "")
    if tokens_str:
        return [t.strip() for t in tokens_str.split(",") if t.strip()]
    single = os.environ.get("GITHUB_TOKEN", "")
    return [single] if single else []


class TokenRevolver:
    """Rotates across multiple GitHub tokens, picking the one with the most remaining quota.

    Tracks X-RateLimit-Remaining and X-RateLimit-Reset per token.
    Always picks the token with the highest remaining calls.
    If all tokens are exhausted, sleeps until the earliest reset.
    """

    def __init__(self, tokens: list[str] | None = None) -> None:
        self._tokens = tokens if tokens is not None else load_tokens()
        # Per-token state: {token: (remaining, reset_at)}
        self._state: dict[str, tuple[int, float]] = {
            t: (5000, 0.0) for t in self._tokens
        }

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def best_token(self) -> str | None:
        """Return the token with the most remaining quota."""
        if not self._tokens:
            return None
        return max(self._tokens, key=lambda t: self._state[t][0])

    def update(self, token: str, remaining: int | None, reset_at: float | None) -> None:
        """Update rate limit state for a token from response headers."""
        prev_remaining, prev_reset = self._state.get(token, (5000, 0.0))
        r = remaining if remaining is not None else prev_remaining
        ra = reset_at if reset_at is not None else prev_reset
        self._state[token] = (r, ra)

    def wait_time(self) -> float:
        """Seconds to wait if all tokens are exhausted. 0 if any token has quota."""
        if not self._tokens:
            return 0
        best = self.best_token()
        remaining, _ = self._state[best]
        if remaining > 0:
            return 0
        # All exhausted — find earliest reset
        earliest_reset = min(self._state[t][1] for t in self._tokens)
        return max(earliest_reset - time.time() + 0.5, 0)

    def status(self) -> str:
        """Human-readable status of all tokens."""
        parts = []
        for i, t in enumerate(self._tokens):
            remaining, reset_at = self._state[t]
            parts.append(f"T{i+1}:{remaining}")
        return " ".join(parts)


# Global revolver instance — initialized lazily
_revolver: TokenRevolver | None = None


def get_revolver() -> TokenRevolver:
    """Get or create the global TokenRevolver."""
    global _revolver
    if _revolver is None:
        _revolver = TokenRevolver()
        if _revolver.token_count > 1:
            log.info("Token revolver: %d tokens loaded", _revolver.token_count)
        elif _revolver.token_count == 1:
            log.debug("Single GitHub token loaded")
        else:
            log.warning("No GitHub tokens found — API calls will be unauthenticated")
    return _revolver


# --- Sync API ---

# Reuse a single requests session for connection pooling + keep-alive
_session = requests.Session()
_session.headers.update({"Accept": "application/vnd.github+json"})


def _sync_request(url: str, **kwargs) -> requests.Response:
    """Make a sync request using the best available token."""
    revolver = get_revolver()
    token = revolver.best_token()
    if token:
        _session.headers["Authorization"] = f"Bearer {token}"
    resp = _session.get(url, timeout=30, **kwargs)
    # Update rate limit state
    if token:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        revolver.update(
            token,
            int(remaining) if remaining else None,
            float(reset) if reset else None,
        )
    return resp


def fetch_contributor_stats(
    repo: str, retries: int = 12,
) -> list[dict]:
    """Fetch contributor stats from GitHub API. Handles 202/204 (computing) responses.

    Returns [] for repos with no contributor stats (e.g. no commits with emails).

    Larger repos can take GitHub minutes to compute the stats response, returning
    202 until done. With retries=12 and the 30s cap, we wait up to ~3.5 minutes
    per repo before giving up — enough to catch most 202-resolutions in practice.
    """
    url = f"{GITHUB_API}/repos/{repo}/stats/contributors"

    for attempt in range(retries):
        log.debug("Fetching stats for %s (attempt %d)", repo, attempt + 1)
        resp = _sync_request(url)

        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data
            log.debug("%s: no contributor stats available", repo)
            return []

        if resp.status_code in (202, 204):
            wait = min(2.0 + attempt * 3, 30.0)
            log.debug("GitHub is computing stats (%d), retrying in %.1fs...", resp.status_code, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(
                f"Rate limited (remaining: {remaining}). Set GITHUB_TOKENS."
            )

        if resp.status_code == 404:
            raise RuntimeError(f"Repo not found: {repo}")

        resp.raise_for_status()

    log.warning("%s: no stats after %d retries, treating as empty", repo, retries)
    return []


# --- Async API ---


class _AsyncRateLimiter:
    """Rate limiter for async GitHub API calls, using TokenRevolver for rotation.

    Token selection is round-robin across the available `GITHUB_TOKENS`,
    re-picked under a brief lock per call. The actual HTTP request is sent
    *outside* the lock so N concurrent calls truly run in parallel — each
    one on a different token. State updates after the response are also
    locked briefly. Without this, `async with self._lock:` around the full
    HTTP roundtrip serialised every request behind one token, regardless
    of concurrency or how many tokens were configured.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._request_count = 0
        self._revolver = get_revolver()
        self._rr_idx = 0  # round-robin pointer
        if self._revolver.token_count >= 1:
            from rich.console import Console as _C
            _C().print(
                f"[dim]Token revolver: {self._revolver.token_count} token(s) "
                f"available[/dim]"
            )

    def _pick_token(self) -> str | None:
        """Round-robin across tokens (caller must hold the lock)."""
        tokens = self._revolver._tokens
        if not tokens:
            return None
        token = tokens[self._rr_idx % len(tokens)]
        self._rr_idx += 1
        return token

    async def get(self, session: aiohttp.ClientSession, url: str, **kwargs) -> aiohttp.ClientResponse:
        # 1. Brief lock: check exhaustion + pick a token round-robin
        async with self._lock:
            wait = self._revolver.wait_time()
            token = None
            if wait <= 0:
                token = self._pick_token()
        if wait > 0:
            log.info("All tokens exhausted, sleeping %.1fs (%s)", wait, self._revolver.status())
            await asyncio.sleep(wait)
            async with self._lock:
                token = self._pick_token()

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # 2. HTTP request OUTSIDE the lock — concurrent calls run in parallel,
        #    each on its own (round-robined) token.
        resp = await session.get(url, headers=headers, **kwargs)

        # 3. Brief lock: update revolver state from response headers.
        if token:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            async with self._lock:
                self._revolver.update(
                    token,
                    int(remaining) if remaining else None,
                    float(reset) if reset else None,
                )
                self._request_count += 1
        return resp

    @property
    def requests_made(self) -> int:
        return self._request_count

    @property
    def revolver(self) -> TokenRevolver:
        return self._revolver


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


def _parse_next_link(link_header: str) -> str | None:
    """Extract the URL marked rel=\"next\" from a GitHub Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if part.endswith('; rel="next"'):
            return part.split(";", 1)[0].strip().lstrip("<").rstrip(">")
    return None


def _parse_last_page(link_header: str) -> int | None:
    """Extract the page= integer from the rel=\"last\" entry of a Link header.

    GitHub uses this for cheap "total count" lookups: GET /commits?per_page=1
    returns one item and a Link header whose `rel="last"` URL has page=N
    where N is the exact total count. Same pattern applies to /contributors.
    """
    if not link_header:
        return None
    from urllib.parse import urlparse, parse_qs
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="last"' in part:
            url = part.split(";", 1)[0].strip().lstrip("<").rstrip(">")
            q = parse_qs(urlparse(url).query)
            pages = q.get("page", [])
            if pages:
                try:
                    return int(pages[0])
                except ValueError:
                    return None
    return None


async def _fetch_total_commits(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter, repo: str,
) -> int:
    """Return the total commits on the repo's default branch.

    Single API call: GET /commits?per_page=1 + Link header rel="last" page.
    Returns 0 for empty repos (HTTP 409). Includes only the default branch.
    """
    url = f"{GITHUB_API}/repos/{repo}/commits?per_page=1"
    try:
        resp = await limiter.get(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise _Deferred() from e
    async with resp:
        status = resp.status
        if status == 200:
            last = _parse_last_page(resp.headers.get("Link", ""))
            if last is not None:
                return last
            data = await resp.json()
            return len(data) if isinstance(data, list) else 0
        if status == 409:
            return 0
        if status == 404:
            raise RuntimeError(f"Repo not found: {repo}")
        if status == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(f"Rate limited ({repo}, remaining: {remaining})")
        raise RuntimeError(f"{repo}: HTTP {status}")


async def _fetch_total_contributors(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter, repo: str,
) -> int:
    """Return total distinct contributors (incl. anonymous), via Link header.

    Single API call: GET /contributors?per_page=1&anon=true + Link rel="last".
    Without `anon=true` GitHub silently caps the list at the first 500 GitHub-
    linked accounts. With anon, page count reflects every distinct contributor
    email/login, even ones never linked to a GitHub user.
    """
    url = f"{GITHUB_API}/repos/{repo}/contributors?per_page=1&anon=true"
    try:
        resp = await limiter.get(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise _Deferred() from e
    async with resp:
        status = resp.status
        if status == 204:
            return 0
        if status == 200:
            last = _parse_last_page(resp.headers.get("Link", ""))
            if last is not None:
                return last
            data = await resp.json()
            return len(data) if isinstance(data, list) else 0
        if status == 404:
            raise RuntimeError(f"Repo not found: {repo}")
        if status == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            raise RuntimeError(f"Rate limited ({repo}, remaining: {remaining})")
        raise RuntimeError(f"{repo}: HTTP {status}")


async def _fetch_contributors_paginated(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter,
    repo: str, max_pages: int = 10,
) -> list[dict]:
    """Fetch all contributors via /repos/{owner}/{repo}/contributors.

    Returns the flat list of {login, id, contributions, type, ...} dicts.
    Unlike /stats/contributors (which suffers from a forever-202 caching
    pathology for many repos), this endpoint returns immediately for any
    repo that has at least one commit. GitHub caps the response at the
    top 500 contributors but that is well above the BF/HHI signal floor.

    Raises:
        _Deferred: transient network/timeout error (caller may retry).
        RuntimeError: 4xx/5xx status. 404 == repo deleted/private.
    """
    out: list[dict] = []
    url = f"{GITHUB_API}/repos/{repo}/contributors?per_page=100&anon=false"
    for _ in range(max_pages):
        try:
            resp = await limiter.get(session, url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise _Deferred() from e
        async with resp:
            status = resp.status
            if status == 200:
                page = await resp.json()
                if not page:
                    break
                out.extend(page)
                nxt = _parse_next_link(resp.headers.get("Link", ""))
                if not nxt:
                    break
                url = nxt
                continue
            if status == 204:
                # Repo is empty (no commits).
                return []
            if status == 403:
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                raise RuntimeError(f"Rate limited ({repo}, remaining: {remaining})")
            if status == 404:
                raise RuntimeError(f"Repo not found: {repo}")
            raise RuntimeError(f"{repo}: HTTP {status}")
    return out
