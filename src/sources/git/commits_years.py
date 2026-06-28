"""Fetch per-(repo, year) first/last commit SHAs from the GitHub Commits API.

Output: ``data/sources/github/git/commits-years.csv`` with columns

    repo, year, first_sha, last_sha, commits, fetched_at

This file is foundational for all SHA-pinned analyses (scc, lizard,
semgrep, churn). Each downstream tool reads it to find a year-end
snapshot SHA per repo and pins its analysis to that commit.

Per (repo, year) we issue **two** GitHub API calls:

  Call A: GET /commits?since=Y-01-01&until=Y+1-01-01&per_page=1
    → newest commit IN year + total commits-in-year (parsed from
      Link header rel="last").
  Call B (only if commits > 1): same URL with &page={commits}
    → oldest commit IN year.

Inactive years (no commits) get empty SHAs and commits=0 — still
recorded, so we don't keep refetching them.

Usage:

    uv run python -m src.sources.git.commits_years                 # all risk-scope repos
    uv run python -m src.sources.git.commits_years --limit 10
    uv run python -m src.sources.git.commits_years --force         # re-fetch everything
    uv run python -m src.sources.git.commits_years --years 2024 2025
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import os
import random
from urllib.parse import parse_qs, quote, urlparse

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn,
)

from src.sources.github.display import _ETAColumn
from src.sources.github.github_client import GITHUB_API, get_revolver
from src.common.repos import load_default_branches, load_top_repos

log = logging.getLogger(__name__)
console = Console()

REPOS_FILE = "data/value/value.csv"
SHA_FILE = "data/sources/github/git/commits-years.csv"
DEFAULT_YEARS = [2021, 2022, 2023, 2024, 2025]
DEFAULT_CONCURRENCY = 32
FLUSH_EVERY = 500

SHA_FIELDS = ["repo", "year", "first_sha", "last_sha", "commits", "fetched_at"]


# ────────────────────────────── CSV I/O ────────────────────────────────

def load_sha_data(filepath: str) -> dict[tuple[str, str], dict[str, str]]:
    """Load commits-years.csv → {(repo, year_str): {first_sha, last_sha, commits, fetched_at}}.

    Backward-compatible: rows missing newer columns get empty defaults so
    we don't KeyError on legacy data.
    """
    data: dict[tuple[str, str], dict[str, str]] = {}
    if not os.path.exists(filepath):
        return data
    with open(filepath, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["repo"], row["year"])
            data[key] = {
                "first_sha": row.get("first_sha", ""),
                "last_sha":  row.get("last_sha", ""),
                "commits":   row.get("commits", ""),
                "fetched_at": row.get("fetched_at", ""),
            }
    return data


def write_sha_data(
    filepath: str,
    data: dict[tuple[str, str], dict[str, str]],
) -> None:
    """Write commits-years.csv sorted by (repo, year)."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHA_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for (repo, year), row in sorted(data.items()):
            writer.writerow({
                "repo": repo, "year": year,
                "first_sha": row.get("first_sha", ""),
                "last_sha":  row.get("last_sha", ""),
                "commits":   row.get("commits", ""),
                "fetched_at": row.get("fetched_at", ""),
            })


# ───────────────────────── snapshot SHA cascade ─────────────────────────

def resolve_snapshot_sha(
    sha_data: dict[tuple[str, str], dict[str, str]],
    repo: str, year: int,
) -> str:
    """Find a usable snapshot SHA for ``repo`` at end of ``year``.

    Per the schema, ``last_sha`` is the last commit IN that year and is
    empty when the repo had no commits in that year. For LOC /
    sparse-checkout purposes we still want the most-recent codebase
    state at-or-before year-end — so when ``last_sha`` is empty, walk
    back through earlier years (cap 10y) until we find a populated
    one. Returns ``""`` if no SHA found at all (project hadn't started
    yet, or empty repo).
    """
    for y in range(year, year - 10, -1):
        sha = sha_data.get((repo, str(y)), {}).get("last_sha") or ""
        if sha:
            return sha
    # Last resort: dormant repos with no commits in the year window have
    # a pseudo-row at year="HEAD" populated by `src.sources.git.resolve_head` —
    # use that so sha-pinned fetchers still get a snapshot to analyse.
    return sha_data.get((repo, "HEAD"), {}).get("last_sha") or ""


# ──────────────────────────── HTTP plumbing ─────────────────────────────

def _parse_link_last_page(link_header: str) -> int | None:
    """Extract ``page=N`` from a Link header's rel="last" entry. None if absent."""
    if not link_header:
        return None
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


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", justify="left"),
        BarColumn(bar_width=14),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        _ETAColumn(),
        console=console,
        transient=True,
    )


# ─────────────────────────── async fetcher core ─────────────────────────

async def fetch_commit_shas_multi(
    pairs: list[tuple[str, int]],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    sha_data: dict[tuple[str, str], dict[str, str]] | None = None,
    sha_file: str = "",
    default_branches: dict[str, str] | None = None,
) -> dict[tuple[str, int], dict[str, str]]:
    """For each ``(repo, year)`` pair, fetch first/last commit SHA + commits count.

    Per (repo, year):
      Call A: GET /commits?sha={default_branch}&since=Y-01-01&until=Y+1-01-01&per_page=1
        → newest commit IN year (first record) + commits-in-year
          (Link header rel="last" page).
      Call B (only if commits > 1): same URL with ``&page={commits}``
        → oldest commit IN year.

    ``default_branches`` maps repo → default branch; when a repo is known
    the query is pinned to its default branch with ``&sha=`` so we only
    record commits from the default branch (not, e.g., a stray PR branch).
    The downstream SHA-pinned fetchers additionally check each SHA is on
    the default branch's first-parent line (and recompute it if not) —
    see ``src.sources.git.clone.resolve_mainline_sha``.

    Returns ``{(repo, year): {"first_sha", "last_sha", "commits"}}``.
    Inactive years (no commits) get empty SHAs and commits=0.

    If ``sha_data`` and ``sha_file`` are both provided, results flush
    incrementally every ``FLUSH_EVERY`` fetches and once at the end so
    a crash doesn't lose progress.
    """
    revolver = get_revolver()
    sem = asyncio.Semaphore(concurrency)
    results: dict[tuple[str, int], dict[str, str]] = {}
    total = len(pairs)
    _fetched = 0
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    progress = _make_progress()
    name_width = min(max((len(r) for r, _ in pairs), default=20), 30)

    async def _request(session, url: str) -> tuple[int, list[dict], str]:
        """Make one authenticated request via the revolver. Returns (status, body, link)."""
        token = revolver.best_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await session.get(url, headers=headers)
        try:
            if token:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                revolver.update(
                    token,
                    int(remaining) if remaining else None,
                    float(reset) if reset else None,
                )
            link = resp.headers.get("Link", "")
            status = resp.status
            body = await resp.json() if status == 200 else []
        finally:
            await resp.release()
        return status, body, link

    async with aiohttp.ClientSession(
        headers={"Accept": "application/vnd.github+json"},
    ) as session:
        with progress:
            task = progress.add_task(" " * name_width, total=total)

            async def _fetch_one(repo: str, year: int) -> None:
                nonlocal _fetched
                since = f"{year}-01-01T00:00:00Z"
                until = f"{year + 1}-01-01T00:00:00Z"
                base = (
                    f"{GITHUB_API}/repos/{repo}/commits"
                    f"?since={since}&until={until}&per_page=1"
                )
                # Pin to the default branch so we never pick up a commit
                # from a stray ref. (GitHub can still leak a fork-network
                # sibling's commit even with &sha — the SHA-pinned fetchers
                # verify against the real branch as the hard guard.)
                branch = (default_branches or {}).get(repo)
                if branch:
                    base += f"&sha={quote(branch, safe='')}"
                first_sha, last_sha, commits = "", "", 0
                async with sem:
                    try:
                        status, body, link = await _request(session, base)
                        if status == 200 and body:
                            last_sha = body[0]["sha"]
                            commits = _parse_link_last_page(link) or len(body)
                            if commits > 1:
                                _, body2, _ = await _request(
                                    session, f"{base}&page={commits}",
                                )
                                if body2:
                                    first_sha = body2[0]["sha"]
                            else:
                                first_sha = last_sha  # 1 commit → first==last
                    except Exception as e:
                        log.debug("Failed to get SHAs for %s@%d: %s", repo, year, e)
                    finally:
                        results[(repo, year)] = {
                            "first_sha": first_sha,
                            "last_sha": last_sha,
                            "commits": str(commits),
                        }
                        if sha_data is not None:
                            sha_data[(repo, str(year))] = {
                                "first_sha": first_sha,
                                "last_sha": last_sha,
                                "commits": str(commits),
                                "fetched_at": fetched_at,
                            }
                        _fetched += 1
                        if (
                            sha_data is not None
                            and sha_file
                            and _fetched % FLUSH_EVERY == 0
                        ):
                            write_sha_data(sha_file, sha_data)
                        desc = repo[:name_width].ljust(name_width)
                        progress.update(task, advance=1, description=desc)

            await asyncio.gather(*[_fetch_one(r, y) for r, y in pairs])

    if sha_data is not None and sha_file:
        write_sha_data(sha_file, sha_data)

    return results


# ─────────────────────────────── CLI ────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch per-(repo, year) first/last commit SHAs from GitHub.",
    )
    parser.add_argument("--input", default=REPOS_FILE,
                        help=f"value-data CSV (default: {REPOS_FILE})")
    parser.add_argument("--sha-file", default=SHA_FILE,
                        help=f"Output CSV (default: {SHA_FILE})")
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                        help=f"Years to fetch (default: {DEFAULT_YEARS})")
    parser.add_argument("--limit", type=int,
                        help="Process N random risk-scope repos (for smoke tests).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all (repo, year) SHAs even if already cached.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    started = datetime.datetime.now()
    years = sorted(args.years)
    console.print()
    console.print(
        f"[bold]commits-years[/bold]  "
        f"[dim]years={years} · concurrency={args.concurrency} · "
        f"{started:%Y-%m-%d %H:%M:%S}[/dim]"
    )

    # Use the risk-scope set (A/B value classes from value-data.csv) as the
    # canonical fetch list.
    entries = load_top_repos()
    repos_all = [e.repo for e in entries]
    if args.limit:
        repos_all = random.sample(repos_all, min(args.limit, len(repos_all)))

    # Per-repo default branch — used to pin each Commits API query so we
    # only record default-branch commits. Empty when github/repos.csv is
    # missing (the query then falls back to the unpinned default).
    default_branches = load_default_branches()

    sha_data = {} if args.force else load_sha_data(args.sha_file)
    pairs_to_fetch: list[tuple[str, int]] = []
    for year in years:
        yr = str(year)
        for r in repos_all:
            if args.force or (r, yr) not in sha_data:
                pairs_to_fetch.append((r, year))

    if not pairs_to_fetch:
        console.print(
            f"[dim]All {len(repos_all)} repos × {len(years)} years already "
            f"cached in {args.sha_file}.[/dim]"
        )
        return

    console.print(
        f"[dim]fetching {len(pairs_to_fetch):,} (repo, year) SHAs from "
        f"{len(repos_all)} repos[/dim]"
    )
    console.print()

    asyncio.run(fetch_commit_shas_multi(
        pairs_to_fetch,
        concurrency=args.concurrency,
        sha_data=sha_data,
        sha_file=args.sha_file,
        default_branches=default_branches,
    ))

    # Final write so a crash mid-run doesn't keep the file behind
    write_sha_data(args.sha_file, sha_data)

    console.print()
    console.print(
        f"[green]wrote {len(sha_data):,} (repo, year) rows → {args.sha_file}[/green]"
    )


if __name__ == "__main__":
    main()
