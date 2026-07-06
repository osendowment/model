"""Unified sha-pinned code-metrics fetcher: ONE clone, scc **and** lizard.

Background — the three-clone problem
------------------------------------
Historically three separate fetchers each independently sparse-cloned the
**same** end-of-year snapshot of every risk-scope repo:

    src/sources/git/fetch_scc.py                 → scc (loc / complexity)
    src/sources/github/fetch_advanced_complexity → lizard cyclomatic (McCabe)
    src/sources/github/fetch_cognitive           → lizard cognitive (Sonar)

Three clones of the same commit is three-times the download and disk churn.
This module resolves the target SHA once, sparse-clones the repo **once**, and
runs scc + both lizard passes on that single checkout — writing the same two
long-format files the builders already read:

    data/sources/git/scc.csv     (repo, repo_id, git_url, commit_sha, metric, value, checked_at)
    data/sources/git/lizard.csv  (same schema)

The metric computation is unchanged from the originals (scc's `_analyze_dir`,
lizard's `_run_lizard` McCabe and the `SonarCognitiveExt` cognitive pass) — the
goal is one clone, not different numbers. scc's smart-upsert skip and lizard's
sha-pinned TTL are both preserved: a repo is cloned only when scc **or** lizard
needs work, and each tool re-runs only the part that is stale/missing.

Target SHA & off-mainline correction
-------------------------------------
Target SHA is resolved via `fetch_scc._target_sha_per_repo` — the existing
"most-recent year ≤2025 with commits>0 → last_sha, cascade back (HEAD
fallback)" logic. Metrics are recorded under that pinned SHA (the key
`build_complexity` / `build_security` join on). The *checkout* is corrected to
the default branch's first-parent mainline via
`src.sources.git.clone.corrected_clone_sha` — an off-mainline CI/template
commit would otherwise measure the template, not the repo.

Usage:
    uv run python -m src.sources.git.fetch_sha_metrics --limit 5
    uv run python -m src.sources.git.fetch_sha_metrics                  # full risk-scope set
    uv run python -m src.sources.git.fetch_sha_metrics --repos a/b c/d
    uv run python -m src.sources.git.fetch_sha_metrics --force          # ignore skip / TTL
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import datetime
import functools
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

# lizard 1.22 transitively imports requests → urllib3/chardet warning; silence.
warnings.filterwarnings("ignore", message=".*urllib3.*chardet.*")
warnings.filterwarnings("ignore", message=".*RequestsDependencyWarning.*")

from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn,
)
from rich.table import Table

from src.common.repos import load_default_branches, load_top_repos
from src.sources.git.clone import SOURCE_EXTS, corrected_clone_sha, sparse_clone
from src.sources.git.commits_years import load_sha_data
from src.sources.git.disk import (
    check_disk_or_exit, make_clone_tmpdir, print_disk_banner,
    sweep_stale_clone_dirs,
)
from src.sources.git.long_format import read as read_long
from src.sources.git.long_format import upsert_snapshot
from src.sources.github.display import _ETAColumn

# scc metric computation + skip logic live in fetch_scc; reuse them verbatim so
# the scc numbers are the SAME single implementation (one clone, one code path).
from src.sources.git.fetch_scc import (
    SCC_METRICS, _already_done, _analyze_dir, _load_repo_id_map,
    _target_sha_per_repo,
)

log = logging.getLogger(__name__)
console = Console()

REPOS_FILE = "data/value/value.csv"
SHA_FILE = "data/sources/git/commits-years.csv"
SCC_OUTPUT = "data/sources/git/scc.csv"
LIZARD_OUTPUT = "data/sources/git/lizard.csv"

DEFAULT_YEARS = [2021, 2022, 2023, 2024, 2025]
DEFAULT_LIMIT = 0          # pipeline step: full risk scope, no random sampling
DEFAULT_CONCURRENCY = int(os.environ.get("SHA_METRICS_WORKERS") or 4)
# lizard rows are sha-pinned (same sha → same complexity forever) and a changed
# sha always re-analyzes regardless of TTL — so the TTL only gates pointless
# same-sha re-analysis. 365 matches the repo-wide freshness convention.
DEFAULT_TTL_DAYS = 365

# lizard metrics emitted per (repo, sha) snapshot → data/sources/git/lizard.csv.
LIZARD_METRICS: tuple[str, ...] = (
    "files",
    "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
    "cognitive_total", "cognitive_avg", "cognitive_max",
)
# Canary metrics: a repo is "lizard-fresh" only when BOTH are present + fresh at
# the target sha, so a partial old snapshot (only one family) still re-runs.
LIZARD_CANARIES: tuple[str, ...] = ("cyclomatic_total", "cognitive_total")

# A single source file larger than this is skipped — minified bundles /
# generated parsers / vendored amalgamations that OOM-kill lizard and aren't
# hand-written code. Same value the two originals used.
MAX_FILE_BYTES = 2_000_000

# Wall-clock cap for one repo's isolated lizard subprocess, scaled up per ~1 GB
# of repo size by `_analysis_timeout` so mega-repos (linux, gcc) aren't
# false-killed.
ANALYSIS_TIMEOUT = 900

# lizard 1.22 registers Fortran but its fixed-form reader OOM-kills on large
# sources (scipy's 359 KB ODRPACK `d_odr.f` SIGKILLed the whole analysis).
# Fortran is a rounding error in our corpus and its CC is unreliable, so drop it
# from the lizard input entirely. `files` still counts these as source files;
# they simply contribute no functions, like any other language lizard skips.
LIZARD_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {".f", ".f03", ".f08", ".f77", ".f90", ".f95", ".for", ".ftn"}
)

# Source file extensions worth analyzing — same set the sparse-checkout uses.
SOURCE_SUFFIXES: set[str] = {ext.lstrip("*").lower() for ext in SOURCE_EXTS}
PY_SUFFIXES: set[str] = {".py", ".pyi", ".pyw"}


# ─────────────────────── cognitive complexity core ─────────────────────────
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


def _run_lizard_cognitive(files: list[str]) -> tuple[int, int, int]:
    """Run our SonarCognitiveExt over `files`. Returns (n_funcs, total, max).

    Files Lizard cannot parse are silently skipped — same behaviour as the
    cyclomatic pass.
    """
    if not files:
        return 0, 0, 0
    import lizard
    # Same Fortran exclusion as the cyclomatic pass: lizard's fixed-form
    # reader OOM-kills on large sources (scipy's d_odr.f took the whole
    # analysis subprocess down with exit -9 — this filter existed only in
    # _run_lizard, so the cognitive pass re-introduced the crash).
    analyzable = [
        f for f in files
        if os.path.splitext(f)[1].lower() not in LIZARD_SKIP_SUFFIXES
    ]
    if not analyzable:
        return 0, 0, 0
    ext = SonarCognitiveExt()
    exts = lizard.get_extensions([ext])
    total = mx = nfn = 0
    for r in lizard.analyze_files(analyzable, exts=exts):
        for fn in r.function_list:
            cog = getattr(fn, 'cognitive_complexity', 0) or 0
            total += cog
            if cog > mx:
                mx = cog
            nfn += 1
    return nfn, total, mx


# ─────────────────────────── cyclomatic core ───────────────────────────────

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


# ───────────────────────── combined lizard analysis ────────────────────────

def _list_source_files(root: str) -> list[str]:
    """Walk `root` and return source files matching SOURCE_SUFFIXES.

    Files larger than MAX_FILE_BYTES are skipped — minified / generated /
    vendored blobs that would OOM-kill lizard and aren't real code.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
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


def analyze_directory(root: str) -> dict:
    """Run BOTH lizard passes over the source files under `root`.

    Cyclomatic (McCabe, `_run_lizard`) runs over every source file; cognitive
    routes Python files to the `cognitive_complexity` AST lib and everything
    else to `SonarCognitiveExt`. This is the exact composition the two former
    fetchers computed independently — collapsed into one pass over one file
    list, so the numbers are identical while the checkout is scanned once.

    Returns a flat dict of metrics ready to attach to a `RepoResult`. Pure CPU
    work — invoked in an isolated subprocess via `--analyze-dir` so a crash/OOM
    on one repo can't poison the run.
    """
    files = _list_source_files(root)
    if not files:
        return {"files": 0}

    # Cyclomatic — per-function McCabe over all source files (Fortran excluded
    # inside `_run_lizard`; files it doesn't understand contribute no functions).
    _cyc_n, ccn_total, ccn_avg, ccn_max = _run_lizard(files)

    # Cognitive — Python via the AST lib, everything else via the lizard hook.
    py = [f for f in files if os.path.splitext(f)[1].lower() in PY_SUFFIXES]
    other = [f for f in files if os.path.splitext(f)[1].lower() not in PY_SUFFIXES]
    py_n, py_total, py_max = _python_cognitive_batch(py)
    cl_n, cl_total, cl_max = _run_lizard_cognitive(other)
    cog_n = py_n + cl_n
    cog_total = py_total + cl_total
    cog_max = max(py_max, cl_max)
    cog_avg = (cog_total / cog_n) if cog_n else 0.0

    return {
        "files": len(files),
        "cyclomatic_total": ccn_total,
        "cyclomatic_avg": round(ccn_avg, 2),
        "cyclomatic_max": ccn_max,
        "cognitive_total": cog_total,
        "cognitive_avg": round(cog_avg, 2),
        "cognitive_max": cog_max,
    }


def _gitlab_targets_by_id(
    sha_file: str, repo_to_id: dict[str, str], years: list[int],
) -> dict[str, tuple[str, int]]:
    """Resolve target (sha, year) for non-GitHub repos by **repo_id**, not slug.

    `load_sha_data` keys commits-years on (repo, year), which is ambiguous for a
    GitLab repo whose slug also has a stale GitHub-mirror row in the same file
    (e.g. gnome/glib). Non-GitHub repos are therefore resolved here by their
    stable `gl/…` id: read commits-years, keep the newest year in `years` with a
    populated last_sha and commits > 0. Returns {slug: (sha, year)}.
    """
    by_id: dict[str, dict[int, str]] = {}
    if os.path.exists(sha_file):
        with open(sha_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rid = (row.get("repo_id") or "").strip()
                last = (row.get("last_sha") or "").strip()
                if not rid or not last:
                    continue
                try:
                    y = int((row.get("year") or "").strip())
                    c = int((row.get("commits") or "0").strip())
                except ValueError:
                    continue
                if c > 0:
                    by_id.setdefault(rid, {})[y] = last
    out: dict[str, tuple[str, int]] = {}
    for repo, rid in repo_to_id.items():
        year_map = by_id.get(rid, {})
        for y in sorted(years, reverse=True):
            if year_map.get(y):
                out[repo] = (year_map[y], y)
                break
    return out


def _analysis_timeout(size_kb: int) -> int:
    """Per-repo lizard timeout, scaled by repo size (+base per ~1 GB).

    Small repos keep the 900 s base; linux (~6 GB) gets ~6300 s. The cap only
    needs to be generous enough not to falsely kill a slow-but-progressing
    analysis — a genuinely hung repo still dies.
    """
    return ANALYSIS_TIMEOUT + (max(0, size_kb) // 1_000_000) * ANALYSIS_TIMEOUT


def _analyze_dir_isolated(dest: str, timeout: int = ANALYSIS_TIMEOUT) -> dict:
    """Run `analyze_directory` for one repo in an isolated subprocess.

    Each repo's lizard analysis gets its own process group. A crash, an
    OOM-kill or a hang is contained to that one process — it cannot poison a
    shared pool or take sibling repos down with it. On timeout the whole
    process group is SIGKILLed. Raises on timeout / non-zero exit / bad output;
    the caller records that as the single repo's `error`.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.sources.git.fetch_sha_metrics",
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


# ──────────────────────────── result dataclass ─────────────────────────────

@dataclass
class RepoResult:
    repo: str
    repo_id: str = ""
    analyzed_sha: str = ""   # pinned target sha — the key both csvs record under
    analyzed_year: str = ""
    # scc metrics
    scc_ran: bool = False
    files_scc: int = 0
    loc: int = 0
    sloc: int = 0
    uloc: int = 0
    complexity: int = 0
    # lizard metrics
    lizard_ran: bool = False
    files: int = 0
    cyclomatic_total: int = 0
    cyclomatic_avg: float = 0.0
    cyclomatic_max: int = 0
    cognitive_total: int = 0
    cognitive_avg: float = 0.0
    cognitive_max: int = 0
    # bookkeeping
    cloned: bool = False
    elapsed_s: float = 0.0
    error: str = ""
    skipped: bool = False   # neither tool needed work → no clone

    @property
    def complexity_density(self) -> float:
        """scc complexity per 100 source lines."""
        return self.complexity / self.sloc * 100 if self.sloc else 0.0


# ───────────────────────── lizard freshness (TTL skip) ─────────────────────

def _lizard_fresh(
    path: str, targets: dict[str, str], ttl_days: int,
) -> set[str]:
    """Repos whose (repo, target_sha) has BOTH lizard canaries fresh within TTL.

    A repo is skipped for lizard only if `cyclomatic_total` AND `cognitive_total`
    are present at the target sha and both `checked_at` timestamps are within
    `ttl_days`. `ttl_days <= 0` disables the skip (always re-run). A changed
    target sha is never skipped (no rows at the new sha).
    """
    if ttl_days <= 0 or not os.path.exists(path):
        return set()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    rows = read_long(path)
    fresh: set[str] = set()
    for repo, sha in targets.items():
        if not sha:
            continue
        ok = True
        for canary in LIZARD_CANARIES:
            row = rows.get((repo, sha, canary))
            ts = row.get("checked_at", "") if row else ""
            if not ts:
                ok = False
                break
            try:
                fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                ok = False
                break
            if fetched < cutoff:
                ok = False
                break
        if ok:
            fresh.add(repo)
    return fresh


# ──────────────────────────── persistence ──────────────────────────────────

def _write_results(path: str, results: list[RepoResult]) -> None:
    """Upsert each successful lizard result into `data/sources/git/lizard.csv`.

    Drops results without an `analyzed_sha` (HEAD-resolved snapshots can't be
    pinned in the long format), results with an error, and results that
    analyzed **zero files** — a `files == 0` outcome (empty/partial checkout,
    crashed worker) means "not measured"; the zero-valued dataclass defaults
    would otherwise persist a real `cyclomatic_max=0` that deflates the
    complexity score and is then shielded by the TTL for a year.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    for rc in results:
        if rc.error or not rc.analyzed_sha:
            continue
        # `files == 0` means the lizard pass didn't run for this repo (only scc
        # was stale) OR the checkout was empty/crashed — either way there is no
        # real measurement to persist, and the zero-valued dataclass defaults
        # would otherwise land a fake `cyclomatic_max=0` in lizard.csv.
        if rc.files == 0:
            if rc.lizard_ran:
                log.warning("%s@%s: 0 files analyzed — skipping (not a real 0)",
                            rc.repo, rc.analyzed_sha[:10])
            continue
        metrics = {m: getattr(rc, m) for m in LIZARD_METRICS}
        upsert_snapshot(
            path,
            repo=rc.repo,
            repo_id=rc.repo_id,
            commit_sha=rc.analyzed_sha,
            metrics=metrics,
            checked_at=now,
        )
        written += 1
    log.debug("wrote %d lizard snapshots to %s", written, path)


def _write_scc(path: str, rc: RepoResult, checked_at: str) -> None:
    """Upsert one repo's scc snapshot (mirrors fetch_scc.run_all's writer)."""
    if rc.error or not rc.scc_ran or not rc.analyzed_sha:
        return
    upsert_snapshot(
        path,
        repo=rc.repo,
        repo_id=rc.repo_id,
        commit_sha=rc.analyzed_sha,
        metrics={
            "files": rc.files_scc,
            "loc": rc.loc,
            "sloc": rc.sloc,
            "uloc": rc.uloc,
            "complexity": rc.complexity,
            "complexity_density": round(rc.complexity_density, 2),
        },
        checked_at=checked_at,
    )


# ──────────────────────────── per-repo worker ──────────────────────────────

async def _process_one(
    repo: str, repo_id: str, sha: str, year: str,
    need_scc: bool, need_lizard: bool,
    sizes: dict[str, int], base_dir: str, sem: asyncio.Semaphore,
    branch: str | None = None, cutoff: str | None = None,
    repo_url: str | None = None,
) -> RepoResult:
    """Sparse-clone `repo`@`sha` ONCE, run the tools that need work, clean up.

    The pinned `sha` is corrected to the default branch's first-parent mainline
    (`corrected_clone_sha`) before checkout — an off-mainline CI/template commit
    would measure the template, not the repo. Metrics are still keyed under the
    original `sha`. Both scc and lizard read the one checkout.
    """
    rc = RepoResult(
        repo=repo, repo_id=repo_id, analyzed_sha=sha, analyzed_year=year,
    )
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    loop = asyncio.get_event_loop()
    async with sem:
        t0 = time.monotonic()
        try:
            # Correct an off-mainline pinned SHA to the real mainline commit.
            # Records still key on the original `sha`. This correction is a
            # GitHub-only concern (GitHub's Commits API + fork-network object
            # sharing can pin an off-mainline template commit); it is skipped for
            # non-GitHub repos, which sparse-clone straight at the pinned
            # commits-years SHA via their own host's `repo_url`.
            is_github = (not repo_url) or ("github.com" in repo_url.lower())
            clone_sha = sha
            if sha and branch and is_github:
                clone_sha = await loop.run_in_executor(
                    None,
                    functools.partial(
                        corrected_clone_sha, repo, branch, sha, cutoff,
                        what="sha-metrics",
                    ),
                ) or sha

            await loop.run_in_executor(
                None,
                functools.partial(
                    sparse_clone, repo, dest, sizes.get(repo, 0), clone_sha,
                    repo_url=repo_url,
                ),
            )
            rc.cloned = True
            if not os.path.isdir(dest):
                rc.error = "clone failed"
                return rc

            # scc — fast subprocess over the whole checkout.
            if need_scc:
                files, lines, code, uloc, complexity = await loop.run_in_executor(
                    None, _analyze_dir, dest,
                )
                rc.files_scc = files
                rc.loc = lines
                rc.sloc = code
                rc.uloc = uloc
                rc.complexity = complexity
                rc.scc_ran = True

            # lizard — CPU-bound, isolated subprocess (per-repo process group)
            # with a size-scaled timeout so mega-repos aren't false-killed.
            if need_lizard:
                metrics = await loop.run_in_executor(
                    None, _analyze_dir_isolated, dest,
                    _analysis_timeout(sizes.get(repo, 0)),
                )
                for k, v in metrics.items():
                    if hasattr(rc, k):
                        setattr(rc, k, v)
                rc.lizard_ran = True

        except subprocess.TimeoutExpired:
            rc.error = "timeout"
        except Exception as e:
            rc.error = str(e)[:80]
        finally:
            rc.elapsed_s = round(time.monotonic() - t0, 2)
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
    return rc


# ──────────────────────────── orchestration ────────────────────────────────

async def run_all(
    targets: dict[str, tuple[str, str]],
    need_scc: set[str],
    need_lizard: set[str],
    sizes: dict[str, int],
    repo_ids: dict[str, str],
    default_branches: dict[str, str],
    cutoffs: dict[str, str],
    scc_output: str,
    lizard_output: str,
    concurrency: int,
    max_disk_gb: float = 0.0,
    git_urls: dict[str, str] | None = None,
) -> list[RepoResult]:
    """Process all (repo, sha) targets concurrently; upsert as each finishes.

    `targets` maps repo → (sha, year). Only repos in `need_scc | need_lizard`
    are cloned. If `max_disk_gb` > 0, stop scheduling new clones once free /tmp
    dips below the threshold (in-flight work is awaited so cleanup still runs).
    """
    url_map = git_urls or {}
    sem = asyncio.Semaphore(concurrency)
    base_dir = make_clone_tmpdir("sha-metrics")
    pool = ThreadPoolExecutor(max_workers=concurrency * 2 + 4)
    loop = asyncio.get_event_loop()
    loop.set_default_executor(pool)

    results: list[RepoResult] = []
    todo = [r for r in targets if r in need_scc or r in need_lizard]
    name_width = min(max((len(r) for r in todo), default=20), 30)
    checked_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
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
    aborted = False

    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(todo))

            async def _run(repo: str) -> RepoResult:
                sha, year = targets[repo]
                rc = await _process_one(
                    repo, repo_ids.get(repo, ""), sha, year,
                    repo in need_scc, repo in need_lizard,
                    sizes, base_dir, sem,
                    default_branches.get(repo), cutoffs.get(repo),
                    url_map.get(repo, ""),
                )
                # Flush per-repo so a crash doesn't discard the batch; both
                # upserts are idempotent + lock-protected.
                _write_scc(scc_output, rc, checked_at)
                _write_results(lizard_output, [rc])
                progress.update(task, advance=1,
                                description=repo[:name_width].ljust(name_width))
                return rc

            tasks = [_run(r) for r in todo]
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


# ──────────────────────────────── reporting ────────────────────────────────

def _print_results(results: list[RepoResult], skipped: int) -> None:
    ok = [r for r in results if not r.error]
    errors = [r for r in results if r.error]

    if ok:
        ok_sorted = sorted(ok, key=lambda r: -r.elapsed_s)[:20]
        tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
        tbl.add_column("Repo")
        tbl.add_column("SHA", justify="right")
        tbl.add_column("kLOC", justify="right")
        tbl.add_column("Cmplx", justify="right")
        tbl.add_column("CCN", justify="right")
        tbl.add_column("CCog", justify="right")
        tbl.add_column("Time", justify="right")
        for r in ok_sorted:
            kloc = r.sloc / 1000
            kloc_s = f"{kloc:,.0f}" if kloc >= 1 else f"{kloc:,.1f}"
            tbl.add_row(
                r.repo,
                f"[dim]{r.analyzed_sha[:7]}[/dim]",
                kloc_s if r.scc_ran else "-",
                f"{r.complexity:,}" if r.scc_ran else "-",
                f"{r.cyclomatic_total:,}" if r.lizard_ran else "-",
                f"{r.cognitive_total:,}" if r.lizard_ran else "-",
                f"[dim]{r.elapsed_s:.1f}s[/dim]",
            )
        console.print(tbl)
        console.print()

    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=14)
    summary.add_column()
    summary.add_row("Cloned", f"[bold]{len(ok)}[/bold] repos (one clone each)")
    if skipped:
        summary.add_row("Skipped", f"[dim]{skipped} already fresh (scc + lizard)[/dim]")
    if errors:
        summary.add_row(
            "Errors",
            f"[red]{len(errors)}[/red] [dim]("
            + ", ".join(f"{r.repo}: {r.error}" for r in errors[:3])
            + ")[/dim]",
        )
    if ok:
        wall = sum(r.elapsed_s for r in ok)
        summary.add_row("Per-repo", f"avg [bold]{wall / len(ok):.1f}s[/bold]")
    console.print(summary)
    console.print()


# ──────────────────────────────── main ─────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One clone per repo: scc + lizard → scc.csv + lizard.csv.",
    )
    parser.add_argument("--input", default=REPOS_FILE,
                        help=f"value-data CSV (default: {REPOS_FILE})")
    parser.add_argument("--sha-file", default=SHA_FILE,
                        help=f"commits-years.csv (default: {SHA_FILE})")
    parser.add_argument("--scc-output", default=SCC_OUTPUT)
    parser.add_argument("--lizard-output", default=LIZARD_OUTPUT)
    parser.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                        help=f"Target years for snapshot SHA (default: {DEFAULT_YEARS}).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="Process N random risk-scope repos (0 = all).")
    parser.add_argument("--repos",
                        help="Comma-separated repo slugs — process only these.")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel clone+analyse workers (default: {DEFAULT_CONCURRENCY}). "
                             "Override with $SHA_METRICS_WORKERS.")
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip repos whose lizard rows are fresh within N days "
                             f"(default: {DEFAULT_TTL_DAYS}).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore scc smart-upsert AND lizard TTL — re-analyse everything.")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for --limit sampling (default: 42).")
    parser.add_argument("--max-disk-gb", type=float, default=2.0,
                        help="Abort gracefully if free /tmp dips below this "
                             "(default: 2.0; set 0 to disable).")
    parser.add_argument("--analyze-dir", default=None,
                        help="Internal: analyse one already-cloned directory, "
                             "print lizard metrics as JSON, exit.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Isolated-analysis subprocess entrypoint — print JSON and exit before any
    # banner / logging / network setup.
    if args.analyze_dir:
        print(json.dumps(analyze_directory(args.analyze_dir)))
        return

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    started = datetime.datetime.now()
    console.print()
    console.print(
        f"[bold]fetch_sha_metrics[/bold] (scc + lizard, one clone)  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · ttl={args.ttl_days}d · "
        f"{started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    sweep_stale_clone_dirs(console=console)
    print_disk_banner(console=console)

    entries = load_top_repos(value_file=args.input)
    repos_all = [e.repo for e in entries]
    sizes = {e.repo: e.size_kb for e in entries if e.size_kb}
    git_urls = {e.repo: e.git_url for e in entries}

    if args.repos:
        want = {r.strip().lower() for r in args.repos.split(",") if r.strip()}
        repos_all = [r for r in repos_all if r in want]
        unknown = want - set(repos_all)
        if unknown:
            console.print(
                f"[yellow]--repos: not in risk scope, skipped — "
                f"{', '.join(sorted(unknown))}[/yellow]"
            )

    rng = random.Random(args.seed)
    if args.limit and args.limit < len(repos_all):
        repos = rng.sample(repos_all, args.limit)
    else:
        repos = list(repos_all)

    sha_data = load_sha_data(args.sha_file)
    if not sha_data:
        console.print(
            f"[red]no commits-years data found at {args.sha_file}[/red]\n"
            f"[dim]run `uv run python -m src.sources.git.commits_years` first.[/dim]"
        )
        return

    # Target SHA (+ its year) per repo. GitHub repos use the slug-keyed scc
    # cascade (unchanged — byte-identical). Non-GitHub repos (gl/…) resolve by
    # repo_id instead, so a stale GitHub-mirror row that shares a GitLab repo's
    # slug can't hand it the wrong SHA (see `_gitlab_targets_by_id`).
    rid_by_repo = {e.repo: str(e.repo_id) for e in entries}
    gl_repos = [r for r in repos if str(rid_by_repo.get(r, "")).startswith("gl/")]
    gh_repos = [r for r in repos if r not in set(gl_repos)]
    raw_targets = _target_sha_per_repo(sha_data, gh_repos, args.years)  # {repo:(sha,year)}
    raw_targets.update(
        _gitlab_targets_by_id(args.sha_file, {r: rid_by_repo[r] for r in gl_repos}, args.years)
    )
    targets = {r: (sha, str(y)) for r, (sha, y) in raw_targets.items()}
    cutoffs = {r: f"{y + 1}-01-01T00:00:00" for r, (_sha, y) in raw_targets.items()}
    missing = [r for r in repos if r not in targets]
    if missing:
        console.print(
            f"[dim]no SHA found for {len(missing)} repos in years "
            f"{args.years} — skipped.[/dim]"
        )

    # Skip logic — scc smart-upsert AND lizard TTL, per tool, at the target sha.
    scc_targets = {r: sha for r, (sha, _y) in targets.items()}
    if args.force:
        scc_done: set[str] = set()
        lizard_ok: set[str] = set()
    else:
        scc_done = _already_done(args.scc_output, scc_targets)
        lizard_ok = _lizard_fresh(args.lizard_output, scc_targets, args.ttl_days)

    need_scc = {r for r in targets if r not in scc_done}
    need_lizard = {r for r in targets if r not in lizard_ok}
    to_clone = need_scc | need_lizard
    skipped = len(targets) - len(to_clone)

    console.print(
        f"[dim]{len(to_clone)} to clone · {skipped} fresh (scc+lizard) · "
        f"{len(missing)} no-sha · scc-needed={len(need_scc)} "
        f"lizard-needed={len(need_lizard)}[/dim]"
    )
    console.print()

    if not to_clone:
        console.print("[green]nothing to do — every target is fresh in scc.csv + lizard.csv[/green]")
        return

    repo_ids = _load_repo_id_map()
    # github/repos.csv (the id map's source) has no GitLab rows, so a gl/ repo
    # would land in scc.csv / lizard.csv with a blank repo_id — invisible to the
    # id-keyed builder joins. Stamp the gl/ ids from the risk-scope entries.
    for e in entries:
        if str(e.repo_id).startswith("gl/"):
            repo_ids[e.repo] = str(e.repo_id)
    default_branches = load_default_branches()

    t0 = time.monotonic()
    results = asyncio.run(run_all(
        targets, need_scc, need_lizard, sizes, repo_ids, default_branches,
        cutoffs, args.scc_output, args.lizard_output, args.concurrency,
        max_disk_gb=args.max_disk_gb, git_urls=git_urls,
    ))
    elapsed = time.monotonic() - t0

    _print_results(results, skipped)
    console.print(
        f"[dim]wallclock {elapsed:.1f}s · scc → {args.scc_output} · "
        f"lizard → {args.lizard_output}[/dim]"
    )


if __name__ == "__main__":
    main()
