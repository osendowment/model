"""Batch processing — async multi-repo fetching and CSV I/O."""

import asyncio
import csv as csv_mod
import datetime
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass

import aiohttp
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn, TimeElapsedColumn,
)
from rich.table import Table

from src.github.models import RunResult, THRESHOLD
from src.github.github_client import (
    _AsyncRateLimiter,
    _fetch_contributors_paginated,
    _fetch_total_commits,
    _fetch_total_contributors,
    _Deferred,
)
from src.github.display import console, _ETAColumn, _fmt_contribs_label

log = logging.getLogger(__name__)


# --- CSV I/O ---
#
# Contributor metrics are persisted to a single file, data/concentration-data.csv,
# one row per repo: repo, repo_id, total_commits, total_contributors,
# active_contributors, bus_factor, hhi, fetched_at.

# Per-repo concentration summary
CONCENTRATION_FILE = "data/concentration-data.csv"
CONCENTRATION_FIELDS = [
    "repo", "repo_id",
    "total_commits", "total_contributors",
    "bus_factor", "hhi", "fetched_at",
]
CONCENTRATION_TTL_DAYS = 90  # rows older than this get re-fetched
GH_REPOS_FILE = "data/github/repos.csv"


def _load_repo_id_map() -> dict[str, str]:
    """Return {repo_lowercased: numeric_repo_id} from data/github/repos.csv.

    Empty if the file doesn't exist — concentration-data.csv falls back
    to empty string for repo_id in that case.
    """
    if not os.path.exists(GH_REPOS_FILE):
        return {}
    out: dict[str, str] = {}
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for row in csv_mod.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            rid = (row.get("repo_id") or "").strip()
            if slug and rid:
                out[slug] = rid
    return out


def _recently_fetched_repos(ttl_days: int = CONCENTRATION_TTL_DAYS) -> set[str]:
    """Return repos in concentration-data.csv whose `fetched_at` is within TTL.

    Used as the freshness skip filter — repos with a fresh row in this file
    are considered up-to-date and don't need to be re-fetched. Bypassed by
    `--force` on the CLI.
    """
    if not os.path.exists(CONCENTRATION_FILE):
        return set()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    fresh: set[str] = set()
    with open(CONCENTRATION_FILE, encoding="utf-8") as f:
        for row in csv_mod.DictReader(f):
            ts_str = (row.get("fetched_at") or "").strip()
            repo = (row.get("repo") or "").strip()
            if not ts_str or not repo:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            if ts >= cutoff:
                fresh.add(repo)
    return fresh


def _upsert_concentration_data(
    batch: list[tuple[str, list[tuple[str, RunResult]]]],
    batch_labels: list[str],
) -> None:
    """Upsert per-repo concentration summary rows into data/concentration-data.csv.

    One row per repo with the bus-factor "weakest link":
        repo, repo_id, min_commits, min_contributor, bus_factor, hhi, fetched_at

    `min_contributor` is the login of the contributor with the lowest commit
    count among the bus-factor-many top contributors — i.e. the marginal
    person whose departure would still leave the project viable. `min_commits`
    is their commit count.
    """
    if not batch:
        return

    agg_label = batch_labels[0] if batch_labels else None
    if agg_label is None:
        return

    repo_ids = _load_repo_id_map()
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    existing: dict[str, dict[str, str]] = {}
    if os.path.exists(CONCENTRATION_FILE):
        with open(CONCENTRATION_FILE, encoding="utf-8") as f:
            for row in csv_mod.DictReader(f):
                existing[row["repo"]] = row

    for repo, year_results in batch:
        agg = next((r for label, r in year_results if label == agg_label), None)
        if agg is None or not agg.contributors:
            continue
        humans = [c for c in agg.contributors if not c.is_bot]
        if not humans or agg.bus_factor == 0:
            continue
        existing[repo] = {
            "repo": repo,
            "repo_id": repo_ids.get(repo, existing.get(repo, {}).get("repo_id", "")),
            "total_commits": str(agg.total_commits) if agg.total_commits is not None else "",
            "total_contributors": (
                str(agg.total_contributors) if agg.total_contributors is not None else ""
            ),
            "bus_factor": str(agg.bus_factor),
            "hhi": str(round(agg.hhi * 10000)),
            "fetched_at": fetched_at,
        }

    os.makedirs(os.path.dirname(CONCENTRATION_FILE) or ".", exist_ok=True)
    with open(CONCENTRATION_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=CONCENTRATION_FIELDS,
                                    extrasaction="ignore")
        writer.writeheader()
        for repo in sorted(existing):
            writer.writerow(existing[repo])


def _load_repos_from_csv(filepath: str) -> list[str]:
    """Load repo slugs from a CSV file (expects 'repo' column).

    Skips archived repos if the CSV has an 'archived' column.
    Sorts by stars descending if the CSV has a 'stars' column.
    """
    entries: list[tuple[str, int]] = []
    skipped_archived = 0
    with open(filepath, encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        fields = reader.fieldnames or []
        has_archived = "archived" in fields
        has_stars = "stars" in fields
        for row in reader:
            if has_archived and row.get("archived", "").strip().lower() in ("true", "1"):
                skipped_archived += 1
                continue
            repo = row.get("repo", "").strip().lower()
            if repo:
                stars = int(row.get("stars", 0) or 0) if has_stars else 0
                entries.append((repo, stars))
    if skipped_archived:
        log.info("Skipped %d archived repos", skipped_archived)
        console.print(f"[dim]Skipped {skipped_archived} archived repos[/dim]")
    if has_stars:
        entries.sort(key=lambda e: e[1], reverse=True)
    return [repo for repo, _ in entries]


# --- Async batch orchestration ---

async def batch_update(
    repos: list[str],
    threshold: float = THRESHOLD,
    include_bots: bool = False, limit: int | None = None,
    force: bool = False,
) -> None:
    """Fetch and update metrics for multiple repos in parallel.

    Uses GitHub's /repos/{owner}/{repo}/contributors endpoint (lifetime
    contribution totals). The historical /stats/contributors path was
    abandoned because it returns 202 forever for ~90% of repos, leaving
    contributor metrics empty. Results are written to concentration-data.csv.
    """
    import random
    from src.github.fetch_contributors_metrics import compute_lifetime_metrics

    t_start = time.monotonic()

    # Freshness gate: repos with a row in concentration-data.csv that's
    # younger than CONCENTRATION_TTL_DAYS are considered up-to-date and
    # skipped. `--force` bypasses this. Repos missing from the file (or
    # with stale timestamps) are re-fetched. This replaces the older
    # "any-data-present-in-bus-factor.csv" skip — that one couldn't
    # distinguish 1-day-old from 1-year-old data.
    fresh_repos = set() if force else _recently_fetched_repos()
    to_fetch = [r for r in repos if r not in fresh_repos]
    skipped = len(repos) - len(to_fetch)

    if limit and limit < len(to_fetch):
        to_fetch = random.sample(to_fetch, limit)

    console.print(f"[bold]Batch update[/bold]: {len(repos)} repos, "
                  f"{len(to_fetch)} to fetch, {skipped} skipped")
    if not to_fetch:
        console.print("[dim]Nothing to update.[/dim]")
        return

    headers = {"Accept": "application/vnd.github+json"}

    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(10)
    total_label = "2021-2025"
    # /contributors doesn't have the 202-forever pathology, so we don't
    # need multi-round retries. A single round with a small inner-retry
    # for transient network errors is sufficient.
    MAX_ROUNDS = 2
    ROUND_DELAY = 5

    @dataclass
    class _RepoResult:
        repo: str
        year_results: list[tuple[str, RunResult]] | None
        elapsed: float
        error: str = ""
        rounds: int = 1

    async def _try_one(repo: str):
        """Fetch /contributors + total commit count + total contributor count
        in parallel for one repo. Returns (repo, data, total_commits,
        total_contribs, error, elapsed)."""
        async with sem:
            t = time.monotonic()
            try:
                data, total_commits, total_contribs = await asyncio.gather(
                    _fetch_contributors_paginated(session, limiter, repo),
                    _fetch_total_commits(session, limiter, repo),
                    _fetch_total_contributors(session, limiter, repo),
                )
                return (repo, data, total_commits, total_contribs, "", time.monotonic() - t)
            except _Deferred:
                return (repo, None, None, None, "", time.monotonic() - t)
            except RuntimeError as e:
                return (repo, None, None, None, str(e), time.monotonic() - t)

    results: list[_RepoResult] = []
    repo_api_time: dict[str, float] = defaultdict(float)

    async with aiohttp.ClientSession(headers=headers) as session:
        max_name = max(len(r) for r in to_fetch) if to_fetch else 20
        name_width = min(max_name, 30)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}", justify="left"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            _ETAColumn(),
            console=console,
        ) as progress:
            pad = " " * name_width
            task = progress.add_task(f"{pad} ...", total=len(to_fetch))

            # Flush after every result so partial progress is durable on
            # disk and visible to readers immediately. The cost is one full
            # rewrite of each wide CSV per repo; on ~900-row files that's
            # ~1MB × 6 files per write, dwarfed by the GitHub API wait.
            FLUSH_EVERY = 1
            pending_flush: list[tuple[str, list[tuple[str, RunResult]]]] = []
            pending_repos = list(to_fetch)

            for round_num in range(MAX_ROUNDS):
                if not pending_repos:
                    break

                if round_num > 0:
                    progress.update(task,
                                    description=f"{'retry round ' + str(round_num + 1):>{name_width}} "
                                    f"({len(results)}/{len(to_fetch)})")
                    await asyncio.sleep(ROUND_DELAY)

                deferred: list[str] = []
                coros = [_try_one(repo) for repo in pending_repos]
                for coro in asyncio.as_completed(coros):
                    repo, data, total_commits, total_contribs, error, api_time = await coro
                    repo_api_time[repo] += api_time

                    if error:
                        results.append(_RepoResult(repo=repo, year_results=None,
                                                   elapsed=repo_api_time[repo], error=error,
                                                   rounds=round_num + 1))
                    elif data is None:
                        deferred.append(repo)
                        continue
                    else:
                        yr = compute_lifetime_metrics(
                            data, total_commits=total_commits,
                            total_contributors=total_contribs,
                            label=total_label,
                            threshold=threshold, include_bots=include_bots,
                        )
                        rr = _RepoResult(repo=repo, year_results=yr,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((rr.repo, rr.year_results))

                    if len(pending_flush) >= FLUSH_EVERY:
                        _upsert_concentration_data(pending_flush, ["2021-2025"])
                        pending_flush.clear()

                    label = repo[:name_width].ljust(name_width)
                    progress.update(task, completed=len(results),
                                    description=f"{label} ({len(results)}/{len(to_fetch)})")
                    # Periodic stdout marker so non-TTY (background) runs show progress
                    if len(results) % 25 == 0 or len(results) == len(to_fetch):
                        console.print(
                            f"[dim]{len(results)}/{len(to_fetch)}  ({repo})[/dim]"
                        )

                pending_repos = deferred
                if deferred and round_num < MAX_ROUNDS - 1:
                    log.info("Round %d done: %d resolved, %d deferred → retrying",
                             round_num + 1, len(results), len(deferred))

            for repo in pending_repos:
                yr = compute_lifetime_metrics(
                    [], total_commits=None, total_contributors=None,
                    label=total_label,
                    threshold=threshold, include_bots=include_bots,
                )
                rr = _RepoResult(repo=repo, year_results=yr,
                                 elapsed=repo_api_time[repo], rounds=MAX_ROUNDS)
                results.append(rr)
                pending_flush.append((rr.repo, rr.year_results))
                log.warning("%s: no contributors after %d rounds, treating as empty", repo, MAX_ROUNDS)

            if pending_flush:
                _upsert_concentration_data(pending_flush, ["2021-2025"])
                pending_flush.clear()

    # Results table
    errors = sum(1 for r in results if r.year_results is None)
    elapsed = time.monotonic() - t_start

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Repo", min_width=20)
    table.add_column("BF", justify="right")
    table.add_column("HHI", justify="right")
    table.add_column("Contribs", justify="right")
    table.add_column("Commits", justify="right")
    table.add_column("Time", justify="right", style="dim")

    results.sort(key=lambda r: to_fetch.index(r.repo))

    for i, rr in enumerate(results):
        is_last = i == len(results) - 1
        if rr.year_results is None:
            table.add_row(rr.repo, "[red]error[/red]", "", "", "", f"{rr.elapsed:.1f}s",
                          end_section=is_last)
            continue
        total_r = next((r for label, r in rr.year_results if label == total_label), None)
        if total_r is None:
            continue
        humans = [c for c in total_r.contributors if not c.is_bot]
        total_commits = sum(c.commits for c in humans)
        table.add_row(
            rr.repo,
            f"[yellow bold]{total_r.bus_factor}[/yellow bold]" if total_r.bus_factor > 0 else "[dim]–[/dim]",
            f"{round(total_r.hhi * 10000):,}" if total_r.hhi > 0 else "[dim]–[/dim]",
            _fmt_contribs_label(total_r.contributors) if humans else "[dim]–[/dim]",
            f"{total_commits:,}" if total_commits else "[dim]–[/dim]",
            f"{rr.elapsed:.1f}s",
            end_section=is_last,
        )

    ok_results = [r for r in results if r.year_results is not None]
    times = [r.elapsed for r in results]
    avg_time = sum(times) / len(times) if times else 0
    max_time = max(times) if times else 0
    min_time = min(times) if times else 0

    table.add_row(
        f"[dim]{len(ok_results)} repos[/dim]",
        "", "", "", "",
        f"[bold]{elapsed:.1f}s[/bold]",
    )
    table.add_row(
        "[dim]avg / min / max[/dim]",
        "", "", "", "",
        f"[dim]{avg_time:.1f} / {min_time:.1f} / {max_time:.1f}s[/dim]",
    )
    table.add_row(
        "[dim]API calls[/dim]",
        "", "", "", "",
        f"[dim]{limiter.requests_made}[/dim]",
    )
    table.add_row(
        "[dim]skipped / errors[/dim]",
        "", "", "", "",
        f"[dim]{skipped} / {errors}[/dim]",
    )

    console.print()
    console.print(table)

    problem_repos = [r for r in results if r.error or r.rounds >= 3]
    if problem_repos:
        console.print()
        ptable = Table(show_header=True, header_style="bold dim", padding=(0, 1),
                       title="[bold]Problematic repos[/bold]")
        ptable.add_column("Repo", min_width=20)
        ptable.add_column("Rounds", justify="right")
        ptable.add_column("Issue")
        for rr in problem_repos:
            if rr.error:
                issue = f"[red]{rr.error}[/red]"
            elif rr.rounds >= MAX_ROUNDS:
                issue = "[yellow]no stats after all rounds[/yellow]"
            else:
                issue = f"[dim]resolved after {rr.rounds} rounds[/dim]"
            ptable.add_row(rr.repo, str(rr.rounds), issue)
        console.print(ptable)
