"""Run scc on per-(repo, sha) sparse-checkouts and write to data/git/scc.csv.

Long-format output schema is the standard ``data/git/<tool>.csv`` shape:

    repo, repo_id, commit_sha, metric, value, checked_at

Six metrics emitted per snapshot:
    files, loc, sloc, uloc, complexity, complexity_density

Strategy:
    1. Read ``data/github/git/commits-years.csv`` → per-repo list of
       ``last_sha`` per year. We pin scc to the **most recent** populated
       last_sha (cascading back across earlier years if needed) so each
       repo is analysed once at its newest known snapshot.
    2. Smart upsert: skip repos whose ``(repo, sha)`` already has all 6
       metrics in ``data/git/scc.csv`` with non-empty values.
    3. For each remaining repo, sparse-clone at its target SHA via
       ``src.git.clone.sparse_clone`` (filtered to source-code globs).
    4. Run ``scc --uloc --format json`` and parse the per-language output;
       sum across the SOURCE_LANGS allow-list (skips docs/configs/data).
    5. Upsert metrics via ``src.git.long_format.upsert_snapshot``.
    6. Always ``rmtree`` the per-repo dest in finally so /tmp stays clean.

Concurrency cap defaults to 4. Override with ``--concurrency`` or env
``SCC_WORKERS``.

Usage:

    uv run python -m src.git.fetch_scc --limit 5
    uv run python -m src.git.fetch_scc                   # full risk-scope set
    uv run python -m src.git.fetch_scc --force           # ignore smart-upsert
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn,
)
from rich.table import Table

from src.git.clone import sparse_clone
from src.git.commits_years import load_sha_data, resolve_snapshot_sha
from src.git.disk import check_disk_or_exit, print_disk_banner
from src.git.long_format import read as read_long
from src.git.long_format import upsert_snapshot
from src.github.display import _ETAColumn
from src.pipeline.common.repos import load_repo_ids, load_risk_repos

log = logging.getLogger(__name__)
console = Console()

REPOS_FILE = "data/value-data.csv"
SHA_FILE = "data/github/git/commits-years.csv"
OUTPUT_FILE = "data/git/scc.csv"

DEFAULT_YEARS = [2021, 2022, 2023, 2024, 2025]
DEFAULT_CONCURRENCY = int(os.environ.get("SCC_WORKERS") or 4)
SCC_TIMEOUT_S = 120

# Six metrics emitted per (repo, sha) snapshot.
SCC_METRICS = ("files", "loc", "sloc", "uloc", "complexity", "complexity_density")

# Source-code languages scc recognises — names must match scc's output.
SOURCE_LANGS: set[str] = {
    "C", "C Header", "C++", "C++ Header", "C#", "Objective C", "Objective C++",
    "Java", "Kotlin", "Scala", "JavaScript", "TypeScript", "JSX", "TSX",
    "CoffeeScript", "Python", "Ruby", "Perl", "PHP", "Go", "Rust", "Swift",
    "Dart", "Lua", "R", "Julia", "Haskell", "Erlang", "Elixir", "Clojure",
    "Zig", "Nim", "Assembly", "D", "OCaml", "Fortran Modern", "FORTRAN Legacy",
    "Groovy", "Solidity", "GDScript", "Vue",
}


# ─────────────────────────────── dataclass ───────────────────────────────

@dataclass
class SccResult:
    repo: str
    sha: str = ""
    files: int = 0
    loc: int = 0       # scc "Lines" — total lines including comments/blanks
    sloc: int = 0      # scc "Code"  — source lines excluding comments/blanks
    uloc: int = 0      # unique LOC
    complexity: int = 0
    elapsed_s: float = 0.0
    skipped: bool = False  # True when smart-upsert skipped re-fetch
    error: str = ""

    @property
    def complexity_density(self) -> float:
        """Complexity per 100 source lines."""
        return self.complexity / self.sloc * 100 if self.sloc else 0.0


# ─────────────────────────────── helpers ────────────────────────────────

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


def _run(args: list[str], timeout: int, **kwargs) -> subprocess.CompletedProcess:
    """Subprocess runner that kills the whole process group on timeout."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        proc.wait()
        raise


def _analyze_dir(path: str) -> tuple[int, int, int, int, int]:
    """Run scc on a directory. Returns (files, lines, code, uloc, complexity).

    Sums across the SOURCE_LANGS allow-list — skips docs, configs, data files.
    """
    result = _run(
        ["scc", "--uloc", "--format", "json", path],
        timeout=SCC_TIMEOUT_S,
    )
    if result.returncode != 0:
        return 0, 0, 0, 0, 0
    data = json.loads(result.stdout)
    files = lines = code = uloc = complexity = 0
    for lang in data:
        if lang["Name"] not in SOURCE_LANGS:
            continue
        files += lang["Count"]
        lines += lang["Lines"]
        code += lang["Code"]
        uloc += lang.get("ULOC", 0)
        complexity += lang["Complexity"]
    return files, lines, code, uloc, complexity


# ───────────────────── target SHA & smart-upsert filter ─────────────────

def _target_sha_per_repo(
    sha_data: dict[tuple[str, str], dict[str, str]],
    repos: list[str],
    years: list[int],
) -> dict[str, str]:
    """For each repo, pick its newest populated last_sha across ``years``.

    Walks years high→low, returns the first non-empty ``last_sha``. Cascade
    via ``resolve_snapshot_sha`` so a year with 0 commits doesn't block us.
    Repos with no SHA at any requested year are absent from the result.
    """
    out: dict[str, str] = {}
    for r in repos:
        for y in sorted(years, reverse=True):
            sha = resolve_snapshot_sha(sha_data, r, y)
            if sha:
                out[r] = sha
                break
    return out


def _already_done(
    long_path: str | Path,
    targets: dict[str, str],
) -> set[str]:
    """Return the set of repos whose target (repo, sha) is fully present.

    "Fully present" = all six SCC_METRICS exist with non-empty values
    **and** `loc` is non-zero. An all-zero snapshot means a failed or
    empty sparse-checkout (scc saw 0 files) — it must be re-analysed, not
    skipped, otherwise a bad snapshot is cached forever.
    """
    rows = read_long(long_path)
    by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for (repo, sha, metric), row in rows.items():
        val = row.get("value") or ""
        if val == "":
            continue
        by_pair.setdefault((repo, sha), {})[metric] = val
    needed = set(SCC_METRICS)
    done: set[str] = set()
    for repo, sha in targets.items():
        metrics = by_pair.get((repo, sha), {})
        if not needed.issubset(metrics.keys()):
            continue
        try:
            loc_ok = float(metrics.get("loc", "0")) > 0
        except ValueError:
            loc_ok = False
        if loc_ok:
            done.add(repo)
    return done


def _load_repo_id_map() -> dict[str, str]:
    """Map ``repo`` → ``repo_id`` from data/github/repos.csv (best-effort)."""
    return load_repo_ids()


# ──────────────────────────── per-repo worker ───────────────────────────

async def _process_one(
    repo: str, sha: str, sizes: dict[str, int],
    base_dir: str, sem: asyncio.Semaphore,
) -> SccResult:
    """Sparse-clone at ``sha`` and run scc. Always cleans up its tmp dir."""
    res = SccResult(repo=repo, sha=sha)
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    async with sem:
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        try:
            await loop.run_in_executor(
                None, sparse_clone, repo, dest, sizes.get(repo, 0), sha,
            )
            if not os.path.isdir(dest):
                res.error = "clone failed"
                return res
            files, lines, code, uloc, complexity = await loop.run_in_executor(
                None, _analyze_dir, dest,
            )
            res.files = files
            res.loc = lines
            res.sloc = code
            res.uloc = uloc
            res.complexity = complexity
        except subprocess.TimeoutExpired:
            res.error = "timeout"
        except Exception as e:
            res.error = str(e)[:80]
        finally:
            res.elapsed_s = time.monotonic() - t0
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
    return res


# ──────────────────────────── orchestration ─────────────────────────────

async def run_all(
    targets: dict[str, str],
    sizes: dict[str, int],
    repo_ids: dict[str, str],
    output_path: str,
    concurrency: int,
    max_disk_gb: float = 0.0,
) -> list[SccResult]:
    """Process all (repo, sha) targets concurrently, upsert as each finishes.

    If ``max_disk_gb`` > 0, poll free /tmp space after each completed repo
    and stop scheduling new ones once it dips below the threshold. In-flight
    work is awaited so cleanup still runs.
    """
    sem = asyncio.Semaphore(concurrency)
    base_dir = tempfile.mkdtemp(prefix="complexity-")
    pool = ThreadPoolExecutor(max_workers=concurrency * 2 + 4)
    loop = asyncio.get_event_loop()
    loop.set_default_executor(pool)

    results: list[SccResult] = []
    name_width = min(max((len(r) for r in targets), default=20), 30)
    progress = _make_progress()
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    aborted = False

    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(targets))

            async def _run(repo: str, sha: str) -> SccResult:
                res = await _process_one(repo, sha, sizes, base_dir, sem)
                if not res.error:
                    upsert_snapshot(
                        output_path,
                        repo=repo,
                        repo_id=repo_ids.get(repo, ""),
                        commit_sha=sha,
                        metrics={
                            "files":      res.files,
                            "loc":        res.loc,
                            "sloc":       res.sloc,
                            "uloc":       res.uloc,
                            "complexity": res.complexity,
                            "complexity_density": round(res.complexity_density, 2),
                        },
                        checked_at=checked_at,
                    )
                desc = repo[:name_width].ljust(name_width)
                progress.update(task, advance=1, description=desc)
                return res

            tasks = [_run(r, s) for r, s in targets.items()]
            for coro in asyncio.as_completed(tasks):
                results.append(await coro)
                if max_disk_gb > 0 and not aborted:
                    if not check_disk_or_exit(max_disk_gb, console=console):
                        aborted = True
                        break
    finally:
        pool.shutdown(wait=False)
        shutil.rmtree(base_dir, ignore_errors=True)

    return results


# ────────────────────────────── results table ───────────────────────────

def _print_results(results: list[SccResult]) -> None:
    ok = [r for r in results if not r.error and not r.skipped]
    skipped = [r for r in results if r.skipped]
    errors = [r for r in results if r.error]

    if ok:
        ok_sorted = sorted(ok, key=lambda r: -r.elapsed_s)
        show = ok_sorted[:20]
        tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
        tbl.add_column("Repo")
        tbl.add_column("SHA", justify="right")
        tbl.add_column("Files", justify="right")
        tbl.add_column("kLOC", justify="right")
        tbl.add_column("Cmplx", justify="right")
        tbl.add_column("Dnst", justify="right")
        tbl.add_column("Time", justify="right")
        for r in show:
            kloc = r.sloc / 1000
            kloc_s = f"{kloc:,.0f}" if kloc >= 1 else f"{kloc:,.1f}"
            tbl.add_row(
                r.repo,
                f"[dim]{r.sha[:7]}[/dim]",
                f"{r.files:,}",
                kloc_s,
                f"{r.complexity:,}",
                f"{r.complexity_density:.0f}",
                f"[dim]{r.elapsed_s:.1f}s[/dim]",
            )
        if len(ok_sorted) > len(show):
            rest = ok_sorted[len(show):]
            tbl.add_row(
                f"[dim]... {len(rest)} more[/dim]", "", "",
                f"[dim]{sum(r.sloc for r in rest)/1000:,.0f}[/dim]", "", "", "",
            )
        console.print(tbl)
        console.print()

    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=14)
    summary.add_column()
    summary.add_row("Analysed", f"[bold]{len(ok)}[/bold] (repo, sha) snapshots")
    if skipped:
        summary.add_row("Skipped", f"[dim]{len(skipped)} already in scc.csv[/dim]")
    if errors:
        summary.add_row(
            "Errors",
            f"[red]{len(errors)}[/red] [dim]("
            + ", ".join(f"{r.repo}: {r.error}" for r in errors[:3])
            + ")[/dim]",
        )
    if ok:
        wall = sum(r.elapsed_s for r in ok)
        avg = wall / len(ok)
        summary.add_row("Per-repo", f"avg [bold]{avg:.1f}s[/bold]")
    console.print(summary)
    console.print()


# ─────────────────────────────── CLI ────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run scc per (repo, sha) → data/git/scc.csv (long format).",
    )
    parser.add_argument("--input", default=REPOS_FILE,
                        help=f"value-data CSV (default: {REPOS_FILE})")
    parser.add_argument("--sha-file", default=SHA_FILE,
                        help=f"commits-years.csv (default: {SHA_FILE})")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"long-format CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                        help=f"Target years for snapshot SHA (default: {DEFAULT_YEARS}). "
                             "We pick the most-recent populated year per repo.")
    parser.add_argument("--limit", type=int,
                        help="Process N random risk-scope repos (for smoke tests).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel sparse-clone+scc workers (default: {DEFAULT_CONCURRENCY}). "
                             "Override with $SCC_WORKERS.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore smart-upsert; re-fetch + re-analyse even if "
                             "(repo, sha) already has all 6 metrics in scc.csv.")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for --limit sampling (default: 42).")
    parser.add_argument("--max-disk-gb", type=float, default=2.0,
                        help="Abort gracefully if free /tmp dips below this "
                             "(default: 2.0; set 0 to disable).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    started = datetime.datetime.now()
    console.print()
    console.print(
        f"[bold]fetch_scc[/bold]  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · output={args.output} · "
        f"{started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    print_disk_banner(console=console)

    # Use the risk-scope set (A/B value classes from value-data.csv) as the
    # canonical fetch list. Entries carry size_kb directly so no side load needed.
    entries = load_risk_repos(value_file=args.input)
    repos_all = [e.repo for e in entries]
    sizes = {e.repo: e.size_kb for e in entries if e.size_kb}

    rng = random.Random(args.seed)
    if args.limit and args.limit < len(repos_all):
        repos = rng.sample(repos_all, args.limit)
    else:
        repos = list(repos_all)

    # Pick a target SHA per repo (most-recent populated year).
    sha_data = load_sha_data(args.sha_file)
    if not sha_data:
        console.print(
            f"[red]no commits-years data found at {args.sha_file}[/red]\n"
            f"[dim]run `uv run python -m src.git.commits_years` first.[/dim]"
        )
        return

    targets = _target_sha_per_repo(sha_data, repos, args.years)
    missing = [r for r in repos if r not in targets]
    if missing:
        console.print(
            f"[dim]no SHA found for {len(missing)} repos in years "
            f"{args.years} — they will be skipped.[/dim]"
        )

    # Smart-upsert: skip repos whose (repo, sha) is already complete.
    if args.force:
        skipped: set[str] = set()
    else:
        skipped = _already_done(args.output, targets)

    pending = {r: s for r, s in targets.items() if r not in skipped}
    console.print(
        f"[dim]{len(pending)} to analyse · {len(skipped)} already complete · "
        f"{len(missing)} no-sha[/dim]"
    )
    console.print()

    if not pending:
        console.print("[green]nothing to do — all targets already in scc.csv[/green]")
        return

    repo_ids = _load_repo_id_map()

    t0 = time.monotonic()
    results = asyncio.run(run_all(
        pending, sizes, repo_ids, args.output, args.concurrency,
        max_disk_gb=args.max_disk_gb,
    ))
    elapsed = time.monotonic() - t0

    # Tag skipped results so the summary shows them — synthetic entries.
    for r in skipped:
        results.append(SccResult(repo=r, sha=targets[r], skipped=True))

    _print_results(results)
    console.print(
        f"[dim]wallclock {elapsed:.1f}s · output → {args.output}[/dim]"
    )


if __name__ == "__main__":
    main()
