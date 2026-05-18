"""Fetch issue counts (opened, closed) per repo per year via GitHub Search API.

Uses /search/issues with `is:issue created:Y-01-01..Y-12-31` and
`is:issue closed:Y-01-01..Y-12-31` qualifiers to get total_count directly.
Excludes pull requests via `is:issue`.

Filtered to repos with value_class A or B in data/value-data.csv that have a
non-empty github_repo. Only fetches missing (repo, year, state) cells unless
`--force` is given.

Search API rate limit: 30 requests/min/token. Multiple tokens are rotated.

Output (long format):
    data/github/issues.csv  — header: repo, repo_id, year, metric, value
    where metric ∈ {opened_issues, closed_issues}. One row per (repo, year,
    metric). Stable-sorted by (repo, year, metric). Smart upsert keyed on
    (repo, year, metric) — re-fetching a cell overwrites in place.

Usage:
    uv run python -m src.github.fetch_issue_metrics
    uv run python -m src.github.fetch_issue_metrics --years 2021 2025
    uv run python -m src.github.fetch_issue_metrics --limit 10
    uv run python -m src.github.fetch_issue_metrics --force --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import random
import time
from collections import defaultdict

import aiohttp
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from src.github.display import _ETAColumn, console
from src.github.github_client import GITHUB_API, get_revolver
from src.pipeline.common.repos import VALUE_FILE, load_repo_ids, load_risk_slugs

log = logging.getLogger(__name__)

# Source of truth for the A/B universe is data/value-data.csv (handled by
# src.repos.load_ab_slugs, which also skips archived repos).
INPUT_FILE = VALUE_FILE
OUTPUT_FILE = "data/github/issues.csv"
# Internal "state" name → long-format metric name.
STATE_METRICS = {"opened": "opened_issues", "closed": "closed_issues"}
LONG_FIELDS = ["repo", "repo_id", "year", "metric", "value"]
DEFAULT_YEARS = (2021, 2025)
SEARCH_LIMIT_PER_MIN = 30  # GitHub search-API authenticated quota per token
# 5% safety margin — stays just under 30/min/token so we never get a 429 from
# the primary limit and don't trigger the secondary abuse heuristic.
PACING_BUFFER = 1.05
TOKEN_INTERVAL = (60.0 / SEARCH_LIMIT_PER_MIN) * PACING_BUFFER  # 2.1s/token


# --- long-format CSV I/O ----------------------------------------------------
# Internal shape used by the fetcher loop:
#     rows_by_state[state][repo][year_str] = value_str
# Persistence layer reads this from / writes this to a long-format CSV with
# header `repo, repo_id, year, metric, value` where metric encodes the state.
# Smart upsert: keyed on (repo, year, metric); re-fetching a cell overwrites
# in place. Output is stable-sorted by (repo, year, metric).



def _load_long(path: str) -> tuple[dict[str, dict[str, dict[str, str]]], set[str]]:
    """Load long-format issues.csv into rows_by_state[state][repo][year] = value.

    Returns (rows_by_state, years_seen). Initialises both states with empty
    dicts so callers can safely index `rows_by_state[state]`.
    """
    rows_by_state: dict[str, dict[str, dict[str, str]]] = {s: {} for s in STATE_METRICS}
    metric_to_state = {m: s for s, m in STATE_METRICS.items()}
    years_seen: set[str] = set()
    if not os.path.exists(path):
        return rows_by_state, years_seen
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip()
            year = (row.get("year") or "").strip()
            metric = (row.get("metric") or "").strip()
            value = (row.get("value") or "").strip()
            if not repo or not year or value == "":
                continue
            state = metric_to_state.get(metric)
            if state is None:
                continue  # unknown metric — leave it alone (we'll re-write it below)
            rows_by_state[state].setdefault(repo, {})[year] = value
            years_seen.add(year)
    return rows_by_state, years_seen


def _write_long(
    path: str,
    rows_by_state: dict[str, dict[str, dict[str, str]]],
    repo_ids: dict[str, str],
    extra_rows: list[dict[str, str]] | None = None,
) -> int:
    """Write long-format issues.csv stable-sorted by (repo, year, metric).

    `extra_rows` is for any rows we read but couldn't classify (unknown
    metric) — they're round-tripped untouched. Returns rows written.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out_rows: list[dict[str, str]] = []
    for state, by_repo in rows_by_state.items():
        metric = STATE_METRICS[state]
        for repo, year_map in by_repo.items():
            rid = repo_ids.get(repo, "")
            for year, value in year_map.items():
                if value == "":
                    continue
                out_rows.append({
                    "repo": repo,
                    "repo_id": rid,
                    "year": year,
                    "metric": metric,
                    "value": value,
                })
    if extra_rows:
        out_rows.extend(extra_rows)
    out_rows.sort(key=lambda r: (r["repo"], r["year"], r["metric"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    return len(out_rows)


def _load_extra_rows(path: str) -> list[dict[str, str]]:
    """Round-trip any rows whose metric isn't in STATE_METRICS (forward-compat)."""
    if not os.path.exists(path):
        return []
    known = set(STATE_METRICS.values())
    out: list[dict[str, str]] = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            metric = (row.get("metric") or "").strip()
            if metric and metric not in known:
                out.append({k: row.get(k, "") for k in LONG_FIELDS})
    return out


def find_missing(
    repos: list[str],
    years: list[int],
    rows_by_state: dict[str, dict[str, dict[str, str]]],
    *,
    force: bool = False,
) -> list[tuple[str, str, int]]:
    """Return list of (repo, state, year) cells that have no value yet.

    Cells are grouped by repo, then state, then year so that the asyncio
    scheduler dispatches one repo's cells contiguously. With the per-repo
    `pending[repo]` counter in fetch_all, this means repos complete (and the
    progress bar advances) on a steady cadence rather than only after the
    entire 'opened' phase of all repos finishes.

    If `force` is True, every cell is treated as missing (re-fetch all).
    """
    missing: list[tuple[str, str, int]] = []
    for repo in repos:
        for state in STATE_METRICS:
            row = rows_by_state[state].get(repo, {})
            for y in years:
                if force or not row.get(str(y)):
                    missing.append((repo, state, y))
    return missing


# --- search-API rate limiter ------------------------------------------------
#
# Smooth per-token pacing instead of bursts: each token can fire at most once
# every TOKEN_INTERVAL seconds (2.1s with the 5% buffer), so over the wall-clock
# minute each token issues ~28 requests and never trips the 30/min limit.
# With 4 tokens this gives a steady ~115 req/min total, no sawtooth.


class _AsyncSearchRateLimiter:
    """Paced rate limiter for /search/issues with per-token rotation.

    Each token has its own `next_send_at` timestamp. Acquisition picks the
    token whose `next_send_at` is earliest, sleeps until that moment if it's
    in the future, dispatches, and pushes that token's next slot to
    `now + TOKEN_INTERVAL`. Cooled tokens (after 403/429) are skipped until
    their cooldown ends.

    Mirrors github_client._AsyncRateLimiter at the boundary (a `get()` method
    returning (response, token)), but does not use the optimistic-decrement
    pattern — pacing is the source of truth, not GitHub-reported `remaining`.
    """

    def __init__(self) -> None:
        self._tokens: list[str] = list(get_revolver()._tokens)
        now = time.time()
        # Stagger startup so all 4 tokens don't fire at the same instant.
        # Token i is first available at now + i * (interval / N), giving a
        # uniformly-spaced opening burst.
        self._next_send: dict[str, float] = {
            t: now + i * (TOKEN_INTERVAL / max(len(self._tokens), 1))
            for i, t in enumerate(self._tokens)
        }
        # Cooldown after 403/429 — skip the token until this time.
        self._cooled_until: dict[str, float] = {t: 0.0 for t in self._tokens}
        # Last-known GitHub-reported remaining, for the status display only.
        self._remaining: dict[str, int] = {t: SEARCH_LIMIT_PER_MIN for t in self._tokens}
        self._lock = asyncio.Lock()
        self._sleeping_until: float = 0.0
        self._requests = 0

    def _earliest_token(self) -> tuple[float, str | None]:
        """Token whose next-available time is soonest. Returns (when, token)."""
        if not self._tokens:
            return (0.0, None)
        best_t = self._tokens[0]
        best_at = max(self._next_send[best_t], self._cooled_until[best_t])
        for t in self._tokens[1:]:
            at = max(self._next_send[t], self._cooled_until[t])
            if at < best_at:
                best_at, best_t = at, t
        return best_at, best_t

    async def get(self, session: aiohttp.ClientSession, url: str,
                  params: dict | None = None) -> tuple[aiohttp.ClientResponse, str | None]:
        """Send one paced search request. Returns (response, token_used)."""
        async with self._lock:
            available_at, token = self._earliest_token()
            now = time.time()
            wait = available_at - now
            if wait > 0:
                self._sleeping_until = available_at
                log.debug("Pacing: sleeping %.2fs until token %s ready", wait,
                          token[:8] if token else "?")
                await asyncio.sleep(wait)
                self._sleeping_until = 0.0
                now = time.time()
            if token is not None:
                self._next_send[token] = now + TOKEN_INTERVAL

        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await session.get(url, headers=headers, params=params)

        # Track remaining for the status display only — pacing already prevents
        # exhaustion, so we don't drive scheduling off this number.
        if token and resp.headers.get("X-RateLimit-Resource") == "search":
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                self._remaining[token] = int(remaining)
        self._requests += 1
        return resp, token

    def cool_token(self, token: str | None, seconds: float) -> None:
        """Skip this token for `seconds` (after 403/429 secondary rate limit)."""
        if token is None or token not in self._cooled_until:
            return
        self._cooled_until[token] = max(self._cooled_until[token],
                                        time.time() + seconds)

    def status(self) -> str:
        """Compact status: 'T:28/29/29/30' or 'wait Ns' when paced."""
        if self._sleeping_until:
            remaining = max(int(self._sleeping_until - time.time()), 0)
            if remaining > 0:
                return f"wait {remaining}s"
        return "T:" + "/".join(str(self._remaining[t]) for t in self._tokens)

    @property
    def requests_made(self) -> int:
        return self._requests


# --- fetch ------------------------------------------------------------------


def _build_query(repo: str, state: str, year: int) -> str:
    """GitHub search query for a repo + state + year."""
    qualifier = "created" if state == "opened" else "closed"
    range_ = f"{year}-01-01..{year}-12-31"
    return f"repo:{repo} is:issue {qualifier}:{range_}"


async def _fetch_one(
    session: aiohttp.ClientSession,
    limiter: _AsyncSearchRateLimiter,
    repo: str,
    state: str,
    year: int,
    retries: int = 6,
) -> int | None:
    """Fetch total_count for one (repo, state, year). Returns None on failure."""
    params = {"q": _build_query(repo, state, year), "per_page": "1"}
    url = f"{GITHUB_API}/search/issues"
    for attempt in range(retries):
        try:
            resp, token = await limiter.get(session, url, params=params)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("%s/%s/%d: network error: %s", repo, state, year, e)
            await asyncio.sleep(2 ** attempt)
            continue
        async with resp:
            if resp.status == 200:
                data = await resp.json()
                return int(data.get("total_count", 0))
            if resp.status in (403, 429):
                # Secondary rate limit — cool the token so the limiter rotates away
                wait = float(resp.headers.get("Retry-After", 30))
                limiter.cool_token(token, wait)
                log.debug("%s/%s/%d: %d secondary, cooling token %.0fs",
                          repo, state, year, resp.status, wait)
                continue
            if resp.status == 422:
                # Search rejected the query (repo too new, blocked, etc.)
                log.debug("%s/%s/%d: 422 invalid query", repo, state, year)
                return None
            if resp.status == 404:
                log.debug("%s/%s/%d: 404 repo not found", repo, state, year)
                return None
            log.warning("%s/%s/%d: HTTP %d", repo, state, year, resp.status)
            await asyncio.sleep(2 ** attempt)
    return None


async def fetch_all(
    missing: list[tuple[str, str, int]],
    rows_by_state: dict[str, dict[str, dict[str, str]]],
    output_path: str,
    repo_ids: dict[str, str],
    extra_rows: list[dict[str, str]],
    concurrency: int = 4,
) -> tuple[int, int]:
    """Fetch all missing cells and upsert per-repo as soon as a repo finishes.

    Returns (ok, errors). A repo is "finished" when all its remaining cells
    (across both states × all needed years) have been attempted. The long CSV
    is rewritten on every repo completion, so a Ctrl-C loses ≤ N_concurrent
    repos worth of in-flight work.
    """
    limiter = _AsyncSearchRateLimiter()
    sem = asyncio.Semaphore(concurrency)
    ok = 0
    errors = 0

    # Count outstanding cells per repo so we know when to flush + advance the bar.
    pending: dict[str, int] = defaultdict(int)
    for repo, _, _ in missing:
        pending[repo] += 1
    n_repos = len(pending)

    flush_lock = asyncio.Lock()

    def _flush() -> None:
        _write_long(output_path, rows_by_state, repo_ids, extra_rows)

    # Slim layout for narrow terminals: spinner + bar + count + ETA + status.
    # Bar tracks REPOS completed (not individual cells), so the count matches
    # the universe shown in the banner.
    progress = Progress(
        SpinnerColumn(),
        BarColumn(bar_width=12),
        TextColumn("[dim]{task.completed}/{task.total} repos[/]"),
        _ETAColumn(),
        TextColumn("[dim]{task.description}[/]"),
        console=console,
    )

    async with aiohttp.ClientSession() as session:
        with progress:
            task = progress.add_task(limiter.status(), total=n_repos)

            async def _ticker() -> None:
                """Refresh status text once a second so the wait countdown ticks."""
                while True:
                    progress.update(task, description=limiter.status())
                    await asyncio.sleep(1)

            async def _run(repo: str, state: str, year: int) -> None:
                nonlocal ok, errors
                async with sem:
                    count = await _fetch_one(session, limiter, repo, state, year)
                if count is None:
                    errors += 1
                else:
                    rows_by_state[state].setdefault(repo, {})[str(year)] = str(count)
                    ok += 1
                pending[repo] -= 1
                if pending[repo] == 0:
                    # Last cell of this repo done — upsert and advance the bar.
                    async with flush_lock:
                        _flush()
                    progress.update(task, advance=1, description=limiter.status())
                else:
                    # Still mid-repo — refresh status text only.
                    progress.update(task, description=limiter.status())

            ticker = asyncio.create_task(_ticker())
            try:
                await asyncio.gather(*[_run(r, s, y) for r, s, y in missing])
            finally:
                ticker.cancel()
                # Belt-and-suspenders flush on Ctrl-C / unexpected exit.
                async with flush_lock:
                    _flush()

    return ok, errors


# --- main -------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", default=INPUT_FILE,
                        help=f"value-data CSV (default: {INPUT_FILE} — "
                             f"filters to value_class A/B with non-empty github_repo)")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output long-format CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--repos-file", default="data/github/repos.csv",
                        help="repos.csv for repo→repo_id lookup (default: data/github/repos.csv)")
    parser.add_argument("--years", type=int, nargs=2, metavar=("START", "END"),
                        default=list(DEFAULT_YEARS),
                        help=f"Year range, inclusive (default: {DEFAULT_YEARS[0]}–{DEFAULT_YEARS[1]})")
    parser.add_argument("--limit", type=int,
                        help="Process only N random risk-scope repos (for testing)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all (repo, year, state) cells in range, "
                             "overwriting existing values.")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Concurrent in-flight requests (default: 4)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.verbose:
        logging.getLogger("aiohttp").setLevel(logging.WARNING)

    year_start, year_end = args.years
    years_int = list(range(year_start, year_end + 1))

    all_repos = load_risk_slugs(value_file=args.input)

    repo_ids = load_repo_ids(repos_file=args.repos_file)
    rows_by_state, _existing_years = _load_long(args.output)
    extra_rows = _load_extra_rows(args.output)

    # Find repos with at least one missing cell — these are the fetch candidates.
    if args.force:
        # In force mode every cell is "missing"; candidate repos = all repos.
        incomplete = sorted(all_repos)
    else:
        incomplete = sorted({r for r, _, _ in find_missing(all_repos, years_int, rows_by_state)})
    complete_count = len(all_repos) - len(incomplete)

    # --limit applies only to incomplete repos so every run makes progress.
    if args.limit and args.limit < len(incomplete):
        repos = random.sample(incomplete, args.limit)
    else:
        repos = incomplete

    missing = find_missing(repos, years_int, rows_by_state, force=args.force)

    total_cells = len(all_repos) * len(years_int) * 2
    filled_cells = total_cells - len(find_missing(all_repos, years_int, rows_by_state))

    revolver = get_revolver()
    mode = " [yellow](--force)[/]" if args.force else ""
    console.print(f"[bold]Issue counts {year_start}–{year_end}[/bold]{mode}")
    console.print(f"[dim]Repos:  {len(all_repos):,} risk-scope | "
                  f"{complete_count:,} complete | {len(incomplete):,} incomplete | "
                  f"this run: {len(repos):,}[/dim]")
    console.print(f"[dim]Cells:  {total_cells:,} total | "
                  f"{filled_cells:,} filled | {len(missing):,} {'forced' if args.force else 'missing'} this run[/dim]")
    console.print(f"[dim]Tokens: {revolver.token_count} | "
                  f"Search budget: {revolver.token_count * SEARCH_LIMIT_PER_MIN}/min[/dim]")
    console.print()

    if not missing:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    t_start = time.monotonic()
    ok, errors = asyncio.run(fetch_all(
        missing, rows_by_state, args.output, repo_ids, extra_rows,
        concurrency=args.concurrency,
    ))
    elapsed = time.monotonic() - t_start

    # Summary
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=12)
    summary.add_column()
    summary.add_row("Fetched", f"[bold]{ok:,}[/bold] cells "
                    f"[dim]in {elapsed:.1f}s | {ok * 60 / elapsed:.0f} cells/min[/dim]"
                    if elapsed else f"[bold]{ok:,}[/bold] cells")
    if errors:
        summary.add_row("Errors", f"[red]{errors:,}[/red]")
    summary.add_row("Output", args.output)
    console.print(summary)


if __name__ == "__main__":
    main()
