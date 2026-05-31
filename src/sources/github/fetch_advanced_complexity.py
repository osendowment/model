"""Compute *real* cyclomatic complexity (McCabe) per repo via lizard.

Why this exists:
    `src/sources/git/fetch_scc.py` runs `scc` for fast LOC/branch counts, but scc's
    "complexity" is just a count of branch-statement keywords — not real
    McCabe per-function complexity. For finer sustainability/risk modelling
    we want true per-function cyclomatic complexity, multi-language.

Library:
    lizard (https://github.com/terryyin/lizard)
        - Per-function McCabe via a fast hand-rolled tokenizer.
        - Built-in C/C++/JS/TS/Python/Rust/Java/Go/etc. support — covers all
          four ecosystems we ship (npm, pypi, crates, cpp).
        - Files it doesn't understand are skipped silently.

Period semantics:
    For each repo, look up `data/sources/github/git/commits-years.csv` to find the
    most recent year ≤ 2025 with commits>0; use that year's `last_sha`. If
    no per-year SHA is recorded fall back to HEAD (and we DO NOT persist
    HEAD-resolved snapshots — without a pinned sha we can't key them in the
    long format). Same logic as `src.risk.build_complexity._load_target_year()`.

Output format:
    Writes long-format rows to `data/sources/git/lizard.csv` (shared with
    `fetch_cognitive`) via `src.sources.git.long_format.upsert_snapshot`. Each row is
    `(repo, repo_id, commit_sha, metric, value, checked_at)`.

    Metrics emitted by this fetcher:
        files, cyclomatic_total, cyclomatic_avg, cyclomatic_max

    Order note: this fetcher and `fetch_cognitive` both write a `files`
    metric for the same (repo, sha). Whichever runs LAST wins on that key.
    In practice the two scan the same source-suffix set so values agree
    closely; if they differ this fetcher's `files` will overwrite cognitive's
    when run after. `elapsed_s` is logged but NOT persisted.

Usage:
    uv run python -m src.sources.github.fetch_advanced_complexity --limit 10
    uv run python -m src.sources.github.fetch_advanced_complexity --limit 0   # full set
    uv run python -m src.sources.github.fetch_advanced_complexity --repos a/b c/d
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from src.common.repos import load_risk_repos
from src.sources.git.clone import SOURCE_EXTS
from src.sources.git.clone import download_tarball as _download_tarball
from src.sources.git.clone import sparse_clone as _sparse_clone
from src.sources.git.disk import check_disk_or_exit, make_clone_tmpdir, print_disk_banner, sweep_stale_clone_dirs
from src.sources.git.long_format import read as _read_long
from src.sources.git.long_format import upsert_snapshot
from src.sources.github.display import _ETAColumn

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = "data"
COMMITS_YEARS_FILE = f"{DATA_DIR}/github/git/commits-years.csv"
SCC_LONG_FILE = f"{DATA_DIR}/git/scc.csv"
OUTPUT_FILE = f"{DATA_DIR}/git/lizard.csv"
COMPARISON_FILE = "/tmp/cyclo-vs-scc.md"

DEFAULT_LIMIT = 10
DEFAULT_SEED = 42
DEFAULT_CONCURRENCY = int(os.environ.get("CYCLO_WORKERS") or 4)
DEFAULT_TTL_DAYS = 0  # 0 = always re-run

# A single source file larger than this is skipped. Files this big are
# minified bundles, generated parsers or vendored amalgamations — not
# hand-written code (so meaningless for cyclomatic complexity) and exactly
# what OOM-kills the lizard analysis on mega-repos.
MAX_FILE_BYTES = 2_000_000

# Wall-clock cap for one repo's isolated analysis subprocess.
ANALYSIS_TIMEOUT = 900

# Metrics this fetcher emits per snapshot to data/sources/git/lizard.csv.
CYCLO_METRICS: tuple[str, ...] = (
    "files",
    "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
)

# File extensions worth analyzing — same set the sparse-checkout uses.
# Build a set of suffixes (".py", ".js", …) for quick filtering after checkout.
SOURCE_SUFFIXES: set[str] = {ext.lstrip("*").lower() for ext in SOURCE_EXTS}

# lizard 1.22.1 registers Fortran (.f/.f90/.for/…) as a supported language,
# but its Fortran reader has a pathological memory blowup on large fixed-form
# sources: scipy's 359 KB ODRPACK `d_odr.f` OOM-kills the *entire* repo
# analysis (no traceback — a bare SIGKILL). That single file is why
# scipy/scipy was the lone 1/899 repo we could never cover. Fortran is a
# rounding error in our corpus (6/1648 files even in scipy) and lizard's
# fixed-form CC is unreliable anyway — so exclude Fortran from the lizard
# input entirely. The `files` metric still counts these as source files;
# they simply contribute no functions, exactly like any other language
# lizard skips.
LIZARD_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {".f", ".f03", ".f08", ".f77", ".f90", ".f95", ".for", ".ftn"}
)


# ────────────────────────── target-SHA resolution ──────────────────────────

def _load_target_year_shas() -> dict[str, tuple[str, str]]:
    """Map repo → (year_str, last_sha) for the most recent year ≤2025 with commits>0.

    Mirrors `src.risk.build_complexity._load_target_year` but also returns
    the corresponding SHA so the caller can sparse-checkout that exact ref.
    Repos with no entry, or no commits in 2021–2025, are absent → caller falls
    back to HEAD.
    """
    if not os.path.exists(COMMITS_YEARS_FILE):
        return {}
    by_repo: dict[str, dict[int, tuple[int, str]]] = {}
    head_by_repo: dict[str, str] = {}
    with open(COMMITS_YEARS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            yr_raw = (row.get("year") or "").strip()
            sha = (row.get("last_sha") or "").strip()
            if yr_raw == "HEAD":
                if sha:
                    head_by_repo[slug] = sha
                continue
            try:
                year = int(yr_raw)
                commits = int((row.get("commits") or "0").strip())
            except ValueError:
                continue
            if 2021 <= year <= 2025:
                by_repo.setdefault(slug, {})[year] = (commits, sha)
    out: dict[str, tuple[str, str]] = {}
    for slug, years in by_repo.items():
        for y in (2025, 2024, 2023, 2022, 2021):
            entry = years.get(y)
            if entry and entry[0] > 0 and entry[1]:
                out[slug] = (str(y), entry[1])
                break
    # Dormant repos — no per-year sha but a HEAD pseudo-row resolved by
    # `src.sources.git.resolve_head`. Use the HEAD sha so sha-pinned analysis
    # still produces a long-format row for them.
    for slug, sha in head_by_repo.items():
        if slug not in out:
            out[slug] = ("HEAD", sha)
    return out


def _load_scc_complexity() -> dict[str, int]:
    """Map repo → scc complexity for the comparison table.

    Reads the long-format `data/sources/git/scc.csv`. For repos with multiple
    snapshots takes the lexicographically-largest sha (deterministic).
    """
    if not os.path.exists(SCC_LONG_FILE):
        return {}
    rows = _read_long(SCC_LONG_FILE)
    best: dict[str, tuple[str, int]] = {}
    for (repo, sha, metric), row in rows.items():
        if metric != "complexity":
            continue
        slug = (repo or "").strip().lower()
        if not slug:
            continue
        try:
            val = int(float(row.get("value") or 0))
        except ValueError:
            continue
        prev = best.get(slug)
        if prev is None or sha > prev[0]:
            best[slug] = (sha, val)
    return {slug: val for slug, (_sha, val) in best.items()}


# ──────────────────────────── analysis dataclass ───────────────────────────

@dataclass
class RepoComplexity:
    repo: str
    repo_id: str = ""
    analyzed_sha: str = ""
    analyzed_year: str = "current"
    files: int = 0
    cyclomatic_total: int = 0
    cyclomatic_avg: float = 0.0
    cyclomatic_max: int = 0
    elapsed_s: float = 0.0
    download_s: float = 0.0
    analysis_s: float = 0.0
    error: str = ""


# ────────────────────────────── analysis core ──────────────────────────────

def _list_source_files(root: str) -> list[str]:
    """Walk `root` and return source files matching SOURCE_SUFFIXES.

    Files larger than MAX_FILE_BYTES are skipped — minified / generated /
    vendored blobs that would OOM-kill lizard and aren't real code.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip VCS metadata
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SOURCE_SUFFIXES:
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(path)
    return out


def _run_lizard(files: list[str]) -> tuple[int, int, float, int]:
    """Return (n_funcs, total_ccn, avg_ccn, max_ccn) using lizard.

    Lizard skips files it doesn't understand silently — those just don't
    contribute to the CCN count. Fortran files are dropped *explicitly*
    (see LIZARD_SKIP_SUFFIXES): lizard claims to parse them but its Fortran
    reader OOM-kills the process on large fixed-form sources, so they can't
    even be handed to `analyze_files`.
    """
    import lizard
    analyzable = [
        f for f in files
        if os.path.splitext(f)[1].lower() not in LIZARD_SKIP_SUFFIXES
    ]
    total = 0
    nfn = 0
    mx = 0
    # analyze_files is a generator; iterate to drive it.
    for r in lizard.analyze_files(analyzable):
        for fn in r.function_list:
            ccn = fn.cyclomatic_complexity
            total += ccn
            if ccn > mx:
                mx = ccn
            nfn += 1
    avg = (total / nfn) if nfn else 0.0
    return nfn, total, avg, mx


def analyze_directory(root: str) -> dict:
    """Run lizard over all source files under `root`.

    Returns a flat dict of metrics ready to attach to a RepoComplexity.
    Pure CPU work — invoked in an isolated subprocess via `--analyze-dir`
    so a crash/OOM on one repo can't poison the run.
    """
    files = _list_source_files(root)
    if not files:
        return {"files": 0}

    # Lizard: per-function McCabe across the supported subset (files it
    # doesn't understand contribute no functions).
    nfn, ccn_total, ccn_avg, ccn_max = _run_lizard(files)

    return {
        "files": len(files),
        "cyclomatic_total": ccn_total,
        "cyclomatic_avg": round(ccn_avg, 2),
        "cyclomatic_max": ccn_max,
    }


def _analyze_dir_isolated(dest: str, timeout: int = ANALYSIS_TIMEOUT) -> dict:
    """Run `analyze_directory` for one repo in an isolated subprocess.

    Each repo's lizard analysis gets its own process group. A crash, an
    OOM-kill or a hang is contained to that one process — it cannot poison
    a shared pool or take sibling repos down with it (the bug that turned a
    few mega-repo failures into a 67-error cascade). On timeout the whole
    process group is SIGKILLed. Raises on timeout / non-zero exit / bad
    output; the caller records that as the single repo's `error`.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.sources.github.fetch_advanced_complexity",
         "--analyze-dir", dest],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip()[-160:]
        raise RuntimeError(f"analysis subprocess exit {proc.returncode}: {tail}")
    # The JSON result is the last non-empty stdout line — robust against any
    # stray output from lizard or imports.
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {"files": 0}


# ─────────────────────────── repo-level processing ─────────────────────────

async def _fetch_and_analyze(
    repo: str, repo_id: str, sha: str, analyzed_year: str, base_dir: str,
    sem: asyncio.Semaphore, client: httpx.Client,
) -> RepoComplexity:
    """Sparse-checkout `repo`@`sha`, analyse in an isolated subprocess, clean up."""
    rc = RepoComplexity(
        repo=repo, repo_id=repo_id,
        analyzed_sha=sha, analyzed_year=analyzed_year,
    )
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    loop = asyncio.get_running_loop()

    async with sem:
        t0 = time.monotonic()
        try:
            # Try tarball first (fast for any size — we don't have repo size
            # here so always try tarball before falling back to sparse-clone).
            try:
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _download_tarball, repo, dest, client, sha or "HEAD",
                )
                rc.download_s = dl_elapsed
            except Exception as e:
                log.debug("tarball failed for %s: %s — sparse-cloning", repo, e)
                if os.path.isdir(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _sparse_clone, repo, dest, 0, sha or None,
                )
                rc.download_s = dl_elapsed

            if not os.path.isdir(dest):
                rc.error = "download failed"
                return rc

            # CPU-bound analysis runs in an isolated subprocess (per-repo
            # process group) so a crash/OOM can't poison sibling repos.
            t_an = time.monotonic()
            metrics = await loop.run_in_executor(
                None, _analyze_dir_isolated, dest,
            )
            rc.analysis_s = time.monotonic() - t_an

            for k, v in metrics.items():
                if hasattr(rc, k):
                    setattr(rc, k, v)

        except subprocess.TimeoutExpired:
            rc.error = "timeout"
        except Exception as e:
            rc.error = str(e)[:80]
        finally:
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            rc.elapsed_s = round(time.monotonic() - t0, 2)

    return rc


async def analyze_repos(
    repos: list[str], shas: dict[str, tuple[str, str]],
    repo_ids: dict[str, str], concurrency: int,
    output_path: str | None = None, flush_every: int = 25,
    max_disk_gb: float = 0.0,
) -> list[RepoComplexity]:
    """Process every repo concurrently. `shas` maps repo → (year, sha).

    If `output_path` is given, persist each successful result via
    `upsert_snapshot` so a long run can survive a crash mid-way.
    If ``max_disk_gb`` > 0, abort gracefully when free /tmp drops below
    that threshold (waits for in-flight repos to finish).
    """
    sem = asyncio.Semaphore(concurrency)
    base_dir = make_clone_tmpdir("cyclo")
    name_width = min(max((len(r) for r in repos), default=20), 38)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}", justify="left"),
        BarColumn(bar_width=14),
        TaskProgressColumn(),
        TextColumn("[dim]{task.completed}/{task.total}[/]"),
        _ETAColumn(),
        console=console,
        transient=True,
    )

    results: list[RepoComplexity] = []
    client = httpx.Client(http2=True)
    # Each repo's analysis runs in its own isolated subprocess (see
    # `_analyze_dir_isolated`); the `sem` caps how many run concurrently.
    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(repos))

            async def _one(repo: str) -> RepoComplexity:
                year, sha = shas.get(repo, ("current", ""))
                rc = await _fetch_and_analyze(
                    repo, repo_ids.get(repo, ""), sha, year,
                    base_dir, sem, client,
                )
                progress.update(task, advance=1, description=repo[:name_width].ljust(name_width))
                return rc

            tasks = [_one(r) for r in repos]
            aborted = False
            for coro in asyncio.as_completed(tasks):
                rc = await coro
                results.append(rc)
                if output_path and len(results) % flush_every == 0:
                    _write_results(output_path, [r for r in results if not r.error])
                if max_disk_gb > 0 and not aborted:
                    if not check_disk_or_exit(max_disk_gb, console=console):
                        aborted = True
                        break
    finally:
        client.close()
        shutil.rmtree(base_dir, ignore_errors=True)
    return results


# ────────────────────────────── persistence ────────────────────────────────

def _filter_by_ttl(
    repos: list[str], path: str, ttl_days: int,
    targets: dict[str, tuple[str, str]],
) -> tuple[list[str], int]:
    """Skip repos whose (repo, target_sha) already has a fresh cyclomatic_total row.

    "Fresh" means `checked_at` within `ttl_days`. `ttl_days <= 0` disables
    the filter (always re-run). Repos whose target sha changed since last
    write are NOT skipped — we always reanalyze a new sha.
    """
    if ttl_days <= 0 or not os.path.exists(path):
        return repos, 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    rows = _read_long(path)
    fresh: set[str] = set()
    for r in repos:
        _yr, sha = targets.get(r, ("current", ""))
        if not sha:
            continue
        # A repo is "fresh" iff the canary metric `cyclomatic_total` is present
        # at the target sha and its checked_at is within the TTL window.
        row = rows.get((r, sha, "cyclomatic_total"))
        if not row:
            continue
        ts = row.get("checked_at", "")
        if not ts:
            continue
        try:
            fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if fetched >= cutoff:
            fresh.add(r)
    filtered = [r for r in repos if r not in fresh]
    return filtered, len(repos) - len(filtered)


def _write_results(path: str, results: list[RepoComplexity]) -> None:
    """Upsert each successful result into `data/sources/git/lizard.csv`.

    Drops results without an `analyzed_sha` (HEAD-resolved snapshots can't be
    pinned in the long format). `elapsed_s` is logged separately, never
    persisted.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    for rc in results:
        if rc.error or not rc.analyzed_sha:
            continue
        metrics = {m: getattr(rc, m) for m in CYCLO_METRICS}
        upsert_snapshot(
            path,
            repo=rc.repo,
            repo_id=rc.repo_id,
            commit_sha=rc.analyzed_sha,
            metrics=metrics,
            checked_at=now,
        )
        written += 1
    log.debug("wrote %d snapshots to %s", written, path)


# ──────────────────────────────── reporting ────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _print_comparison(results: list[RepoComplexity], scc_map: dict[str, int]) -> None:
    """Inline rich table — repo × (scc complexity, lizard CCN, time)."""
    ok = [r for r in results if not r.error]
    if not ok:
        console.print("[red]No successful results to display[/red]")
        return

    tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Repo")
    tbl.add_column("Yr", justify="right")
    tbl.add_column("Files", justify="right")
    tbl.add_column("scc", justify="right")
    tbl.add_column("CCN", justify="right")
    tbl.add_column("avg", justify="right")
    tbl.add_column("max", justify="right")
    tbl.add_column("t (s)", justify="right")

    for rc in sorted(ok, key=lambda r: -r.elapsed_s):
        scc = scc_map.get(rc.repo, 0)
        tbl.add_row(
            rc.repo,
            rc.analyzed_year,
            f"{rc.files:,}",
            f"{scc:,}" if scc else "-",
            f"{rc.cyclomatic_total:,}",
            f"{rc.cyclomatic_avg:.1f}",
            f"{rc.cyclomatic_max:,}",
            f"{rc.elapsed_s:.1f}",
        )

    console.print(tbl)


def _write_markdown_report(
    path: str, results: list[RepoComplexity], scc_map: dict[str, int],
    total_elapsed: float,
) -> None:
    """Persist a side-by-side comparison report for the user to read later."""
    ok = [r for r in results if not r.error]
    fail = [r for r in results if r.error]
    times = [r.elapsed_s for r in ok]

    lines: list[str] = [
        "# Cyclomatic complexity (lizard) vs. scc",
        "",
        f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Library",
        "",
        "**lizard** — per-function McCabe cyclomatic complexity, multi-language",
        "(C/C++/JS/TS/Python/Rust/Java/Go/…). scc's `complexity` is only a",
        "branch-keyword tally; this is the real per-function metric.",
        "",
        "## Comparison table",
        "",
        "| Repo | Year | Files | scc | CCN total | CCN avg | CCN max | t (s) |",
        "|------|-----:|------:|----:|----------:|--------:|--------:|------:|",
    ]
    for rc in sorted(ok, key=lambda r: -r.elapsed_s):
        scc = scc_map.get(rc.repo, 0)
        lines.append(
            f"| {rc.repo} | {rc.analyzed_year} | {rc.files:,} | "
            f"{scc:,} | {rc.cyclomatic_total:,} | {rc.cyclomatic_avg:.1f} | "
            f"{rc.cyclomatic_max} | {rc.elapsed_s:.1f} |"
        )

    lines += ["", "## Performance", ""]
    if times:
        lines += [
            f"- repos analysed: **{len(ok)}** (errors: {len(fail)})",
            f"- total wallclock: **{total_elapsed:.1f}s**",
            f"- per-repo time: min {min(times):.1f}s · p50 {_percentile(times, 0.5):.1f}s · "
            f"p90 {_percentile(times, 0.9):.1f}s · max {max(times):.1f}s",
        ]
        # Estimate full-set wallclock by mean repo time / concurrency *baked in*
        # but for transparency we just project mean × repos / concurrency_factor.
        mean_per_repo = sum(times) / len(times)
        # Real wallclock includes parallelism — extrapolate as scaling factor.
        scale = 899 / len(ok)
        lines.append(
            f"- projected full-risk-scope wallclock (×{scale:.1f}): "
            f"**~{total_elapsed * scale / 60:.1f} min** "
            f"(mean per-repo {mean_per_repo:.1f}s)"
        )
    if fail:
        lines += ["", "## Errors", ""]
        for rc in fail:
            lines.append(f"- `{rc.repo}` — {rc.error}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ──────────────────────────────── main ────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute cyclomatic complexity (McCabe) per repo via lizard.",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Sample N random risk-scope repos (default: {DEFAULT_LIMIT}, 0 = all).",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help=f"Random seed for reproducible sampling (default: {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel workers (default: {DEFAULT_CONCURRENCY}, CPU-bound). "
             "Override with $CYCLO_WORKERS.",
    )
    parser.add_argument(
        "--max-disk-gb", type=float, default=2.0,
        help="Abort gracefully if free /tmp dips below this "
             "(default: 2.0; set 0 to disable).",
    )
    parser.add_argument(
        "--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
        help=f"Skip repos analysed within N days (default: {DEFAULT_TTL_DAYS}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore TTL — always re-analyze (equivalent to --ttl-days 0).",
    )
    parser.add_argument(
        "--repos", nargs="+", default=None,
        help="Explicit list of repo slugs (e.g. 'owner/name'). Overrides --limit/--seed.",
    )
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument(
        "--analyze-dir", default=None,
        help="Internal: analyse one already-cloned directory, print metrics "
             "as JSON, exit. Used for isolated per-repo analysis.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Isolated-analysis subprocess entrypoint — print JSON and exit before
    # any banner / logging / network setup.
    if args.analyze_dir:
        print(json.dumps(analyze_directory(args.analyze_dir)))
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ── startup banner ──
    started = datetime.datetime.now()
    console.print()
    console.print(
        f"[bold]Advanced complexity[/bold] (lizard McCabe)  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · {started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    risk_repos = load_risk_repos()
    repo_ids: dict[str, str] = {e.repo: e.repo_id for e in risk_repos}
    repos_all = [e.repo for e in risk_repos]

    # Repo selection: explicit --repos > --limit/--seed sample > full set.
    if args.repos:
        explicit = [r.strip().lower() for r in args.repos if r.strip()]
        risk_set = set(repos_all)
        repos = [r for r in explicit if r in risk_set]
        unknown = [r for r in explicit if r not in risk_set]
        if unknown:
            console.print(f"[yellow]Skipping non-risk-scope: {', '.join(unknown)}[/yellow]")
    else:
        rng = random.Random(args.seed)
        if args.limit and args.limit < len(repos_all):
            repos = rng.sample(repos_all, args.limit)
        else:
            repos = list(repos_all)

    # Resolve target SHAs first — TTL skip needs (repo, sha) keys.
    sha_map = _load_target_year_shas()
    targets: dict[str, tuple[str, str]] = {
        r: sha_map.get(r, ("current", "")) for r in repos
    }

    ttl = 0 if args.force else args.ttl_days
    repos, ttl_skipped = _filter_by_ttl(repos, args.output, ttl, targets)
    if not repos:
        console.print(f"[dim]All sampled repos fresh (TTL {ttl}d) — nothing to do.[/dim]")
        return

    console.print(
        f"[dim]processing {len(repos)} repos[/dim]"
        f"{f' · skipped {ttl_skipped} fresh' if ttl_skipped else ''}"
    )
    console.print()

    t_start = time.monotonic()
    results = asyncio.run(analyze_repos(
        repos, targets, repo_ids, args.concurrency,
        output_path=args.output, flush_every=25,
        max_disk_gb=args.max_disk_gb,
    ))
    elapsed = time.monotonic() - t_start

    ok = [r for r in results if not r.error]
    errors = [r for r in results if r.error]

    if ok:
        _write_results(args.output, ok)

    # Inline comparison table.
    scc_map = _load_scc_complexity()
    _print_comparison(results, scc_map)
    console.print()

    # Summary stats.
    times = [r.elapsed_s for r in ok]
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=14)
    summary.add_column()
    summary.add_row("Analysed", f"[bold]{len(ok)}[/bold] repos")
    if errors:
        summary.add_row(
            "Errors",
            f"[red]{len(errors)}[/red] [dim]("
            + ", ".join(f"{r.repo}: {r.error}" for r in errors[:3])
            + ")[/dim]",
        )
    summary.add_row("Wallclock", f"[bold]{elapsed:.1f}s[/bold]")
    if times:
        summary.add_row(
            "Per-repo (s)",
            f"min {min(times):.1f} · p50 {_percentile(times, 0.5):.1f} · "
            f"p90 {_percentile(times, 0.9):.1f} · max {max(times):.1f}",
        )
        scale = 899 / len(ok)
        summary.add_row(
            "Full-set est.",
            f"~{elapsed * scale / 60:.1f} min "
            f"[dim](×{scale:.1f} for 899 repos at this concurrency)[/dim]",
        )
    summary.add_row("Output CSV", args.output)
    summary.add_row("Markdown", COMPARISON_FILE)
    console.print(summary)
    console.print()

    _write_markdown_report(COMPARISON_FILE, results, scc_map, elapsed)


if __name__ == "__main__":
    main()
