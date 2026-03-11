#!/usr/bin/env python3
"""Search GitHub repos by language and min stars, bypassing 1K result cap via created_at cohorts.

Uses async HTTP with serialized rate limiter driven by GitHub's X-RateLimit headers.
Caches search counts in data/search-counts.json to skip range-building on repeat runs.

Usage:
    python -m src.search --language C --min-stars 1000 --output data/c-repos.csv
    python -m src.search --language "C++" --min-stars 1000 --output data/cpp-repos.csv
"""

import argparse
import asyncio
import csv
import datetime
import json
import os
import time
import logging

import aiohttp
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()
TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    raise RuntimeError("GITHUB_TOKEN not found")

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
COUNTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "search-counts.json")


class GitHubRateLimiter:
    """Serialized rate limiter driven by GitHub's X-RateLimit-* response headers."""

    def __init__(self) -> None:
        self._remaining: int | None = None
        self._reset_at: float = 0
        self._lock = asyncio.Lock()
        self._request_count = 0

    async def request(self, session: aiohttp.ClientSession, **kwargs) -> aiohttp.ClientResponse:
        async with self._lock:
            if self._remaining is not None and self._remaining <= 1:
                wait = self._reset_at - time.time() + 0.5
                if wait > 0:
                    log.info("Rate limit: %d remaining, sleeping %.1fs", self._remaining, wait)
                    await asyncio.sleep(wait)
            resp = await session.get(**kwargs)
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


_limiter = GitHubRateLimiter()


async def _get(session: aiohttp.ClientSession, query: str, page: int = 1) -> dict:
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page}
    for _ in range(5):
        resp = await _limiter.request(session, url=API_URL, params=params)
        async with resp:
            if resp.status == 403:
                reset = int(resp.headers.get("X-RateLimit-Reset", 0))
                wait = max(reset - int(time.time()), 2)
                log.warning("Rate limited (403), waiting %ds...", wait)
                await asyncio.sleep(wait)
                continue
            if resp.status == 422:
                log.warning("422 for query (page %d), skipping", page)
                return {"total_count": 0, "items": []}
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("Rate limit retries exhausted")


# --- Count cache ---

def _load_counts_cache() -> dict:
    if os.path.exists(COUNTS_FILE):
        with open(COUNTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_counts_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(COUNTS_FILE)), exist_ok=True)
    with open(COUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def _cache_key(language: str, min_stars: int, date_from: str, date_to: str) -> str:
    return f"{language}|{min_stars}|{date_from}|{date_to}"


async def _count(session: aiohttp.ClientSession, language: str, min_stars: int,
                 date_from: str, date_to: str, cache: dict) -> int:
    key = _cache_key(language, min_stars, date_from, date_to)
    if key in cache:
        return cache[key]
    q = f"stars:>={min_stars} language:{language} created:{date_from}..{date_to}"
    data = await _get(session, q)
    total = data.get("total_count", 0)
    cache[key] = total
    return total


# --- Range building ---

async def _build_ranges(session: aiohttp.ClientSession, language: str,
                        min_stars: int, cache: dict) -> list[tuple[str, str]]:
    """Build created_at ranges where each has <=1000 results, splitting as needed."""
    today = datetime.date.today()
    years = []
    for year in range(2007, today.year + 1):
        start = f"{year}-01-01"
        end = f"{year}-12-31" if year < today.year else today.isoformat()
        years.append((start, end))

    # Count all years (cached ones are instant, others hit API serially)
    counts = await asyncio.gather(*[
        _count(session, language, min_stars, s, e, cache) for s, e in years
    ])

    result: list[tuple[str, str]] = []
    to_split: list[tuple[str, str, int]] = []

    for (start, end), total in zip(years, counts):
        if total == 0:
            continue
        if total <= 1000:
            result.append((start, end))
            log.info("  %s..%s → %d repos", start, end, total)
        else:
            to_split.append((start, end, total))

    while to_split:
        split_tasks = []
        for start, end, total in to_split:
            s = datetime.date.fromisoformat(start)
            e = datetime.date.fromisoformat(end)
            mid = s + (e - s) // 2
            mid_next = mid + datetime.timedelta(days=1)
            log.info("  %s..%s → %d repos (>1000), splitting at %s", start, end, total, mid)
            split_tasks.append((start, mid.isoformat()))
            split_tasks.append((mid_next.isoformat(), end))

        split_counts = await asyncio.gather(*[
            _count(session, language, min_stars, s, e, cache) for s, e in split_tasks
        ])

        to_split = []
        for (start, end), total in zip(split_tasks, split_counts):
            if total == 0:
                continue
            if total <= 1000:
                result.append((start, end))
                log.info("  %s..%s → %d repos", start, end, total)
            else:
                to_split.append((start, end, total))

    result.sort()
    return result


# --- Fetching ---

async def _fetch_range(session: aiohttp.ClientSession, language: str, min_stars: int,
                       date_from: str, date_to: str) -> list[dict]:
    q = f"stars:>={min_stars} language:{language} created:{date_from}..{date_to}"
    items_all = []
    for page in range(1, 11):
        data = await _get(session, q, page)
        items = data.get("items", [])
        if not items:
            break
        items_all.extend(items)
        if len(items) < 100:
            break
    return items_all


async def search(language: str, min_stars: int, output: str) -> None:
    t_start = time.monotonic()
    print(f"Searching: language={language} stars>={min_stars}\n")

    cache = _load_counts_cache()
    cached_before = len(cache)

    headers = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        print("Building date ranges...")
        ranges = await _build_ranges(session, language, min_stars, cache)
        print(f"  {len(ranges)} ranges to fetch\n")

        # Save updated cache
        if len(cache) > cached_before:
            _save_counts_cache(cache)
            log.info("Saved %d count entries to cache", len(cache))

        # Fetch all ranges (serialized by rate limiter, but pipelined via gather)
        results = await asyncio.gather(*[
            _fetch_range(session, language, min_stars, df, dt) for df, dt in ranges
        ])

    repos: dict[str, dict] = {}
    fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, ((date_from, date_to), items) in enumerate(zip(ranges, results)):
        added = 0
        for item in items:
            slug = item["full_name"].lower()
            if slug not in repos:
                desc = (item.get("description") or "").replace("\n", " ").replace("\r", " ")
                license_data = item.get("license") or {}
                owner_data = item.get("owner") or {}
                topics = item.get("topics") or []
                repos[slug] = {
                    # identity
                    "repo": slug,
                    "repo_id": item["id"],
                    "repo_node_id": item.get("node_id", ""),
                    # owner
                    "user_name": (owner_data.get("login") or "").lower(),
                    "user_id": owner_data.get("id", 0),
                    "user_node_id": owner_data.get("node_id", ""),
                    "user_type": owner_data.get("type", ""),
                    # metadata
                    "description": desc,
                    "homepage": item.get("homepage") or "",
                    "default_branch": (item.get("default_branch") or "").lower(),
                    "license": license_data.get("key") or "",
                    "language": (item.get("language") or "").lower(),
                    "topics": ",".join(topics),
                    # metrics
                    "size": item.get("size", 0),
                    "stars": item["stargazers_count"],
                    "forks": item["forks_count"],
                    "open_issues": item.get("open_issues_count", 0),
                    # flags
                    "fork": item.get("fork", False),
                    "has_downloads": item.get("has_downloads", False),
                    "archived": item.get("archived", False),
                    "disabled": item.get("disabled", False),
                    # timestamps
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("updated_at", ""),
                    "pushed_at": item.get("pushed_at", ""),
                    "fetched_at": fetched_at,
                }
                added += 1
        print(f"  [{i+1}/{len(ranges)}] {date_from}..{date_to} +{added} (total: {len(repos)})")

    sorted_repos = sorted(repos.values(), key=lambda r: r["stars"], reverse=True)

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted_repos)

    elapsed = time.monotonic() - t_start
    print(f"\nDone! {len(sorted_repos)} repos → {output} ({elapsed:.1f}s, {_limiter.requests_made} API calls)")
    if sorted_repos:
        print(f"  Top: {sorted_repos[0]['repo']} ({sorted_repos[0]['stars']:,} stars)")


def main():
    parser = argparse.ArgumentParser(description="Search GitHub repos by language and min stars")
    parser.add_argument("--language", required=True, help="Language filter (e.g. C, C++, Rust)")
    parser.add_argument("--min-stars", type=int, default=1000, help="Minimum stars (default: 1000)")
    parser.add_argument("--output", required=True, help="Output CSV path")
    args = parser.parse_args()

    asyncio.run(search(args.language, args.min_stars, args.output))


if __name__ == "__main__":
    main()
