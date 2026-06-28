"""Compute Sonar-style **cognitive complexity** per repo.

Why this exists:
    `src/sources/git/fetch_scc.py` (scc) gives a fast keyword-count "complexity" that
    barely correlates with how *hard a function is to read*. McCabe's
    cyclomatic number (computed by `fetch_advanced_complexity.py` via lizard)
    counts independent paths but treats `if (a) if (b) if (c)` exactly the
    same as three sibling ifs — both score 4. Cognitive complexity (Sonar,
    G. Ann Campbell, 2018) explicitly **penalises nesting**: the deeper a
    control structure sits inside other ones, the more it costs.

    See https://www.sonarsource.com/docs/CognitiveComplexity.pdf

Library choice — there is **no** off-the-shelf library that ships
cognitive complexity for every language we ship (npm/JS+TS, pypi/Python,
crates/Rust, cpp). The Lizard tokenizer's `--Wcognitive` flag rumoured in
some changelogs does not exist in the released v1.22 we depend on; the
only built-in nesting metrics are NS (max nested structures) and ND (max
nesting depth), neither of which is Sonar cognitive complexity.

Hybrid strategy used here:

    * **Python files** → `cognitive_complexity` package (pure-AST; matches
      Sonar's reference algorithm exactly for `def`/`async def` nodes).
    * **Everything else** (C, C++, Java, JS, TS, Rust, Go, Swift, …) →
      a Lizard extension (`SonarCognitiveExt`) that walks Lizard's token
      stream and applies the Sonar rules using brace-depth tracking. This
      covers any language Lizard understands (~25), at the cost of small
      mis-counts on edge cases (lambdas, ternaries inside conditions).

We deliberately do **not** fall back to cyclomatic-as-a-proxy: cognitive
captures different signal (nesting penalty, logical-operator chain toggles)
that the cyclo-halstead pass already covers separately.

Period semantics:
    For each repo, look up `data/sources/github/git/commits-years.csv` to find the
    most recent year ≤ 2025 with commits>0; use that year's `last_sha`. If
    no per-year SHA is recorded fall back to HEAD (and we DO NOT persist
    HEAD-resolved snapshots — without a pinned sha we can't key them in the
    long format). Same logic as `fetch_advanced_complexity.py` so the two
    outputs share `analyzed_year`.

Output format:
    Writes long-format rows to `data/sources/git/lizard.csv` (shared with
    `fetch_advanced_complexity`) via `src.sources.git.long_format.upsert_snapshot`.
    Each row is `(repo, repo_id, commit_sha, metric, value, checked_at)`.

    Metrics emitted by this fetcher:
        files, cognitive_total, cognitive_avg, cognitive_max

    Order note: this fetcher and `fetch_advanced_complexity` both write a
    `files` metric for the same (repo, sha). Whichever runs LAST wins on
    that key. In practice the two scan the same source-suffix set so values
    agree closely. `elapsed_s` is logged but NOT persisted.

Usage:
    uv run python -m src.sources.github.fetch_cognitive --limit 10
    uv run python -m src.sources.github.fetch_cognitive --limit 0   # full set
    uv run python -m src.sources.github.fetch_cognitive --repos a/b c/d
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import datetime
import logging
import math
import os
import random
import shutil
import statistics
import subprocess
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

# Lizard 1.22 imports trigger a urllib3/chardet RequestsDependencyWarning
# transitively — silence at the source like fetch_advanced_complexity does.
warnings.filterwarnings("ignore", message=".*urllib3.*chardet.*")
warnings.filterwarnings("ignore", message=".*RequestsDependencyWarning.*")

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn,
)
from rich.table import Table

from src.sources.git.clone import SOURCE_EXTS
from src.sources.git.clone import corrected_clone_sha
from src.sources.git.clone import download_tarball as _download_tarball
from src.sources.git.clone import sparse_clone as _sparse_clone
from src.sources.git.disk import check_disk_or_exit, make_clone_tmpdir, print_disk_banner, sweep_stale_clone_dirs
from src.sources.git.long_format import read as _read_long
from src.sources.git.long_format import upsert_snapshot
from src.sources.github.display import _ETAColumn
from src.common.repos import load_default_branches, load_top_repos


log = logging.getLogger(__name__)
console = Console()

DATA_DIR = "data/sources"
COMMITS_YEARS_FILE = f"{DATA_DIR}/github/git/commits-years.csv"
SCC_LONG_FILE = f"{DATA_DIR}/git/scc.csv"
LIZARD_LONG_FILE = f"{DATA_DIR}/git/lizard.csv"
OUTPUT_FILE = LIZARD_LONG_FILE

DEFAULT_LIMIT = 0
DEFAULT_SEED = 42
DEFAULT_CONCURRENCY = int(os.environ.get("COGNITIVE_WORKERS") or 4)
DEFAULT_TTL_DAYS = 30

# Metrics this fetcher emits per snapshot to data/sources/git/lizard.csv.
COGNITIVE_METRICS: tuple[str, ...] = (
    "files",
    "cognitive_total", "cognitive_avg", "cognitive_max",
)

# File extensions worth analyzing — same set the sparse-checkout uses.
SOURCE_SUFFIXES: set[str] = {ext.lstrip("*").lower() for ext in SOURCE_EXTS}
PY_SUFFIXES: set[str] = {".py", ".pyi", ".pyw"}

# Files larger than this are skipped — minified bundles / generated parsers
# / vendored amalgamations are the real lizard OOM trigger and aren't
# hand-written code. Mirrors fetch_advanced_complexity.MAX_FILE_BYTES.
MAX_FILE_BYTES = 2_000_000


# ────────────────────────── target-SHA resolution ──────────────────────────

def _load_target_year_shas() -> dict[str, tuple[str, str]]:
    """Map repo → (year_str, last_sha) for the most recent year ≤2025 with commits>0.

    Mirrors `fetch_advanced_complexity._load_target_year_shas` so both passes
    target the same SHA. Repos absent here fall back to HEAD.
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
    for slug, sha in head_by_repo.items():
        if slug not in out:
            out[slug] = ("HEAD", sha)
    return out


def _load_scc_complexity() -> dict[str, int]:
    """Map repo → scc complexity (the keyword-count proxy) for comparison.

    Reads the long-format `data/sources/git/scc.csv`. For repos with multiple
    snapshots takes the lexicographically-largest sha (deterministic).
    """
    if not os.path.exists(SCC_LONG_FILE):
        return {}
    rows = _read_long(SCC_LONG_FILE)
    # repo → (best_sha, complexity_int)
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


def _load_cyclo_totals() -> dict[str, int]:
    """Map repo → cyclomatic_total from the parallel cyclo-halstead pass.

    Reads the long-format `data/sources/git/lizard.csv` (writers share the file).
    For repos with multiple snapshots takes the lexicographically-largest
    sha — used only for the display comparison table, so exact tie-breaking
    doesn't matter.
    """
    if not os.path.exists(LIZARD_LONG_FILE):
        return {}
    rows = _read_long(LIZARD_LONG_FILE)
    out: dict[str, int] = {}
    for (repo, _sha, metric), row in rows.items():
        if metric != "cyclomatic_total":
            continue
        try:
            v = int(row.get("value") or 0)
        except ValueError:
            continue
        if v > out.get(repo, 0):
            out[repo] = v
    return out


# ───────────────────────── cognitive complexity core ────────────────────────

# Sonar Cognitive Complexity rules (paper §3):
#   B1 (Increment):  +1 for each break in linear flow.
#   B2 (Nesting):    +1 per nesting level for nested control structures.
#   B3 (Hybrid):     methods/lambdas/else/finally don't add nesting bonus
#                    (else still gets +1 only).

_INC_NESTING_C = {'if', 'for', 'foreach', 'while', 'do', 'catch', 'switch', 'except'}
_LOGICAL_C = {'&&', '||', 'and', 'or'}


class SonarCognitiveExt:
    """Lizard extension that computes Sonar Cognitive Complexity per function.

    Used for **all non-Python languages**. Python files are routed to the
    purpose-built `cognitive_complexity` AST library where exact-match Sonar
    semantics matter for indent-sensitive code.

    State per-function (set on first token via `not hasattr` guard):
        cognitive_complexity   running tally (Sonar number)
        _brace_depth           current `{` depth inside the function
        _brace_seen            True once we've crossed the function-body brace
                                (so depth=0 inside it)
        _prev_logical          last logical operator seen ('&&' / '||' / '')
                                — used to detect operator chain toggles
        _pending_else_if       True after we saw `else`, expecting an `if`
                                that should NOT count again
    """

    FUNCTION_INFO = {
        "cognitive_complexity": {"caption": " CCog "},
    }

    @staticmethod
    def set_args(parser):
        pass

    def __call__(self, tokens, reader):
        for token in tokens:
            cf = reader.context.current_function
            if not hasattr(cf, "cognitive_complexity"):
                cf.cognitive_complexity = 0
                cf._brace_depth = 0
                cf._brace_seen = False
                cf._prev_logical = ''
                cf._pending_else_if = False

            # depth inside the function body (function-body { is level 0).
            depth = max(0, cf._brace_depth - 1) if cf._brace_seen else 0

            # ── Logical operator chains ──
            # Sonar B1: +1 the first time, then +1 each time the operator
            # toggles within the same boolean expression.
            if token in _LOGICAL_C:
                if cf._prev_logical and cf._prev_logical != token:
                    cf.cognitive_complexity += 1
                if not cf._prev_logical:
                    cf.cognitive_complexity += 1
                cf._prev_logical = token
            elif token in (';', '{', '}', ')', ',', ':'):
                cf._prev_logical = ''

            # ── Control structures ──
            if token == 'else':
                cf.cognitive_complexity += 1
                cf._pending_else_if = True
            elif token == 'finally':
                cf.cognitive_complexity += 1
            elif token == 'elif':
                # python-style 'elif' = +1 (no nesting bonus, like else-if)
                cf.cognitive_complexity += 1
            elif token == 'if':
                if cf._pending_else_if:
                    cf._pending_else_if = False  # 'else if' chain
                else:
                    cf.cognitive_complexity += 1 + depth
            elif token in _INC_NESTING_C:
                cf.cognitive_complexity += 1 + depth
            elif token == '?':
                cf.cognitive_complexity += 1 + depth
            elif token == 'goto':
                cf.cognitive_complexity += 1

            # ── Brace depth tracking (C-family only) ──
            if token == '{':
                cf._brace_depth += 1
                cf._brace_seen = True
            elif token == '}':
                if cf._brace_depth > 0:
                    cf._brace_depth -= 1

            yield token


def _python_cognitive(path: str) -> tuple[int, int, int]:
    """Run the cognitive_complexity package over one .py file.

    Returns (n_funcs, total, max). Skips files that fail to parse.
    """
    try:
        from cognitive_complexity.api import get_cognitive_complexity
    except ImportError:
        # Library missing — skip Python entirely (would mis-attribute to Lizard).
        return 0, 0, 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return 0, 0, 0

    n = total = mx = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                cog = get_cognitive_complexity(node)
            except Exception:
                continue
            n += 1
            total += cog
            if cog > mx:
                mx = cog
    return n, total, mx


def _list_source_files(root: str) -> tuple[list[str], list[str]]:
    """Walk `root`. Returns (python_files, other_source_files).

    Files larger than MAX_FILE_BYTES are skipped — minified / generated /
    vendored blobs that would OOM-kill lizard and aren't real code.
    """
    py: list[str] = []
    other: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in PY_SUFFIXES and ext not in SOURCE_SUFFIXES:
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if ext in PY_SUFFIXES:
                py.append(path)
            else:
                other.append(path)
    return py, other


def _run_lizard_cognitive(files: list[str]) -> tuple[int, int, int]:
    """Run our SonarCognitiveExt over `files`. Returns (n_funcs, total, max).

    Files Lizard cannot parse are silently skipped — same behaviour as the
    cyclo-halstead pass.
    """
    if not files:
        return 0, 0, 0
    import lizard
    ext = SonarCognitiveExt()
    exts = lizard.get_extensions([ext])
    total = mx = nfn = 0
    for r in lizard.analyze_files(files, exts=exts):
        for fn in r.function_list:
            cog = getattr(fn, 'cognitive_complexity', 0) or 0
            total += cog
            if cog > mx:
                mx = cog
            nfn += 1
    return nfn, total, mx


# ────────────────────────────── analysis dataclass ──────────────────────────

@dataclass
class RepoCognitive:
    repo: str
    repo_id: str = ""
    analyzed_sha: str = ""
    analyzed_year: str = "current"
    files: int = 0
    cognitive_total: int = 0
    cognitive_avg: float = 0.0
    cognitive_max: int = 0
    elapsed_s: float = 0.0
    download_s: float = 0.0
    analysis_s: float = 0.0
    error: str = ""


def analyze_directory(root: str) -> dict:
    """Compute cognitive complexity for every supported source file under `root`.

    Pure CPU work — safe to call inside a ProcessPoolExecutor worker.
    Returns a flat dict that gets attached to a `RepoCognitive`.
    """
    py_files, other_files = _list_source_files(root)
    if not py_files and not other_files:
        return {"files": 0}

    py_n, py_total, py_max = _python_cognitive_batch(py_files)
    cl_n, cl_total, cl_max = _run_lizard_cognitive(other_files)

    n = py_n + cl_n
    total = py_total + cl_total
    mx = max(py_max, cl_max)
    avg = (total / n) if n else 0.0
    return {
        "files": len(py_files) + len(other_files),
        "cognitive_total": total,
        "cognitive_avg": round(avg, 2),
        "cognitive_max": mx,
    }


def _python_cognitive_batch(files: list[str]) -> tuple[int, int, int]:
    """Aggregate `_python_cognitive` over many files."""
    n = total = mx = 0
    for path in files:
        fn, t, m = _python_cognitive(path)
        n += fn
        total += t
        if m > mx:
            mx = m
    return n, total, mx


# ─────────────────────────── repo-level processing ──────────────────────────

async def _fetch_and_analyze(
    repo: str, repo_id: str, sha: str, analyzed_year: str, base_dir: str,
    sem: asyncio.Semaphore, pool: ProcessPoolExecutor, client: httpx.Client,
    branch: str | None = None, cutoff: str | None = None, size_kb: int = 0,
) -> RepoCognitive:
    """Sparse-checkout `repo`@`sha`, run analysis in worker process, clean up.

    The pinned ``sha`` is corrected to the default branch's first-parent
    mainline via ``corrected_clone_sha`` before checkout (an off-mainline
    CI/template commit would measure the template, not the repo); metrics are
    still keyed under the original ``sha``. Same shape as the cyclo-halstead
    pass so running both back-to-back is a safe O(2*download) operation.
    """
    rc = RepoCognitive(
        repo=repo, repo_id=repo_id,
        analyzed_sha=sha, analyzed_year=analyzed_year,
    )
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    loop = asyncio.get_running_loop()

    async with sem:
        t0 = time.monotonic()
        try:
            # Correct an off-mainline pinned SHA (fork-network / CI-template
            # leak) to the real mainline commit. Records key on original `sha`.
            clone_sha = sha
            if sha and branch:
                clone_sha = await loop.run_in_executor(
                    None, corrected_clone_sha, repo, branch, sha, cutoff,
                ) or sha
            try:
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _download_tarball, repo, dest, client, clone_sha or "HEAD",
                )
                rc.download_s = dl_elapsed
            except Exception as e:
                log.debug("tarball failed for %s: %s — sparse-cloning", repo, e)
                if os.path.isdir(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _sparse_clone, repo, dest, size_kb, clone_sha or None,
                )
                rc.download_s = dl_elapsed

            if not os.path.isdir(dest):
                rc.error = "download failed"
                return rc

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
    branches: dict[str, str] | None = None,
    cutoffs: dict[str, str] | None = None,
    sizes: dict[str, int] | None = None,
) -> list[RepoCognitive]:
    """Process every repo concurrently. `shas` maps repo → (year, sha).

    ``branches``/``cutoffs`` drive the off-mainline SHA correction (see
    ``corrected_clone_sha``); ``sizes`` (repo KB) sizes the sparse-clone
    fallback. If `output_path` is given, persist a partial CSV every
    `flush_every` repos so a long run can survive a crash mid-way. The final
    write at the end of `main()` is still authoritative. If ``max_disk_gb`` >
    0, abort gracefully when free /tmp dips below the threshold.
    """
    branches = branches or {}
    cutoffs = cutoffs or {}
    sizes = sizes or {}
    sem = asyncio.Semaphore(concurrency)
    base_dir = make_clone_tmpdir("cognitive")
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

    results: list[RepoCognitive] = []
    client = httpx.Client(http2=True)
    pool = ProcessPoolExecutor(max_workers=concurrency + 2)
    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(repos))

            async def _one(repo: str) -> RepoCognitive:
                year, sha = shas.get(repo, ("current", ""))
                rc = await _fetch_and_analyze(
                    repo, repo_ids.get(repo, ""), sha, year,
                    base_dir, sem, pool, client,
                    branch=branches.get(repo), cutoff=cutoffs.get(repo),
                    size_kb=sizes.get(repo, 0),
                )
                progress.update(task, advance=1, description=repo[:name_width].ljust(name_width))
                return rc

            tasks = [_one(r) for r in repos]
            aborted = False
            for coro in asyncio.as_completed(tasks):
                rc = await coro
                results.append(rc)
                # Incremental flush — keeps progress on disk so a crash mid-run
                # doesn't lose hours of work. Cheap (CSV upsert) compared to a
                # repo download/analyze cycle.
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
    """Skip repos whose (repo, target_sha) already has a fresh cognitive_total row.

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
        # Canary metric — if cognitive_total is present at this sha and fresh,
        # we already have everything this fetcher would compute.
        row = rows.get((r, sha, "cognitive_total"))
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


def _write_results(path: str, results: list[RepoCognitive]) -> None:
    """Upsert each successful result into `data/sources/git/lizard.csv`.

    Drops results without an `analyzed_sha` (HEAD-resolved snapshots can't be
    pinned in the long format), and results that analyzed **zero files** —
    a `files == 0` outcome (empty/partial checkout, or a crashed analysis
    worker) means "not measured", and `RepoCognitive`'s zero-valued
    dataclass defaults would otherwise be persisted as a real
    `cognitive_total=0`, indistinguishable from a genuine measurement.
    `elapsed_s` is logged separately, never persisted.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    skipped_empty = 0
    for rc in results:
        if rc.error or not rc.analyzed_sha:
            continue
        if rc.files == 0:
            skipped_empty += 1
            log.warning("%s@%s: 0 files analyzed — skipping (not a real 0)",
                        rc.repo, rc.analyzed_sha[:10])
            continue
        metrics = {m: getattr(rc, m) for m in COGNITIVE_METRICS}
        upsert_snapshot(
            path,
            repo=rc.repo,
            repo_id=rc.repo_id,
            commit_sha=rc.analyzed_sha,
            metrics=metrics,
            checked_at=now,
        )
        written += 1
    log.debug("wrote %d snapshots to %s (%d skipped: 0 files analyzed)",
              written, path, skipped_empty)


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


def _fmt_num(n: float | int) -> str:
    if n >= 1e9:
        return f"{n/1e9:.1f}G"
    if n >= 1e6:
        return f"{n/1e6:.1f}M"
    if n >= 1e3:
        return f"{n/1e3:.1f}k"
    return f"{n:.0f}"


def _print_comparison(
    results: list[RepoCognitive],
    scc_map: dict[str, int],
    cyclo_map: dict[str, int],
) -> None:
    """Inline rich table — repo × (scc, cyclomatic_total, cognitive metrics, time)."""
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
    tbl.add_column("CCog", justify="right")
    tbl.add_column("CCog avg", justify="right")
    tbl.add_column("CCog max", justify="right")
    tbl.add_column("t (s)", justify="right")

    for rc in sorted(ok, key=lambda r: -r.elapsed_s):
        scc = scc_map.get(rc.repo, 0)
        cyclo = cyclo_map.get(rc.repo, 0)
        tbl.add_row(
            rc.repo,
            rc.analyzed_year,
            f"{rc.files:,}",
            _fmt_num(scc) if scc else "-",
            _fmt_num(cyclo) if cyclo else "-",
            _fmt_num(rc.cognitive_total),
            f"{rc.cognitive_avg:.1f}",
            f"{rc.cognitive_max:,}",
            f"{rc.elapsed_s:.1f}",
        )

    console.print(tbl)


# ──────────────────────────────── main ────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help=f"Parallel workers (default: {DEFAULT_CONCURRENCY}). "
             "Override with $COGNITIVE_WORKERS.",
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
    parser.add_argument("--force", action="store_true", help="Ignore TTL — re-analyze.")
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

    started = datetime.datetime.now()
    console.print()
    console.print(
        f"[bold]Cognitive complexity[/bold] (Sonar 2018; Lizard + cognitive_complexity)  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · {started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)
    console.print()

    risk_repos = load_top_repos()
    repo_ids: dict[str, str] = {e.repo: e.repo_id for e in risk_repos}
    repos_all = [e.repo for e in risk_repos]

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

    sha_map = _load_target_year_shas()
    targets: dict[str, tuple[str, str]] = {
        r: sha_map.get(r, ("current", "")) for r in repos
    }

    # Off-mainline SHA correction inputs (default branch + year-end cutoff).
    branches = load_default_branches()
    cutoffs: dict[str, str] = {
        r: f"{int(year) + 1}-01-01T00:00:00"
        for r, (year, _sha) in targets.items() if year.isdigit()
    }
    sizes: dict[str, int] = {e.repo: e.size_kb for e in risk_repos if e.size_kb}

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
        branches=branches, cutoffs=cutoffs, sizes=sizes,
    ))
    elapsed = time.monotonic() - t_start

    ok = [r for r in results if not r.error]
    errors = [r for r in results if r.error]

    if ok:
        _write_results(args.output, ok)

    scc_map = _load_scc_complexity()
    cyclo_map = _load_cyclo_totals()
    _print_comparison(results, scc_map, cyclo_map)
    console.print()

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
    if statistics:
        cog_totals = [r.cognitive_total for r in ok]
        if cog_totals:
            summary.add_row(
                "CCog totals",
                f"min {min(cog_totals):,} · p50 {int(_percentile(cog_totals, 0.5)):,} · "
                f"p90 {int(_percentile(cog_totals, 0.9)):,} · max {max(cog_totals):,}",
            )
    summary.add_row("Output CSV", args.output)
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
