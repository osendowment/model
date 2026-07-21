"""Batch processing — async multi-repo fetching and CSV I/O."""

import asyncio
import csv as csv_mod
import datetime
import logging
import os
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass

import aiohttp
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from src.sources.github.display import _ETAColumn, console
from src.sources.github.github_client import (
    _AsyncRateLimiter,
    _Deferred,
    _fetch_contributors_paginated,
    _fetch_total_commits,
    _fetch_total_contributors,
)
from src.sources.github.models import THRESHOLD
from src.common.params import fetch_ttl_days
from src.common.repos import git_url_for, load_git_urls

log = logging.getLogger(__name__)


# --- CSV I/O ---
#
# Raw /contributors payload is persisted to two files under data/sources/github/:
#   contributor-commits.csv       — long, one row per (repo, contributor)
#   contributor-commits.status.csv — per-repo fetch status sidecar

GH_CONTRIB_FILE = "data/sources/github/contributor-commits.csv"
GH_CONTRIB_STATUS_FILE = "data/sources/github/contributor-commits.status.csv"
# repo_id is load-bearing: build_concentration joins this file by the stable
# id, so a rewrite that drops the column blanks the GitHub cross-check axis.
GH_CONTRIB_FIELDS = ["repo", "repo_id", "git_url", "login", "contributions", "account_type"]
GH_CONTRIB_STATUS_FIELDS = ["repo", "repo_id", "git_url", "status", "n_contributors", "fetched_at"]

# Cache TTL (365 days) from settings.json — rows older than this get re-fetched.
CONCENTRATION_TTL_DAYS = fetch_ttl_days("sources/github/batch_runner")
GH_REPOS_FILE = "data/sources/github/repos.csv"


def _load_repo_id_map() -> dict[str, str]:
    """Return {repo_lowercased: numeric_repo_id} from data/sources/github/repos.csv.

    Empty if the file doesn't exist.
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
    """Return repos in contributor-commits.status.csv whose `fetched_at` is within TTL.

    Used as the freshness skip filter. Bypassed by `--force` on the CLI.
    """
    if not os.path.exists(GH_CONTRIB_STATUS_FILE):
        return set()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    fresh: set[str] = set()
    with open(GH_CONTRIB_STATUS_FILE, encoding="utf-8") as f:
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


def _upsert_contributor_commits(
    batch: list[tuple[str, list[dict] | None]],
) -> None:
    """Upsert raw per-contributor rows into contributor-commits.csv and the status sidecar.

    `batch` is a list of (repo, raw_contributors_list_or_None).
    - None  → status "error",  no long rows written for that repo.
    - []    → status "empty",  no long rows written.
    - [..] → status "ok",    one long row per contributor dict.

    Each contributor dict from the API has at minimum: login, contributions, type.

    Both files are upserts: load existing rows, replace rows for repos in this
    batch, keep all other repos, rewrite sorted by repo. Writes are atomic
    (temp file + os.replace).
    """
    if not batch:
        return

    repo_ids = _load_repo_id_map()
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # Load existing long rows (keyed by repo for replacement)
    existing_contribs: dict[str, list[dict]] = {}
    if os.path.exists(GH_CONTRIB_FILE):
        with open(GH_CONTRIB_FILE, encoding="utf-8") as f:
            for row in csv_mod.DictReader(f):
                r = (row.get("repo") or "").strip()
                if r:
                    existing_contribs.setdefault(r, []).append(row)

    # Load existing status rows
    existing_status: dict[str, dict] = {}
    if os.path.exists(GH_CONTRIB_STATUS_FILE):
        with open(GH_CONTRIB_STATUS_FILE, encoding="utf-8") as f:
            for row in csv_mod.DictReader(f):
                r = (row.get("repo") or "").strip()
                if r:
                    existing_status[r] = row

    for repo, contributors in batch:
        if contributors is None:
            # error — record status, leave long rows untouched
            existing_status[repo] = {
                "repo": repo,
                "repo_id": repo_ids.get(repo, existing_status.get(repo, {}).get("repo_id", "")),
                "status": "error",
                "n_contributors": "0",
                "fetched_at": fetched_at,
            }
            existing_contribs.pop(repo, None)
        else:
            new_rows = [
                {
                    "repo": repo,
                    "repo_id": repo_ids.get(repo, ""),
                    "login": (c.get("login") or "").lower(),
                    "contributions": str(int(c.get("contributions") or 0)),
                    "account_type": c.get("type") or "",
                }
                for c in contributors
            ]
            existing_contribs[repo] = new_rows
            status = "ok" if new_rows else "empty"
            existing_status[repo] = {
                "repo": repo,
                "repo_id": repo_ids.get(repo, existing_status.get(repo, {}).get("repo_id", "")),
                "status": status,
                "n_contributors": str(len(new_rows)),
                "fetched_at": fetched_at,
            }

    # Atomic write — long file. Heal any round-tripped row missing its
    # repo_id (legacy rows written before the column existed).
    os.makedirs(os.path.dirname(GH_CONTRIB_FILE), exist_ok=True)
    gitmap = load_git_urls()
    all_rows = [row for repo in sorted(existing_contribs)
                for row in existing_contribs[repo]]
    for row in all_rows:
        if not (row.get("repo_id") or "").strip():
            row["repo_id"] = repo_ids.get((row.get("repo") or "").strip(), "")
        row["git_url"] = git_url_for(
            row.get("repo_id", ""), (row.get("repo") or "").strip(), gitmap)
    _atomic_write_csv(GH_CONTRIB_FILE, GH_CONTRIB_FIELDS, all_rows)

    # Atomic write — status file
    status_rows = [existing_status[repo] for repo in sorted(existing_status)]
    for row in status_rows:
        row["git_url"] = git_url_for(
            row.get("repo_id", ""), (row.get("repo") or "").strip(), gitmap)
    _atomic_write_csv(GH_CONTRIB_STATUS_FILE, GH_CONTRIB_STATUS_FIELDS, status_rows)


def _atomic_write_csv(path: str, fields: list[str], rows: list[dict]) -> None:
    """Write rows to path atomically via a temp file + os.replace."""
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    include_bots: bool = False,
    limit: int | None = None,
    force: bool = False,
) -> None:
    """Fetch raw /contributors payload for multiple repos in parallel.

    Uses GitHub's /repos/{owner}/{repo}/contributors endpoint (lifetime
    contribution totals). Results are written to data/sources/github/contributor-commits.csv
    and data/sources/github/contributor-commits.status.csv.

    `threshold` and `include_bots` are accepted for CLI compatibility but are
    not used here — metric computation is delegated to build_concentration.py.
    """
    import random

    t_start = time.monotonic()

    # Freshness gate: repos with a fresh status row are skipped.
    # `--force` bypasses this.
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
    MAX_ROUNDS = 2
    ROUND_DELAY = 5

    @dataclass
    class _RepoResult:
        repo: str
        data: list[dict] | None  # None = error; [] = empty; [...] = ok
        elapsed: float
        error: str = ""
        rounds: int = 1

    async def _try_one(repo: str):
        """Fetch /contributors for one repo. Returns (repo, data, error, elapsed)."""
        async with sem:
            t = time.monotonic()
            try:
                data, _total_commits, _total_contribs = await asyncio.gather(
                    _fetch_contributors_paginated(session, limiter, repo),
                    _fetch_total_commits(session, limiter, repo),
                    _fetch_total_contributors(session, limiter, repo),
                )
                return (repo, data, "", time.monotonic() - t)
            except _Deferred:
                return (repo, None, "", time.monotonic() - t)
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

            FLUSH_EVERY = 1
            pending_flush: list[tuple[str, list[dict] | None]] = []
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

                    if error:
                        rr = _RepoResult(repo=repo, data=None,
                                         elapsed=repo_api_time[repo], error=error,
                                         rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((repo, None))
                    elif data is None:
                        deferred.append(repo)
                        continue
                    else:
                        rr = _RepoResult(repo=repo, data=data,
                                         elapsed=repo_api_time[repo], rounds=round_num + 1)
                        results.append(rr)
                        pending_flush.append((repo, data))

                    if len(pending_flush) >= FLUSH_EVERY:
                        _upsert_contributor_commits(pending_flush)
                        pending_flush.clear()

                    label = repo[:name_width].ljust(name_width)
                    progress.update(task, completed=len(results),
                                    description=f"{label} ({len(results)}/{len(to_fetch)})")
                    if len(results) % 25 == 0 or len(results) == len(to_fetch):
                        console.print(
                            f"[dim]{len(results)}/{len(to_fetch)}  ({repo})[/dim]"
                        )

                pending_repos = deferred
                if deferred and round_num < MAX_ROUNDS - 1:
                    log.info("Round %d done: %d resolved, %d deferred → retrying",
                             round_num + 1, len(results), len(deferred))

            for repo in pending_repos:
                rr = _RepoResult(repo=repo, data=[],
                                 elapsed=repo_api_time[repo], rounds=MAX_ROUNDS)
                results.append(rr)
                pending_flush.append((repo, []))
                log.warning("%s: no contributors after %d rounds, treating as empty",
                            repo, MAX_ROUNDS)

            if pending_flush:
                _upsert_contributor_commits(pending_flush)
                pending_flush.clear()

    # Results table
    errors = sum(1 for r in results if r.error)
    elapsed = time.monotonic() - t_start

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Repo", min_width=20)
    table.add_column("Contributors", justify="right")
    table.add_column("Time", justify="right", style="dim")

    results.sort(key=lambda r: to_fetch.index(r.repo))

    for i, rr in enumerate(results):
        is_last = i == len(results) - 1
        if rr.error:
            table.add_row(rr.repo, "[red]error[/red]", f"{rr.elapsed:.1f}s",
                          end_section=is_last)
        elif rr.data is not None:
            table.add_row(
                rr.repo,
                str(len(rr.data)) if rr.data else "[dim]0[/dim]",
                f"{rr.elapsed:.1f}s",
                end_section=is_last,
            )
        else:
            table.add_row(rr.repo, "[dim]–[/dim]", f"{rr.elapsed:.1f}s",
                          end_section=is_last)

    ok_results = [r for r in results if not r.error]
    times = [r.elapsed for r in results]
    avg_time = sum(times) / len(times) if times else 0
    max_time = max(times) if times else 0
    min_time = min(times) if times else 0

    table.add_row(
        f"[dim]{len(ok_results)} repos[/dim]",
        "",
        f"[bold]{elapsed:.1f}s[/bold]",
    )
    table.add_row(
        "[dim]avg / min / max[/dim]",
        "",
        f"[dim]{avg_time:.1f} / {min_time:.1f} / {max_time:.1f}s[/dim]",
    )
    table.add_row(
        "[dim]API calls[/dim]",
        "",
        f"[dim]{limiter.requests_made}[/dim]",
    )
    table.add_row(
        "[dim]skipped / errors[/dim]",
        "",
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
