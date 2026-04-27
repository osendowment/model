"""Batch processing — async multi-repo fetching and CSV I/O."""

import asyncio
import csv as csv_mod
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
from src.github.github_client import _AsyncRateLimiter, _fetch_stats_once, _Deferred, _NoStats
from src.github.display import console, _ETAColumn, _fmt_date, _fmt_contribs_label

log = logging.getLogger(__name__)


# --- Metrics extractors for CSV ---

def _build_metrics_extractors() -> dict[str, object]:
    """Build metrics extractors for CSV output."""
    def _human_commits(r: RunResult) -> int:
        return sum(c.commits for c in r.contributors if not c.is_bot)

    return {
        "bus_factor": lambda r: str(r.bus_factor) if _human_commits(r) > 0 else "",
        "hhi": lambda r: str(round(r.hhi * 10000)) if _human_commits(r) > 0 else "",
        "contributors": lambda r: str(len([c for c in r.contributors if not c.is_bot])),
        "bots": lambda r: str(len([c for c in r.contributors if c.is_bot])),
        "commits": lambda r: str(_human_commits(r)),
        "first_date": lambda r: _fmt_date(r.first_week, ""),
        "last_date": lambda r: _fmt_date(r.last_week, ""),
    }


# --- CSV I/O ---
#
# Contributor metrics are split across multiple CSVs under a single directory:
#   {dir}/bus-factor.csv   — wide: repo, 2021..2025, 2021-2025
#   {dir}/hhi.csv          — wide: same shape
#   {dir}/contributors.csv — wide: same shape
#   {dir}/bots.csv         — wide: same shape
#   {dir}/years.csv        — long: repo, year, commits, first_date, last_date

WIDE_FILES = {
    "bus_factor":   "bus-factor.csv",
    "hhi":          "hhi.csv",
    "contributors": "contributors.csv",
    "bots":         "bots.csv",
    "commits":      "commits.csv",
}
YEARS_FILE = "years.csv"
YEARS_FIELDS = ["repo", "year", "first_date", "last_date"]


def _is_single_year(label: str) -> bool:
    """A year-range aggregate like '2021-2025' is not a single year."""
    return label.isdigit()


def _upsert_yearly_csv(
    dirpath: str, repo: str, year_results: list[tuple[str, RunResult]],
    quiet: bool = False,
) -> None:
    """Upsert yearly metrics for a single repo into the contributors directory."""
    _upsert_yearly_csv_batch(dirpath, [(repo, year_results)])
    if not quiet:
        console.print(f"[dim]Upserted {repo} in {dirpath}[/dim]")


def _upsert_yearly_csv_batch(
    dirpath: str, batch: list[tuple[str, list[tuple[str, RunResult]]]],
) -> None:
    """Upsert yearly metrics for multiple repos across the split CSVs."""
    if not batch:
        return

    os.makedirs(dirpath, exist_ok=True)
    metrics = _build_metrics_extractors()

    batch_labels: list[str] = []
    for _, year_results in batch:
        for label, _ in year_results:
            if label not in batch_labels:
                batch_labels.append(label)

    for metric_name, filename in WIDE_FILES.items():
        _upsert_wide_file(
            os.path.join(dirpath, filename),
            metric_name, metrics[metric_name], batch, batch_labels,
        )

    _upsert_years_file(os.path.join(dirpath, YEARS_FILE), batch, metrics)


def _upsert_wide_file(
    filepath: str, metric_name: str, extractor,
    batch: list[tuple[str, list[tuple[str, RunResult]]]],
    batch_labels: list[str],
) -> None:
    """Write one wide per-metric file, upserting rows for repos in the batch."""
    existing: dict[str, dict[str, str]] = {}
    existing_labels: list[str] = []
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            existing_labels = [fn for fn in (reader.fieldnames or []) if fn != "repo"]
            for row in reader:
                existing[row["repo"]] = dict(row)

    labels = list(existing_labels)
    for lbl in batch_labels:
        if lbl not in labels:
            labels.append(lbl)

    for repo, year_results in batch:
        values = {label: extractor(r) for label, r in year_results}
        merged = existing.get(repo, {"repo": repo})
        merged["repo"] = repo
        for lbl in labels:
            if lbl in values:
                merged[lbl] = values[lbl]
            elif lbl not in merged:
                merged[lbl] = ""
        existing[repo] = merged

    fieldnames = ["repo"] + labels
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for repo in sorted(existing):
            writer.writerow(existing[repo])


def _upsert_years_file(
    filepath: str,
    batch: list[tuple[str, list[tuple[str, RunResult]]]],
    metrics: dict,
) -> None:
    """Write the long years.csv (commits/first_date/last_date per repo-year)."""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            for row in csv_mod.DictReader(f):
                rows[(row["repo"], row["year"])] = row

    batch_repos = {repo for repo, _ in batch}
    rows = {k: v for k, v in rows.items() if k[0] not in batch_repos}

    for repo, year_results in batch:
        for label, r in year_results:
            if not _is_single_year(label):
                continue
            first = metrics["first_date"](r)
            last = metrics["last_date"](r)
            if not (first or last):
                continue
            rows[(repo, label)] = {
                "repo": repo, "year": label,
                "first_date": first, "last_date": last,
            }

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv_mod.DictWriter(f, fieldnames=YEARS_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow(rows[key])


def _read_existing_periods(dirpath: str) -> dict[str, set[str]]:
    """Read bus-factor.csv and return {repo: set of period labels populated for that repo}.

    Critically, only counts a period as "filled" if the cell has a non-empty
    value. Previously this returned the full set of column names for every
    repo that had a row, which silently skipped re-fetches for repos that
    had been recorded but produced no data (e.g. GitHub's /stats/contributors
    202-loop never resolved).
    """
    filepath = os.path.join(dirpath, WIDE_FILES["bus_factor"])
    if not os.path.exists(filepath):
        return {}
    result: dict[str, set[str]] = {}
    with open(filepath, encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        period_cols = [fn for fn in (reader.fieldnames or []) if fn != "repo"]
        for row in reader:
            filled = {p for p in period_cols if (row.get(p) or "").strip()}
            result[row["repo"]] = filled
    return result


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
    repos: list[str], year_start: int, year_end: int, output: str,
    threshold: float = THRESHOLD, base: str = "commits",
    include_bots: bool = False, limit: int | None = None,
) -> None:
    """Fetch and update metrics for multiple repos in parallel."""
    import random
    from src.github.fetch_contributors_metrics import compute_yearly_breakdown

    t_start = time.monotonic()

    existing = _read_existing_periods(output)
    needed_labels = {str(y) for y in range(year_start, year_end + 1)}
    needed_labels.add(f"{year_start}-{year_end}")

    to_fetch: list[str] = []
    skipped = 0
    for repo in repos:
        filled = existing.get(repo, set())
        missing = needed_labels - filled
        if not missing:
            skipped += 1
        else:
            to_fetch.append(repo)

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
    total_label = f"{year_start}-{year_end}"
    MAX_ROUNDS = 6
    ROUND_DELAY = 10

    @dataclass
    class _RepoResult:
        repo: str
        year_results: list[tuple[str, RunResult]] | None
        elapsed: float
        error: str = ""
        rounds: int = 1

    _NO_STATS = "__no_stats__"

    async def _try_one(repo: str) -> tuple[str, list[dict] | None, str, float]:
        async with sem:
            t = time.monotonic()
            try:
                data = await _fetch_stats_once(session, limiter, repo)
                return (repo, data, "", time.monotonic() - t)
            except _Deferred:
                return (repo, None, "", time.monotonic() - t)
            except _NoStats:
                return (repo, None, _NO_STATS, time.monotonic() - t)
            except RuntimeError as e:
                return (repo, None, str(e), time.monotonic() - t)

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

            FLUSH_EVERY = 50
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
                    repo, data, error, api_time = await coro
                    repo_api_time[repo] += api_time

                    if error and error != _NO_STATS:
                        results.append(_RepoResult(repo=repo, year_results=None,
                                                   elapsed=repo_api_time[repo], error=error,
                                                   rounds=round_num + 1))
                    elif error == _NO_STATS:
                        yr = compute_yearly_breakdown([], year_start, year_end,
                                                       threshold=threshold, base=base,
                                                       include_bots=include_bots)
                        rr = _RepoResult(repo=repo, year_results=yr,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((rr.repo, rr.year_results))
                    elif data is None:
                        deferred.append(repo)
                        continue
                    else:
                        yr = compute_yearly_breakdown(data, year_start, year_end,
                                                       threshold=threshold, base=base,
                                                       include_bots=include_bots)
                        rr = _RepoResult(repo=repo, year_results=yr,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((rr.repo, rr.year_results))

                    if len(pending_flush) >= FLUSH_EVERY:
                        _upsert_yearly_csv_batch(output, pending_flush)
                        pending_flush.clear()

                    label = repo[:name_width].ljust(name_width)
                    progress.update(task, completed=len(results),
                                    description=f"{label} ({len(results)}/{len(to_fetch)})")

                pending_repos = deferred
                if deferred and round_num < MAX_ROUNDS - 1:
                    log.info("Round %d done: %d resolved, %d deferred → retrying",
                             round_num + 1, len(results), len(deferred))

            for repo in pending_repos:
                yr = compute_yearly_breakdown([], year_start, year_end,
                                               threshold=threshold, base=base,
                                               include_bots=include_bots)
                rr = _RepoResult(repo=repo, year_results=yr,
                                 elapsed=repo_api_time[repo], rounds=MAX_ROUNDS)
                results.append(rr)
                pending_flush.append((rr.repo, rr.year_results))
                log.warning("%s: no stats after %d rounds, treating as empty", repo, MAX_ROUNDS)

            if pending_flush:
                _upsert_yearly_csv_batch(output, pending_flush)
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
