"""Compute *real* cyclomatic complexity (McCabe) and Halstead metrics per repo.

Why this exists:
    `src/git/fetch_scc.py` already runs `scc` for fast LOC/branch counts, but
    scc's "complexity" is just a count of branch-statement keywords — not real
    McCabe per-function complexity, and it has no Halstead at all. For finer
    sustainability/risk modelling we want both, multi-language.

Library choice (after benchmarking on aiohttp + acorn):

    * lizard (https://github.com/terryyin/lizard)
        - Per-function McCabe via a fast hand-rolled tokenizer.
        - Built-in C/C++/JS/TS/Python/Rust/Java/Go/etc. support — covers all
          four ecosystems we ship (npm, pypi, crates, cpp).
        - ~10× faster than multimetric on the same file set (0.5s vs 4.9s
          for aiohttp's 167 .py files).
        - **No Halstead.**

    * multimetric (https://github.com/priv-kweihmann/multimetric)
        - Multi-language via pygments lexers; produces both McCabe + Halstead.
        - **Bug**: its repo-level `cyclomatic_complexity` is computed as
          `sum(conditions) - sum(exits) + 2` across *all* files combined, so
          for any non-trivial repo it clamps to 0 (e.g. aiohttp reports 0).
          Per-file values are correct, so we sum those manually.
        - Halstead repo-level numbers ARE correct: it concatenates per-file
          operator/operand lists then computes V/D/E/B from the unioned
          vocabulary. We rely on that.
        - Slower (pygments tokenization + multiprocessing.Pool overhead).
        - Maintainability Index: classic formula
          `MI = max(0, 171 - 5.2*ln(V) - 0.23*CCN - 16.2*ln(LOC))` is derived
          from per-module averages; on a whole repo it always clamps to 0
          (e.g. aiohttp: 171 - 5.2*ln(6.25M) - 0.23*2017 - 16.2*ln(67k) → ≪ 0).
          We instead compute MI **per-file** and average across files so the
          number is meaningful.

Hybrid strategy:
    1. lizard → per-function McCabe → cyclomatic_total/avg/max
    2. multimetric (Python API, in-process) → per-file Halstead, summed using
       multimetric's own global aggregator (correct), plus per-file MI averaged.

Period semantics:
    For each repo, look up `data/github/git/commits-years.csv` to find the
    most recent year ≤ 2025 with commits>0; use that year's `last_sha`. If
    no per-year SHA is recorded fall back to HEAD (and we DO NOT persist
    HEAD-resolved snapshots — without a pinned sha we can't key them in the
    long format). Same logic as `src.pipeline.build_complexity._load_target_year()`.

Output format:
    Writes long-format rows to `data/git/lizard.csv` (shared with
    `fetch_cognitive`) via `src.git.long_format.upsert_snapshot`. Each row is
    `(repo, repo_id, commit_sha, metric, value, checked_at)`.

    Metrics emitted by this fetcher:
        files, cyclomatic_total, cyclomatic_avg, cyclomatic_max,
        halstead_volume, halstead_difficulty, halstead_effort, halstead_bugs,
        maintainability_index

    Order note: this fetcher and `fetch_cognitive` both write a `files`
    metric for the same (repo, sha). Whichever runs LAST wins on that key.
    In practice the two scan the same source-suffix set so values agree
    closely; if they differ this fetcher's `files` will overwrite cognitive's
    when run after. `elapsed_s` is logged but NOT persisted.

Usage:
    uv run python -m src.github.fetch_advanced_complexity --limit 10
    uv run python -m src.github.fetch_advanced_complexity --limit 0   # full set
    uv run python -m src.github.fetch_advanced_complexity --repos a/b c/d
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import datetime
import io
import logging
import math
import os
import random
import shutil
import statistics
import subprocess
import tempfile
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

# requests (transitively imported by something multimetric pulls in) emits a
# RequestsDependencyWarning at import time about urllib3/chardet versions —
# silence it before the import cascade fires.
warnings.filterwarnings("ignore", message=".*urllib3.*chardet.*")
warnings.filterwarnings("ignore", message=".*RequestsDependencyWarning.*")

# All third-party / project imports must come after warnings.filterwarnings()
# above so multimetric's transitive `requests` import doesn't print a
# RequestsDependencyWarning. ruff's E402 doesn't see warnings setup as a
# module-level statement worth respecting, so silence it for this block.
import httpx  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import (  # noqa: E402
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table  # noqa: E402

from src.git.clone import SOURCE_EXTS  # noqa: E402
from src.git.clone import download_tarball as _download_tarball  # noqa: E402
from src.git.clone import sparse_clone as _sparse_clone  # noqa: E402
from src.git.disk import check_disk_or_exit, print_disk_banner  # noqa: E402
from src.git.long_format import read as _read_long  # noqa: E402
from src.git.long_format import upsert_snapshot  # noqa: E402
from src.github.display import _ETAColumn  # noqa: E402
from src.pipeline.repos import load_eligible_repos  # noqa: E402

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = "data"
ELIGIBILITY_FILE = f"{DATA_DIR}/eligibility-data.csv"
COMMITS_YEARS_FILE = f"{DATA_DIR}/github/git/commits-years.csv"
SCC_LONG_FILE = f"{DATA_DIR}/git/scc.csv"
OUTPUT_FILE = f"{DATA_DIR}/git/lizard.csv"
COMPARISON_FILE = "/tmp/cyclo-halstead-vs-scc.md"

DEFAULT_LIMIT = 10
DEFAULT_SEED = 42
DEFAULT_CONCURRENCY = int(os.environ.get("CYCLO_WORKERS") or 4)
DEFAULT_TTL_DAYS = 0  # 0 = always re-run

# Metrics this fetcher emits per snapshot to data/git/lizard.csv.
CYCLO_METRICS: tuple[str, ...] = (
    "files",
    "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
    "halstead_volume", "halstead_difficulty", "halstead_effort", "halstead_bugs",
    "maintainability_index",
)

# File extensions worth analyzing — same set the sparse-checkout uses.
# Build a set of suffixes (".py", ".js", …) for quick filtering after checkout.
SOURCE_SUFFIXES: set[str] = {ext.lstrip("*").lower() for ext in SOURCE_EXTS}


# ────────────────────────── target-SHA resolution ──────────────────────────

def _load_target_year_shas() -> dict[str, tuple[str, str]]:
    """Map repo → (year_str, last_sha) for the most recent year ≤2025 with commits>0.

    Mirrors `src.pipeline.build_complexity._load_target_year` but also returns
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
    # `src.git.resolve_head`. Use the HEAD sha so sha-pinned analysis
    # still produces a long-format row for them.
    for slug, sha in head_by_repo.items():
        if slug not in out:
            out[slug] = ("HEAD", sha)
    return out


def _load_scc_complexity() -> dict[str, int]:
    """Map repo → scc complexity for the comparison table.

    Reads the long-format `data/git/scc.csv`. For repos with multiple
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
    halstead_volume: float = 0.0
    halstead_difficulty: float = 0.0
    halstead_effort: float = 0.0
    halstead_bugs: float = 0.0
    maintainability_index: float = 0.0
    elapsed_s: float = 0.0
    download_s: float = 0.0
    analysis_s: float = 0.0
    error: str = ""


# ────────────────────────────── analysis core ──────────────────────────────

def _list_source_files(root: str) -> list[str]:
    """Walk `root` and return every source file matching SOURCE_SUFFIXES."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip VCS metadata
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SOURCE_SUFFIXES:
                out.append(os.path.join(dirpath, fn))
    return out


def _run_lizard(files: list[str]) -> tuple[int, int, float, int]:
    """Return (n_funcs, total_ccn, avg_ccn, max_ccn) using lizard.

    Lizard skips files it doesn't understand silently — that's fine, we'd just
    miss them in the CCN count (they still contribute via multimetric Halstead).
    """
    import lizard
    total = 0
    nfn = 0
    mx = 0
    # analyze_files is a generator; iterate to drive it.
    for r in lizard.analyze_files(files):
        for fn in r.function_list:
            ccn = fn.cyclomatic_complexity
            total += ccn
            if ccn > mx:
                mx = ccn
            nfn += 1
    avg = (total / nfn) if nfn else 0.0
    return nfn, total, avg, mx


class _MultimetricArgs:
    """Stand-in for argparse Namespace expected by multimetric internals.

    We bypass multimetric's own `parse_args()` because it installs noisy
    StreamHandlers on the `stdout`/`stderr` loggers (those handlers capture
    a reference to `sys.stderr` at construction, so later `redirect_stderr`
    can't silence them). Constructing args manually keeps the logger silent.
    """
    halstead_bug_predict_method = "new"
    maintenance_index_calc_method = "classic"
    warn_compiler = warn_duplication = warn_functional = None
    warn_standard = warn_security = coverage = None
    dump = False
    verbose = False
    jobs = 1
    files = ()


def _run_multimetric(files: list[str]) -> dict:
    """Run multimetric in-process (no subprocess, no logger noise).

    We bypass multimetric's `run()` because it spawns an `mp.Pool` worker
    that prints chardet UnicodeDecodeErrors to stderr from inside the
    subprocess (where `redirect_stderr` from this process can't reach).
    By calling `file_process` directly per file in our own process we can
    cleanly silence per-file decode errors via redirect_stderr.
    """
    from multimetric.__main__ import file_process
    from multimetric.cls.importer.pick import importer_pick
    from multimetric.cls.modules import (
        get_modules_calculated,
        get_modules_metrics,
        get_modules_stats,
    )

    args = _MultimetricArgs()

    importer = {}
    importer["import_compiler"] = importer_pick(args, args.warn_compiler)
    importer["import_coverage"] = importer_pick(args, args.coverage)
    importer["import_duplication"] = importer_pick(args, args.warn_duplication)
    importer["import_functional"] = importer_pick(args, args.warn_functional)
    importer["import_security"] = importer_pick(args, args.warn_standard)
    importer["import_standard"] = importer_pick(args, args.warn_security)
    importer = {k: v for k, v in importer.items() if v}

    overall_metrics = get_modules_metrics(args, **importer)
    overall_calc = get_modules_calculated(args, **importer)

    out = {"files": {}, "overall": {}}
    results = []
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        for fpath in files:
            try:
                res = file_process(fpath, args, importer)
                results.append(res)
                out["files"][res[1]] = res[0]
            except Exception:  # per-file: skip and continue
                continue

        for y in overall_metrics:
            out["overall"].update(
                y.get_results_global([x[4] for x in results]),
            )
        for y in overall_calc:
            out["overall"].update(y.get_results(out["overall"]))
        for m in get_modules_stats(args, **importer):
            out = m.get_results(out, "files", "overall")

    return out


def _classic_mi(volume: float, ccn: float, loc: float) -> float:
    """Classic Maintainability Index for a single module/file, scaled 0-100.

    Raw MI = 171 − 5.2·ln(V) − 0.23·CCN − 16.2·ln(LOC)

    The classic formula is unbounded; we follow the Microsoft convention
    of clamping to [0, 100] (their thresholds: 0-9 red, 10-19 yellow,
    20+ green). Tiny files would otherwise score >100 and skew averages.
    """
    if volume <= 0 or loc <= 0:
        return 0.0
    raw = 171.0 - 5.2 * math.log(volume) - 0.23 * ccn - 16.2 * math.log(loc)
    return max(0.0, min(100.0, raw))


def analyze_directory(root: str) -> dict:
    """Run lizard + multimetric over all source files under `root`.

    Returns a flat dict of metrics ready to attach to a RepoComplexity.
    Pure CPU work — safe to call inside a ProcessPoolExecutor worker.
    """
    files = _list_source_files(root)
    if not files:
        return {"files": 0}

    # Lizard: per-function McCabe across the supported subset (the ones it
    # doesn't understand return zero functions and contribute nothing).
    nfn, ccn_total, ccn_avg, ccn_max = _run_lizard(files)

    # Multimetric: per-file Halstead + per-file MI (overall aggregator gives
    # correct Halstead, broken cyclomatic — see module docstring).
    mm = _run_multimetric(files)
    overall = mm.get("overall", {})

    # Per-file MI: average non-zero MIs for a meaningful number.
    per_file = mm.get("files", {})
    mi_vals: list[float] = []
    for fpath, m in per_file.items():
        v = m.get("halstead_volume", 0.0)
        cc = m.get("cyclomatic_complexity", 0.0)
        loc = m.get("loc", 0.0)
        mi = _classic_mi(v, cc, loc)
        if mi > 0:
            mi_vals.append(mi)
    mi_avg = statistics.mean(mi_vals) if mi_vals else 0.0

    return {
        "files": len(per_file),
        "cyclomatic_total": ccn_total,
        "cyclomatic_avg": round(ccn_avg, 2),
        "cyclomatic_max": ccn_max,
        "halstead_volume": round(overall.get("halstead_volume", 0.0), 2),
        "halstead_difficulty": round(overall.get("halstead_difficulty", 0.0), 2),
        "halstead_effort": round(overall.get("halstead_effort", 0.0), 2),
        "halstead_bugs": round(overall.get("halstead_bugprop", 0.0), 2),
        "maintainability_index": round(mi_avg, 2),
    }


# ─────────────────────────── repo-level processing ─────────────────────────

async def _fetch_and_analyze(
    repo: str, repo_id: str, sha: str, analyzed_year: str, base_dir: str,
    sem: asyncio.Semaphore, pool: ProcessPoolExecutor, client: httpx.Client,
) -> RepoComplexity:
    """Sparse-checkout `repo`@`sha`, run analysis in worker process, clean up."""
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

            # CPU-bound analysis runs in a worker process.
            t_an = time.monotonic()
            metrics = await loop.run_in_executor(pool, analyze_directory, dest)
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
    base_dir = tempfile.mkdtemp(prefix="cyclo-halstead-")
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
    # CPU-bound analysis runs in worker processes — we want at most `concurrency`
    # concurrent analyses. Adding a couple of slack workers helps overlap analysis
    # with downloads.
    pool = ProcessPoolExecutor(max_workers=concurrency + 2)
    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(repos))

            async def _one(repo: str) -> RepoComplexity:
                year, sha = shas.get(repo, ("current", ""))
                rc = await _fetch_and_analyze(
                    repo, repo_ids.get(repo, ""), sha, year,
                    base_dir, sem, pool, client,
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
        pool.shutdown(wait=True, cancel_futures=False)
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
    """Upsert each successful result into `data/git/lizard.csv`.

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
    """Inline rich table — repo × (scc CCN, our CCN, halstead, time)."""
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
    tbl.add_column("max", justify="right")
    tbl.add_column("V", justify="right")
    tbl.add_column("D", justify="right")
    tbl.add_column("E", justify="right")
    tbl.add_column("Bugs", justify="right")
    tbl.add_column("MI", justify="right")
    tbl.add_column("t (s)", justify="right")

    for rc in sorted(ok, key=lambda r: -r.elapsed_s):
        scc = scc_map.get(rc.repo, 0)
        tbl.add_row(
            rc.repo,
            rc.analyzed_year,
            f"{rc.files:,}",
            f"{scc:,}" if scc else "-",
            f"{rc.cyclomatic_total:,}",
            f"{rc.cyclomatic_max:,}",
            _fmt_num(rc.halstead_volume),
            f"{rc.halstead_difficulty:.0f}",
            _fmt_num(rc.halstead_effort),
            f"{rc.halstead_bugs:.1f}",
            f"{rc.maintainability_index:.1f}",
            f"{rc.elapsed_s:.1f}",
        )

    console.print(tbl)


def _fmt_num(n: float) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f}G"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}k"
    return f"{n:.0f}"


def _write_markdown_report(
    path: str, results: list[RepoComplexity], scc_map: dict[str, int],
    total_elapsed: float,
) -> None:
    """Persist a side-by-side comparison report for the user to read later."""
    ok = [r for r in results if not r.error]
    fail = [r for r in results if r.error]
    times = [r.elapsed_s for r in ok]

    lines: list[str] = [
        "# Cyclomatic / Halstead vs. scc",
        "",
        f"Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Library choice",
        "",
        "Hybrid: **lizard** for per-function McCabe (multi-language, ~10× faster than",
        "multimetric on the same files) + **multimetric** for Halstead V/D/E/B (the",
        "only multi-language Halstead implementation on PyPI).",
        "",
        "**Bug found in multimetric**: its repo-level `cyclomatic_complexity` is",
        "computed as `sum(branch_keywords) - sum(exit_keywords) + 2` across the whole",
        "corpus — for any non-trivial repo this clamps to 0 (e.g. aiohttp reports 0).",
        "Per-file values are correct; we only use multimetric for Halstead and",
        "compute MI per-file before averaging.",
        "",
        "## Comparison table",
        "",
        "| Repo | Year | Files | scc CCN | CCN total | CCN max | V | D | E | Bugs | MI | t (s) |",
        "|------|-----:|------:|--------:|----------:|--------:|--:|--:|--:|-----:|---:|-----:|",
    ]
    for rc in sorted(ok, key=lambda r: -r.elapsed_s):
        scc = scc_map.get(rc.repo, 0)
        lines.append(
            f"| {rc.repo} | {rc.analyzed_year} | {rc.files:,} | "
            f"{scc:,} | {rc.cyclomatic_total:,} | {rc.cyclomatic_max} | "
            f"{rc.halstead_volume:,.0f} | {rc.halstead_difficulty:.1f} | "
            f"{rc.halstead_effort:,.0f} | {rc.halstead_bugs:.2f} | "
            f"{rc.maintainability_index:.1f} | {rc.elapsed_s:.1f} |"
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
            f"- projected full-eligible-set wallclock (×{scale:.1f}): "
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
        description="Compute cyclomatic complexity (McCabe) + Halstead per repo.",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Sample N random eligible repos (default: {DEFAULT_LIMIT}, 0 = all).",
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ── startup banner ──
    started = datetime.datetime.now()
    console.print()
    console.print(
        f"[bold]Advanced complexity[/bold] (lizard + multimetric)  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · {started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    print_disk_banner(console=console)
    console.print()

    eligible = load_eligible_repos()
    repo_ids: dict[str, str] = {e.repo: e.repo_id for e in eligible}
    repos_all = [e.repo for e in eligible]

    # Repo selection: explicit --repos > --limit/--seed sample > full set.
    if args.repos:
        explicit = [r.strip().lower() for r in args.repos if r.strip()]
        eligible_set = set(repos_all)
        repos = [r for r in explicit if r in eligible_set]
        unknown = [r for r in explicit if r not in eligible_set]
        if unknown:
            console.print(f"[yellow]Skipping non-eligible: {', '.join(unknown)}[/yellow]")
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
