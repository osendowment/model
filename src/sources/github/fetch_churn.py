"""Code-churn metric per risk-scope repo over the 2021-2025 window.

Strategy
--------
For each repo we want lines added + lines deleted on the default branch
between 2021-01-01 and 2025-12-31 inclusive, restricted to source files
(generated/data/lockfile noise excluded).

  1. `git clone --bare --no-tags <url>` — pulls the full commit graph
     and packed blobs (no working tree). We need blobs because numstat
     reads the actual file contents on each side of the diff. With
     `--filter=blob:none` git would lazy-fetch them one diff at a
     time, which is *much* slower (HTTP RTT per missing blob).
  2. `git log --numstat --no-merges --since=2021-01-01 --until=2026-01-01`
     — walks default branch only (HEAD on a bare clone), one numstat
     block per commit.
  3. Aggregate added + deleted per file, keep only paths that match
     SOURCE_EXTS, drop the rest.
  4. Hard timeout of 5 min per repo; on timeout, the row is skipped
     (logged in the failed-repos table).

We deliberately don't try to identify the *default* branch — a bare
clone from a fresh URL has its HEAD pointing at whatever the remote's
default is, which is exactly what we want.

Output: data/sources/git/churn.csv

Columns:
  repo, analyzed_through_year, commits_5y_examined,
  churn_5y_added, churn_5y_deleted, churn_5y_total,
  churn_files_count,
  top_file_path, top_file_churn,
  elapsed_s, fetched_at

Usage:
    uv run python -m src.sources.github.fetch_churn --limit 10
    uv run python -m src.sources.github.fetch_churn --concurrency 8
    uv run python -m src.sources.github.fetch_churn --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import os
import random
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass

from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn,
)
from rich.table import Table

from src.sources.git.clone import SOURCE_EXTS
from src.sources.git.disk import check_disk_or_exit, make_clone_tmpdir, print_disk_banner, sweep_stale_clone_dirs
from src.sources.github.display import _ETAColumn
from src.common.repos import git_url_for, load_git_urls, load_repo_ids, load_top_repos

log = logging.getLogger(__name__)
console = Console()

OUTPUT_FILE = "data/sources/git/churn.csv"

PERIOD_START = "2021-01-01"
PERIOD_END_EXCLUSIVE = "2026-01-01"  # git log --until is exclusive at midnight UTC
ANALYZED_THROUGH_YEAR = 2025

DEFAULT_TTL_DAYS = 365
DEFAULT_CONCURRENCY = int(os.environ.get("CHURN_WORKERS") or 4)
PER_REPO_TIMEOUT = 300  # 5 minutes hard cap per repo

# Source-file matcher built from SOURCE_EXTS (strips the leading "*.").
SOURCE_SUFFIXES: tuple[str, ...] = tuple(
    e.lstrip("*").lower() for e in SOURCE_EXTS
)

OUTPUT_FIELDS = [
    "repo", "repo_id", "git_url", "analyzed_through_year", "commits_5y_examined",
    "churn_5y_added", "churn_5y_deleted", "churn_5y_total",
    "churn_files_count",
    "top_file_path", "top_file_churn",
    "elapsed_s", "fetched_at",
]


@dataclass
class ChurnResult:
    repo: str
    commits: int = 0
    added: int = 0
    deleted: int = 0
    files_count: int = 0
    top_file_path: str = ""
    top_file_churn: int = 0
    elapsed: float = 0.0
    clone_bytes: int = 0
    error: str = ""

    @property
    def total(self) -> int:
        return self.added + self.deleted


def _is_source_path(path: str) -> bool:
    """True if the path ends with a known source-file suffix."""
    p = path.lower()
    # Use endswith on the full set — fast enough at ~80 entries.
    return p.endswith(SOURCE_SUFFIXES)


def _run(args: list[str], timeout: int, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with hard process-group kill on timeout."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait()
        raise


def _bare_clone(repo: str, dest: str, timeout: int) -> None:
    """Bare clone (no working tree, no tags). HEAD points at the remote default branch.

    We *don't* use --filter=blob:none here — numstat needs blob contents to
    diff, and lazy-fetching one missing blob at a time over HTTP is far
    slower than the upfront packfile download.
    """
    url = f"https://github.com/{repo}.git"
    res = _run(
        ["git", "clone", "--bare", "--quiet", "--no-tags", url, dest],
        timeout=timeout,
    )
    if res.returncode != 0:
        msg = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        snippet = (msg[-1] if msg else "git clone failed")[:80]
        raise RuntimeError(snippet)


def _git_log_numstat(git_dir: str, timeout: int) -> bytes:
    """Run `git log --numstat --no-merges --since --until` on HEAD. Returns raw stdout."""
    res = _run(
        [
            "git", f"--git-dir={git_dir}", "log",
            "--numstat", "--no-merges", "--no-renames",
            "--format=%H",  # one commit-hash line per commit, then numstat block
            f"--since={PERIOD_START}T00:00:00Z",
            f"--until={PERIOD_END_EXCLUSIVE}T00:00:00Z",
        ],
        timeout=timeout,
    )
    if res.returncode != 0:
        msg = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        snippet = (msg[-1] if msg else "git log failed")[:80]
        raise RuntimeError(snippet)
    return res.stdout


def _parse_numstat(raw: bytes) -> tuple[int, int, int, dict[str, int]]:
    """Parse git log --numstat output.

    Returns (commits, total_added, total_deleted, per_file_churn).
    Only counts source-file paths. Binary files (numstat shows '-\\t-')
    are skipped.

    Format (one block per commit, separated by blank lines):
        <commit-hash>
        <added>\\t<deleted>\\t<path>
        <added>\\t<deleted>\\t<path>
        ...
    """
    commits = 0
    total_added = 0
    total_deleted = 0
    per_file: dict[str, int] = {}

    # Decode line-by-line to avoid building a giant str for huge repos.
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line:
            continue
        if "\t" not in line:
            # Commit-hash line (the %H format above).
            commits += 1
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a_str, d_str, path = parts
        if a_str == "-" or d_str == "-":
            continue  # binary file — numstat reports dashes
        if not _is_source_path(path):
            continue
        try:
            a = int(a_str)
            d = int(d_str)
        except ValueError:
            continue
        total_added += a
        total_deleted += d
        per_file[path] = per_file.get(path, 0) + a + d

    return commits, total_added, total_deleted, per_file


def _dir_size_bytes(path: str) -> int:
    """Walk a dir tree, return total file size in bytes. Cheap stat-only walk."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _process_repo_blocking(repo: str, base_dir: str) -> ChurnResult:
    """Synchronous path: clone, log, parse, cleanup. Runs in a worker thread."""
    result = ChurnResult(repo=repo)
    t0 = time.monotonic()
    dest = os.path.join(base_dir, repo.replace("/", "__") + ".git")

    try:
        _bare_clone(repo, dest, timeout=PER_REPO_TIMEOUT)
        try:
            result.clone_bytes = _dir_size_bytes(dest)
        except OSError:
            pass

        raw = _git_log_numstat(dest, timeout=PER_REPO_TIMEOUT)
        commits, added, deleted, per_file = _parse_numstat(raw)
        result.commits = commits
        result.added = added
        result.deleted = deleted
        result.files_count = len(per_file)

        if per_file:
            top_path, top_churn = max(per_file.items(), key=lambda kv: kv[1])
            result.top_file_path = top_path
            result.top_file_churn = top_churn

    except subprocess.TimeoutExpired:
        result.error = f"timeout (>{PER_REPO_TIMEOUT}s)"
    except RuntimeError as e:
        result.error = str(e)[:80]
    except Exception as e:  # noqa: BLE001 — defensive catch-all
        result.error = f"{type(e).__name__}: {str(e)[:60]}"
    finally:
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        result.elapsed = time.monotonic() - t0

    return result


async def _process_repo(
    repo: str, base_dir: str, sem: asyncio.Semaphore,
) -> ChurnResult:
    """Async wrapper — runs the blocking clone+parse in a thread."""
    async with sem:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _process_repo_blocking, repo, base_dir)


# --- CSV I/O ---------------------------------------------------------------

def _load_existing(path: str) -> dict[str, dict[str, str]]:
    """Load existing churn rows keyed by repo. Empty if file missing."""
    if not os.path.exists(path):
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = row
    return out


def _filter_by_ttl(
    repos: list[str], existing: dict[str, dict[str, str]], ttl_days: int,
) -> tuple[list[str], int]:
    """Drop repos whose existing row is fresher than `ttl_days`."""
    if ttl_days <= 0 or not existing:
        return repos, 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    fresh: set[str] = set()
    for slug, row in existing.items():
        ts = (row.get("fetched_at") or "").strip()
        if not ts:
            continue
        try:
            fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if fetched >= cutoff:
            fresh.add(slug)
    keep = [r for r in repos if r not in fresh]
    return keep, len(repos) - len(keep)


def _write_csv(
    path: str, results: list[ChurnResult], existing: dict[str, dict[str, str]],
    repo_ids: dict[str, str] | None = None,
) -> None:
    """Merge new results into existing rows and rewrite the CSV.

    Errored results are skipped (existing rows for those repos are preserved
    so we don't lose previously-good data on a transient failure).

    `repo_ids` (slug -> stable GitHub id) stamps new rows and backfills any
    existing row missing its `repo_id` — downstream builders join churn by
    repo_id, so a blank id makes the row invisible to them. The writer is
    schema-tolerant on extra columns (extrasaction="ignore") so a file whose
    header gained a column can never crash a rewrite mid-file again.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ids = repo_ids or {}

    for r in results:
        if r.error:
            continue
        existing[r.repo] = {
            "repo": r.repo,
            "repo_id": ids.get(r.repo, ""),
            "analyzed_through_year": str(ANALYZED_THROUGH_YEAR),
            "commits_5y_examined": str(r.commits),
            "churn_5y_added": str(r.added),
            "churn_5y_deleted": str(r.deleted),
            "churn_5y_total": str(r.total),
            "churn_files_count": str(r.files_count),
            "top_file_path": r.top_file_path,
            "top_file_churn": str(r.top_file_churn) if r.top_file_churn else "0",
            "elapsed_s": f"{r.elapsed:.1f}",
            "fetched_at": now,
        }

    gitmap = load_git_urls()
    for slug, row in existing.items():
        if not (row.get("repo_id") or "").strip():
            row["repo_id"] = ids.get(slug, "")
        row["git_url"] = git_url_for(row.get("repo_id", ""), slug, gitmap)

    rows = sorted(
        existing.values(),
        key=lambda r: -int(r.get("churn_5y_total", 0) or 0),
    )
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


# --- Display ---------------------------------------------------------------

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


def _fmt_int(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n}"


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}G"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n}B"


def _print_results_table(ok: list[ChurnResult]) -> None:
    """Top-30 by churn_5y_total."""
    if not ok:
        return
    by_churn = sorted(ok, key=lambda r: -r.total)
    show = by_churn[:30]

    tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Repo")
    tbl.add_column("Commits", justify="right")
    tbl.add_column("Added", justify="right")
    tbl.add_column("Deleted", justify="right")
    tbl.add_column("Total", justify="right")
    tbl.add_column("Files", justify="right")
    tbl.add_column("Top file", overflow="fold")
    tbl.add_column("Top churn", justify="right")
    tbl.add_column("Time", justify="right")

    for r in show:
        tbl.add_row(
            r.repo,
            f"{r.commits:,}",
            _fmt_int(r.added),
            _fmt_int(r.deleted),
            f"[bold]{_fmt_int(r.total)}[/bold]",
            f"{r.files_count:,}",
            r.top_file_path[:40],
            _fmt_int(r.top_file_churn),
            f"[dim]{r.elapsed:.0f}s[/dim]",
        )

    if len(by_churn) > len(show):
        rest = by_churn[len(show):]
        rest_tot = sum(r.total for r in rest)
        tbl.add_row(
            f"[dim]... {len(rest)} more[/dim]", "", "", "",
            f"[dim]{_fmt_int(rest_tot)}[/dim]", "", "", "", "",
        )

    tbl.add_section()
    tot_added = sum(r.added for r in ok)
    tot_del = sum(r.deleted for r in ok)
    tot_files = sum(r.files_count for r in ok)
    tot_commits = sum(r.commits for r in ok)
    tot_time = sum(r.elapsed for r in ok)
    tbl.add_row(
        f"[bold]Total[/bold]",
        f"[bold]{tot_commits:,}[/bold]",
        f"[bold]{_fmt_int(tot_added)}[/bold]",
        f"[bold]{_fmt_int(tot_del)}[/bold]",
        f"[bold]{_fmt_int(tot_added + tot_del)}[/bold]",
        f"[bold]{tot_files:,}[/bold]",
        "", "",
        f"[bold]{tot_time:.0f}s[/bold]",
    )
    console.print(tbl)
    console.print()


def _print_perf_summary(
    ok: list[ChurnResult], errors: list[ChurnResult],
    skipped: int, total_eligible: int, existing_count: int,
    wallclock: float,
) -> None:
    """Per-repo timing distribution + bytes summary."""
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=14)
    summary.add_column()

    summary.add_row("Wallclock", f"[bold]{wallclock:.1f}s[/bold]")
    summary.add_row("Analyzed", f"[bold]{len(ok):,}[/bold] repos")
    if ok:
        times = sorted(r.elapsed for r in ok)
        p50 = statistics.median(times)
        p90 = times[int(len(times) * 0.9)] if len(times) >= 10 else times[-1]
        summary.add_row(
            "Per-repo",
            f"min {times[0]:.1f}s [dim]·[/] p50 {p50:.1f}s "
            f"[dim]·[/] p90 {p90:.1f}s [dim]·[/] max {times[-1]:.1f}s",
        )
        cloned = sum(r.clone_bytes for r in ok)
        rate = cloned / wallclock if wallclock else 0
        summary.add_row(
            "Cloned",
            f"{_fmt_bytes(cloned)} [dim]({_fmt_bytes(int(rate))}/s avg)[/dim]",
        )
    if skipped:
        summary.add_row("Skipped (TTL)", f"{skipped:,}")
    if errors:
        summary.add_row("Failed", f"[red]{len(errors):,}[/red]")
    summary.add_row(
        "Coverage",
        f"{existing_count + len(ok):,} / {total_eligible:,} risk-scope repos",
    )
    summary.add_row("Output", OUTPUT_FILE)
    console.print(summary)
    console.print()

    if errors:
        err_tbl = Table(
            title="[bold]Failed repos[/bold]",
            show_header=True, header_style="bold dim", padding=(0, 1),
        )
        err_tbl.add_column("Repo")
        err_tbl.add_column("Error")
        err_tbl.add_column("Time", justify="right")
        for r in errors[:20]:
            err_tbl.add_row(r.repo, r.error, f"{r.elapsed:.1f}s")
        if len(errors) > 20:
            err_tbl.add_row(f"[dim]... {len(errors) - 20} more[/dim]", "", "")
        console.print(err_tbl)
        console.print()


# --- Main ------------------------------------------------------------------

async def _run_all(
    repos: list[str], concurrency: int, base_dir: str,
    on_result: callable | None = None,
    max_disk_gb: float = 0.0,
) -> list[ChurnResult]:
    sem = asyncio.Semaphore(concurrency)
    progress = _make_progress()
    name_width = min(max((len(r) for r in repos), default=20), 30)
    results: list[ChurnResult] = []

    with progress:
        task = progress.add_task(" " * name_width, total=len(repos))

        async def _one(repo: str) -> ChurnResult:
            r = await _process_repo(repo, base_dir, sem)
            desc = repo[:name_width].ljust(name_width)
            progress.update(task, advance=1, description=desc)
            return r

        coros = [_one(r) for r in repos]
        aborted = False
        for coro in asyncio.as_completed(coros):
            r = await coro
            results.append(r)
            if on_result:
                on_result(r, len(results))
            if max_disk_gb > 0 and not aborted:
                if not check_disk_or_exit(max_disk_gb, console=console):
                    aborted = True
                    break

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch 5y code-churn metric per risk-scope (A/B value-class) GitHub repo.",
    )
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--limit", type=int,
                        help="Process only N random risk-scope repos (for testing)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for --limit sampling (default: 42)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Concurrent clones (default: {DEFAULT_CONCURRENCY}). "
                             "Override with $CHURN_WORKERS.")
    parser.add_argument("--max-disk-gb", type=float, default=2.0,
                        help="Abort gracefully if free /tmp dips below this "
                             "(default: 2.0; set 0 to disable).")
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip repos refreshed within N days (default: {DEFAULT_TTL_DAYS})")
    parser.add_argument("--force", action="store_true",
                        help="Bypass TTL — refetch all selected repos")
    parser.add_argument("--repos", nargs="+",
                        help="Override repo selection — process exactly these slugs")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    started = datetime.datetime.now()
    console.print(
        f"[bold]Code churn 2021-01-01 → 2025-12-31[/bold]  "
        f"[dim]concurrency={args.concurrency}, ttl={args.ttl_days}d, "
        f"started {started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    # Load risk-scope set. repo_ids stamps every written row — downstream
    # builders join churn by the stable repo_id, not the renameable slug.
    repo_ids = load_repo_ids()
    if args.repos:
        repos = [r.strip().lower() for r in args.repos]
        total_eligible = len(repos)
    else:
        # GitHub-only: `_bare_clone` hardcodes the github.com URL, so a GitLab
        # slug would clone the wrong host's mirror. Non-GitHub repos are skipped
        # (their churn stays blank; build_complexity's hotspot input is optional
        # and does not feed the complexity score).
        risk_repos = [e for e in load_top_repos()
                      if not str(e.repo_id).startswith("gl/")]
        repos = [e.repo for e in risk_repos]
        repo_ids.update({e.repo: str(e.repo_id) for e in risk_repos if e.repo_id})
        total_eligible = len(repos)

    existing = _load_existing(args.output)
    existing_count = len(existing)

    # TTL filter (unless --force)
    if not args.force:
        repos, ttl_skipped = _filter_by_ttl(repos, existing, args.ttl_days)
    else:
        ttl_skipped = 0

    if args.limit and len(repos) > args.limit:
        random.seed(args.seed)
        repos = random.sample(repos, args.limit)

    if not repos:
        console.print(f"[dim]Nothing to process — all {total_eligible:,} repos fresh "
                      f"(ttl={args.ttl_days}d). Use --force to refetch.[/dim]")
        return

    console.print(
        f"[dim]Processing {len(repos):,} repos "
        f"(of {total_eligible:,} risk-scope, {ttl_skipped:,} TTL-skipped)[/dim]"
    )
    console.print()

    base_dir = make_clone_tmpdir("churn")
    t0 = time.monotonic()
    pending_flush: list[ChurnResult] = []

    def _on_result(r: ChurnResult, _completed: int) -> None:
        # Flush every 10 results so a crash doesn't lose work — write the
        # batch into `existing` (which already holds prior rows + earlier
        # flushes) and rewrite the CSV. _write_csv merges, doesn't replace.
        if r.error:
            return
        pending_flush.append(r)
        if len(pending_flush) >= 10:
            _write_csv(args.output, pending_flush, existing, repo_ids)
            pending_flush.clear()

    try:
        results = asyncio.run(_run_all(
            repos, args.concurrency, base_dir, on_result=_on_result,
            max_disk_gb=args.max_disk_gb,
        ))
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)

    wallclock = time.monotonic() - t0
    ok = [r for r in results if not r.error]
    errors = [r for r in results if r.error]

    # Final write — covers anything still pending from the incremental flush.
    if ok:
        _write_csv(args.output, ok, existing, repo_ids)

    _print_results_table(ok)
    _print_perf_summary(
        ok, errors,
        skipped=ttl_skipped,
        total_eligible=total_eligible,
        existing_count=existing_count,
        wallclock=wallclock,
    )


if __name__ == "__main__":
    main()
