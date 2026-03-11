#!/usr/bin/env python3
"""Search GitHub repos by language and min stars, bypassing 1K result cap via created_at cohorts.

Uses async HTTP with serialized rate limiter driven by GitHub's X-RateLimit headers.
Caches search counts in data/github/repo-search-counts.csv to skip range-building on repeat runs.

Usage:
    python -m src.github.search --language Python --min-stars 10000
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import os
import time
import logging

import aiohttp
from rich.progress import Progress, TaskID

from src.github.display import (
    console, fmt_step_summary, make_search_progress, display_search_summary,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

from src.github.api import get_revolver

API_URL = "https://api.github.com/search/repositories"
FIELDS = [
    # identity
    "repo", "repo_id", "repo_node_id",
    # owner
    "user_name", "user_id", "user_node_id", "user_type",
    # metadata
    "description", "homepage", "default_branch", "license", "language", "topics",
    # metrics
    "size", "stars", "forks", "open_issues",
    # flags
    "fork", "has_downloads", "archived", "disabled",
    # timestamps
    "created_at", "updated_at", "pushed_at", "fetched_at",
]
COUNTS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "github", "repo-search-counts.csv")
REPOS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "github", "top-repos.csv")
COUNTS_FIELDS = ["language", "min_stars", "date_from", "date_to", "count", "fetched_at"]


class GitHubRateLimiter:
    """Concurrent rate limiter: allows multiple in-flight requests while respecting GitHub limits.

    Uses a semaphore for concurrency and a gate lock for atomic rate-limit state checks.
    Exposes status_text for progress bars to display rate limit state.
    """

    def __init__(self, max_concurrent: int = 10) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._gate = asyncio.Lock()
        self._remaining: int | None = None
        self._reset_at: float = 0
        self._request_count = 0
        self.status_text: str = ""  # set by callers to update progress bar

    async def request(self, session: aiohttp.ClientSession, **kwargs) -> aiohttp.ClientResponse:
        async with self._semaphore:
            async with self._gate:
                if self._remaining is not None and self._remaining <= 1:
                    wait = self._reset_at - time.time() + 0.5
                    if wait > 0:
                        reset_time = datetime.datetime.fromtimestamp(
                            self._reset_at,
                        ).strftime("%H:%M:%S")
                        self.status_text = f"rate limit reset {reset_time}"
                        await asyncio.sleep(wait)
                        self.status_text = ""
                if self._remaining is not None:
                    self._remaining -= 1

            resp = await session.get(**kwargs)

            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            async with self._gate:
                if remaining is not None:
                    self._remaining = int(remaining)
                if reset is not None:
                    self._reset_at = float(reset)
                self._request_count += 1
            return resp

    @property
    def requests_made(self) -> int:
        return self._request_count


_limiter = GitHubRateLimiter()


async def _get(session: aiohttp.ClientSession, query: str, page: int = 1) -> dict:
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page}
    for _ in range(5):
        resp = await _limiter.request(session, url=API_URL, params=params)
        async with resp:
            if resp.status == 403:
                reset = int(resp.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset - int(time.time()), 2)
                reset_time = datetime.datetime.fromtimestamp(reset).strftime("%H:%M:%S")
                _limiter.status_text = f"rate limit reset {reset_time}"
                await asyncio.sleep(wait)
                _limiter.status_text = ""
                continue
            if resp.status == 422:
                log.warning("422 for query (page %d), skipping", page)
                return {"total_count": 0, "items": []}
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("Rate limit retries exhausted")


# --- Count cache ---

def _load_counts_cache() -> dict[str, tuple[int, str]]:
    """Load cache as {key: (count, fetched_at)}."""
    if os.path.exists(COUNTS_FILE):
        with open(COUNTS_FILE, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cache: dict[str, tuple[int, str]] = {}
            for row in reader:
                key = _cache_key(row["language"], int(row["min_stars"]),
                                 row["date_from"], row["date_to"])
                cache[key] = (int(row["count"]), row.get("fetched_at", ""))
            return cache
    return {}


def _save_counts_cache(cache: dict[str, tuple[int, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(COUNTS_FILE)), exist_ok=True)
    rows = []
    for key, (count, fetched_at) in cache.items():
        lang, min_stars, date_from, date_to = key.split("|")
        rows.append({"language": lang, "min_stars": min_stars,
                     "date_from": date_from, "date_to": date_to,
                     "count": count, "fetched_at": fetched_at})
    rows.sort(key=lambda r: (r["language"], r["date_from"]))
    with open(COUNTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COUNTS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _cache_key(language: str, min_stars: int, date_from: str, date_to: str) -> str:
    return f"{language}|{min_stars}|{date_from}|{date_to}"


async def _count(session: aiohttp.ClientSession, language: str, min_stars: int,
                 date_from: str, date_to: str,
                 cache: dict[str, tuple[int, str]]) -> tuple[int, list[dict]]:
    """Return (total_count, first_page_items). Uses cache for count but must re-fetch items."""
    key = _cache_key(language, min_stars, date_from, date_to)
    if key in cache:
        return cache[key][0], []
    q = f"stars:>={min_stars} language:{language} created:{date_from}..{date_to}"
    data = await _get(session, q)
    total = data.get("total_count", 0)
    items = data.get("items", [])
    fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache[key] = (total, fetched_at)
    return total, items


# --- Range building ---

def _initial_periods() -> list[tuple[str, str]]:
    """Build 5 initial 4-year periods starting from 2008 up to today."""
    today = datetime.date.today()
    periods = []
    for i in range(5):
        start_year = 2008 + i * 4
        end_year = start_year + 3
        start = f"{start_year}-01-01"
        end = today.isoformat() if end_year >= today.year else f"{end_year}-12-31"
        periods.append((start, end))
        if end_year >= today.year:
            break
    return periods


async def _count_and_advance(
    session: aiohttp.ClientSession, language: str, min_stars: int,
    date_from: str, date_to: str, cache: dict[str, tuple[int, str]],
    progress: Progress, task_id: TaskID,
) -> tuple[int, list[dict]]:
    total, items = await _count(session, language, min_stars, date_from, date_to, cache)
    progress.advance(task_id)
    return total, items


async def _build_ranges(
    session: aiohttp.ClientSession, language: str,
    min_stars: int, cache: dict[str, tuple[int, str]],
    progress: Progress,
    on_resolved: "Callable | None" = None,
) -> list[tuple[str, str]]:
    """Build created_at ranges where each has <=1000 results via binary splitting.

    Shows a progress bar with ETA for each phase. When a phase completes, the bar
    is replaced by a summary line. Calls on_resolved(language, start, end, prefetched)
    for each resolved range to enable pipelined fetching.
    """
    periods = _initial_periods()

    status_fn = getattr(progress, "_status_fn", None)

    # Phase 1: count initial periods
    task = progress.add_task(
        f"[cyan]{language}[/] ranges", total=len(periods), status_fn=status_fn,
    )
    t0 = time.monotonic()
    results = await asyncio.gather(*[
        _count_and_advance(session, language, min_stars, s, e, cache, progress, task)
        for s, e in periods
    ])
    elapsed = time.monotonic() - t0
    progress.remove_task(task)
    progress.console.print(fmt_step_summary(f"{language} ranges", len(periods), elapsed))

    resolved: list[tuple[str, str]] = []
    to_split: list[tuple[str, str, int]] = []

    for (start, end), (total, items) in zip(periods, results):
        if total == 0:
            continue
        if total <= 1000:
            resolved.append((start, end))
            if on_resolved:
                on_resolved(language, start, end, items)
        else:
            to_split.append((start, end, total))

    # Phase 2+: binary split rounds
    round_num = 0
    while to_split:
        round_num += 1
        halves = []
        for start, end, total in to_split:
            s = datetime.date.fromisoformat(start)
            e = datetime.date.fromisoformat(end)
            mid = s + (e - s) // 2
            mid_next = mid + datetime.timedelta(days=1)
            halves.append((start, mid.isoformat()))
            halves.append((mid_next.isoformat(), end))

        label = f"{language} split {round_num}"
        task = progress.add_task(
            f"[cyan]{language}[/] split {round_num}", total=len(halves), status_fn=status_fn,
        )
        t0 = time.monotonic()
        split_results = await asyncio.gather(*[
            _count_and_advance(session, language, min_stars, s, e, cache, progress, task)
            for s, e in halves
        ])
        elapsed = time.monotonic() - t0
        progress.remove_task(task)
        progress.console.print(fmt_step_summary(label, len(halves), elapsed))

        to_split = []
        for (start, end), (total, items) in zip(halves, split_results):
            if total == 0:
                continue
            if total <= 1000:
                resolved.append((start, end))
                if on_resolved:
                    on_resolved(language, start, end, items)
            else:
                to_split.append((start, end, total))

    resolved.sort()
    return resolved


# --- Fetching ---

async def _fetch_range(session: aiohttp.ClientSession, language: str, min_stars: int,
                       date_from: str, date_to: str,
                       prefetched: list[dict] | None = None) -> list[dict]:
    """Fetch all items for a range. Reuses prefetched first-page items if available."""
    q = f"stars:>={min_stars} language:{language} created:{date_from}..{date_to}"

    if prefetched:
        # Already have page 1 from the count call
        if len(prefetched) < 100:
            return prefetched  # all results fit in one page
        items_all = list(prefetched)
        start_page = 2
    else:
        items_all = []
        start_page = 1

    for page in range(start_page, 11):
        data = await _get(session, q, page)
        items = data.get("items", [])
        if not items:
            break
        items_all.extend(items)
        if len(items) < 100:
            break
    return items_all


def _upsert_repos(new_repos: dict[str, dict]) -> tuple[int, int, int]:
    """Upsert repos into data/github/top-repos.csv, keyed by repo_id.

    Returns (total, added, updated).
    """
    existing: dict[str, dict] = {}
    if os.path.exists(REPOS_FILE):
        with open(REPOS_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[str(row["repo_id"])] = row

    added = updated = 0
    for repo in new_repos.values():
        key = str(repo["repo_id"])
        if key in existing:
            updated += 1
        else:
            added += 1
        existing[key] = repo

    sorted_all = sorted(existing.values(), key=lambda r: int(r.get("stars", 0)), reverse=True)
    os.makedirs(os.path.dirname(os.path.abspath(REPOS_FILE)), exist_ok=True)
    with open(REPOS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted_all)

    return len(sorted_all), added, updated


def _parse_item(item: dict, fetched_at: str) -> tuple[str, dict]:
    """Parse a GitHub search result item into a (slug, row) tuple."""
    slug = item["full_name"].lower()
    desc = (item.get("description") or "").replace("\n", " ").replace("\r", " ")
    license_data = item.get("license") or {}
    owner_data = item.get("owner") or {}
    topics = item.get("topics") or []
    return slug, {
        "repo": slug,
        "repo_id": item["id"],
        "repo_node_id": item.get("node_id", ""),
        "user_name": (owner_data.get("login") or "").lower(),
        "user_id": owner_data.get("id", 0),
        "user_node_id": owner_data.get("node_id", ""),
        "user_type": owner_data.get("type", ""),
        "description": desc,
        "homepage": item.get("homepage") or "",
        "default_branch": (item.get("default_branch") or "").lower(),
        "license": license_data.get("key") or "",
        "language": (item.get("language") or "").lower(),
        "topics": ",".join(topics),
        "size": item.get("size", 0),
        "stars": item["stargazers_count"],
        "forks": item["forks_count"],
        "open_issues": item.get("open_issues_count", 0),
        "fork": item.get("fork", False),
        "has_downloads": item.get("has_downloads", False),
        "archived": item.get("archived", False),
        "disabled": item.get("disabled", False),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
        "pushed_at": item.get("pushed_at", ""),
        "fetched_at": fetched_at,
    }


async def search(languages: list[str], min_stars: int, output: str) -> None:
    t_start = time.monotonic()
    lang_label = ", ".join(languages)
    console.print(f"[bold]Search:[/] {lang_label}  stars >= {min_stars:,}\n")

    cache = _load_counts_cache()
    cached_before = len(cache)

    revolver = get_revolver()
    token = revolver.best_token()
    if not token:
        raise RuntimeError("No GitHub tokens found. Set GITHUB_TOKENS in .env")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    repos: dict[str, dict] = {}
    fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_tasks: list[asyncio.Task] = []
    fetch_total = 0
    fetch_t_start: float | None = None
    fetch_task_id: TaskID | None = None

    async with aiohttp.ClientSession(headers=headers) as session:
        def _rate_status() -> str:
            return _limiter.status_text

        progress = make_search_progress(status_fn=_rate_status)

        async def _fetch_and_collect(
            language: str, df: str, dt: str, prefetched: list[dict],
        ) -> None:
            items = await _fetch_range(session, language, min_stars, df, dt, prefetched)
            for item in items:
                slug, row = _parse_item(item, fetched_at)
                if slug not in repos:
                    repos[slug] = row
            if fetch_task_id is not None:
                progress.advance(fetch_task_id)
                progress.update(
                    fetch_task_id,
                    description=f"[green]fetching[/] {len(repos):,} repos",
                )

        def on_range_resolved(
            language: str, df: str, dt: str, prefetched: list[dict],
        ) -> None:
            nonlocal fetch_total, fetch_t_start, fetch_task_id
            fetch_total += 1
            if fetch_t_start is None:
                fetch_t_start = time.monotonic()
            if fetch_task_id is None:
                fetch_task_id = progress.add_task(
                    "[green]fetching[/]", total=fetch_total,
                    status_fn=_rate_status,
                )
            else:
                progress.update(fetch_task_id, total=fetch_total)
            fetch_tasks.append(asyncio.create_task(
                _fetch_and_collect(language, df, dt, prefetched),
            ))

        with progress:
            for language in languages:
                ranges = await _build_ranges(
                    session, language, min_stars, cache, progress,
                    on_resolved=on_range_resolved,
                )

            if len(cache) > cached_before:
                _save_counts_cache(cache)

            if fetch_tasks:
                await asyncio.gather(*fetch_tasks)

            # Replace fetch bar with summary line
            if fetch_task_id is not None and fetch_t_start is not None:
                fetch_elapsed = time.monotonic() - fetch_t_start
                progress.remove_task(fetch_task_id)
                progress.console.print(fmt_step_summary(
                    "fetching", fetch_total, fetch_elapsed,
                ))

    sorted_repos = sorted(repos.values(), key=lambda r: r["stars"], reverse=True)

    # Upsert BEFORE writing output — in case output == top-repos.csv
    total, added, updated = _upsert_repos(repos)
    repos_path = os.path.normpath(REPOS_FILE)

    if output:
        output_is_repos = (
            os.path.normpath(os.path.abspath(output))
            == os.path.normpath(os.path.abspath(REPOS_FILE))
        )
        if not output_is_repos:
            os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
            with open(output, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(sorted_repos)

    elapsed = time.monotonic() - t_start
    top = sorted_repos[0] if sorted_repos else None
    display_search_summary(
        repo_count=len(sorted_repos),
        elapsed=elapsed,
        api_calls=_limiter.requests_made,
        output=output,
        repos_path=repos_path,
        total=total,
        added=added,
        updated=updated,
        top_repo=top["repo"] if top else None,
        top_stars=top["stars"] if top else 0,
    )


def main():
    parser = argparse.ArgumentParser(description="Search GitHub repos by language and min stars")
    parser.add_argument("--language", nargs="+", required=True,
                        help="Language filter(s) (e.g. C C++ Rust)")
    parser.add_argument("--min-stars", type=int, default=1000, help="Minimum stars (default: 1000)")
    parser.add_argument("--output", default=None, help="Output CSV path (default: top-repos.csv only)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    asyncio.run(search(args.language, args.min_stars, args.output))


if __name__ == "__main__":
    main()
