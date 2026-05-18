"""Run **semgrep** SAST per risk-scope repo and emit per-repo finding counts.

Why this exists:
    OpenSSF Scorecard gives a project-management/security-posture signal but
    doesn't actually scan the code. Semgrep (Rust-based, multi-language) runs
    static-analysis rule packs against the working tree and reports concrete
    findings — useful for the risk pipeline as a hands-on "code quality /
    security smell" indicator independent of CVE history.

Library choice: semgrep CLI via subprocess + JSON output.
    The Python `semgrep` package on PyPI installs the same CLI binary; we call
    it as a subprocess so each repo runs in a clean process (rule cache lives
    in `~/.semgrep/`, shared across runs). `pysemgrep` has no stable public
    API, and the CLI's --json schema is documented and stable.

Rule pack:
    Default `p/default` — the lightweight defaults, ~5–15 s per medium repo.
    `p/security-audit` is heavier (~30 s/repo); the user can pick via
    --rulepack to compare. We don't run the heavy pack on the full set in
    this loop; the test plan compares 3 repos default-vs-audit so the
    delta is documented before committing to a full rerun.

Period semantics:
    For each repo, look up `data/github/git/commits-years.csv` to find the
    most recent year ≤ 2025 with commits>0; use that year's `last_sha`.
    Same logic as `src.pipeline.risk.build_complexity._load_target_year` and
    `src.github.fetch_advanced_complexity._load_target_year_shas`. If no
    per-year SHA is recorded, fall back to HEAD (`analyzed_year="current"`).

Reuse: clones via `_download_tarball` / `_sparse_clone` from
`src.git.clone`. Sparse-checkout is fine — semgrep only needs the source
files, not git history.

Output: data/git/semgrep.csv (long format, shared schema)

    repo, repo_id, commit_sha, metric, value, checked_at

Each (repo, sha) snapshot emits one row per numeric metric. Metric names
are rulepack-prefixed so multiple rule packs coexist for the same SHA:

    p/default        → p_default.findings_total, p_default.findings_security, …
    p/security-audit → p_security_audit.findings_total, …

Numeric metrics persisted: findings_total, findings_error, findings_warning,
findings_info, findings_security, findings_correctness, findings_performance,
top_rule_count. The `top_rule` string is logged but NOT persisted (long
format is numeric-only). `analyzed_year`, `elapsed_s`, and `rulepack` are
not persisted either — rulepack is encoded in the metric name prefix and
year metadata can be reconstructed from `data/github/git/commits-years.csv`.

Usage:
    uv run python -m src.github.fetch_semgrep --limit 10
    uv run python -m src.github.fetch_semgrep --limit 10 --rulepack p/security-audit
    uv run python -m src.github.fetch_semgrep --limit 0   # all eligible (don't!)
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
import tempfile
import time
import warnings
from collections import Counter
from dataclasses import dataclass

# requests (transitively imported by semgrep) emits a RequestsDependencyWarning
warnings.filterwarnings("ignore", message=".*urllib3.*chardet.*")
warnings.filterwarnings("ignore", message=".*RequestsDependencyWarning.*")

import httpx
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn,
)
from rich.table import Table

from src.git.clone import download_tarball as _download_tarball
from src.git.clone import sparse_clone as _sparse_clone
from src.git.disk import check_disk_or_exit, print_disk_banner
from src.git.long_format import read as _read_long
from src.git.long_format import upsert_snapshot as _upsert_snapshot
from src.github.display import _ETAColumn
from src.pipeline.common.repos import load_risk_repos


log = logging.getLogger(__name__)
console = Console()

DATA_DIR = "data"
COMMITS_YEARS_FILE = f"{DATA_DIR}/github/git/commits-years.csv"
OUTPUT_FILE = f"{DATA_DIR}/git/semgrep.csv"

DEFAULT_LIMIT = 10
DEFAULT_SEED = 42
DEFAULT_CONCURRENCY = int(os.environ.get("SEMGREP_WORKERS") or 4)
DEFAULT_TTL_DAYS = 30
DEFAULT_RULEPACK = "p/default"  # fast lightweight defaults
DEFAULT_TIMEOUT_S = 300         # 5 min per repo — semgrep can hang on huge ones
ELIGIBLE_TOTAL = 899            # for full-set wallclock estimation in the report

# Numeric metrics persisted to the long-format CSV. `top_rule` (string) is
# only logged; `elapsed_s`, `analyzed_year`, and `rulepack` are not
# persisted (rulepack is captured by the metric-name prefix below).
NUMERIC_METRICS: tuple[str, ...] = (
    "findings_total",
    "findings_error",
    "findings_warning",
    "findings_info",
    "findings_security",
    "findings_correctness",
    "findings_performance",
    "top_rule_count",
)


def rulepack_prefix(rulepack: str) -> str:
    """`p/security-audit` → `p_security_audit` (snake-case identifier).

    Mirrors `src.git.migrations.migrate_semgrep.rulepack_prefix` so historical
    migrated rows and freshly fetched rows share one metric naming convention.
    """
    return rulepack.replace("/", "_").replace("-", "_").lower()


# ────────────────────────── target-SHA resolution ──────────────────────────

def _load_target_year_shas() -> dict[str, tuple[str, str]]:
    """Map repo → (year_str, last_sha) for the most recent year ≤ 2025 with commits>0.

    Mirrors `src.pipeline.risk.build_complexity._load_target_year` /
    `src.github.fetch_advanced_complexity._load_target_year_shas`. Repos not
    present (or with no commits in 2021–2025) are absent — the caller falls
    back to HEAD with analyzed_year="current".
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


# ──────────────────────────────── analysis ────────────────────────────────

@dataclass
class RepoFindings:
    repo: str
    analyzed_sha: str = ""
    analyzed_year: str = "current"
    rulepack: str = DEFAULT_RULEPACK
    findings_total: int | None = 0  # None => timeout / unknown
    findings_error: int = 0
    findings_warning: int = 0
    findings_info: int = 0
    findings_security: int = 0
    findings_correctness: int = 0
    findings_performance: int = 0
    top_rule: str = ""
    top_rule_count: int = 0
    elapsed_s: float = 0.0
    download_s: float = 0.0
    scan_s: float = 0.0
    error: str = ""


def _parse_semgrep_json(blob: dict) -> dict:
    """Reduce a semgrep --json blob to the counts we keep.

    Severity normalised to lower-case keys (`error`, `warning`, `info`); the
    metadata category uses semgrep's own taxonomy (`security`, `correctness`,
    `performance`, …) — we keep those three plus the top-firing rule.
    """
    results = blob.get("results", []) or []
    sev = Counter()
    cat = Counter()
    rules = Counter()
    for r in results:
        extra = r.get("extra") or {}
        s = (extra.get("severity") or "").upper()
        if s == "ERROR":
            sev["error"] += 1
        elif s == "WARNING":
            sev["warning"] += 1
        elif s == "INFO":
            sev["info"] += 1
        meta = extra.get("metadata") or {}
        c = (meta.get("category") or "").lower()
        if c:
            cat[c] += 1
        cid = r.get("check_id") or ""
        if cid:
            rules[cid] += 1

    top_rule, top_count = ("", 0)
    if rules:
        top_rule, top_count = rules.most_common(1)[0]

    return {
        "findings_total": len(results),
        "findings_error": sev["error"],
        "findings_warning": sev["warning"],
        "findings_info": sev["info"],
        "findings_security": cat["security"],
        "findings_correctness": cat["correctness"],
        "findings_performance": cat["performance"],
        "top_rule": top_rule,
        "top_rule_count": top_count,
    }


def _run_semgrep(path: str, rulepack: str, timeout_s: int) -> tuple[dict, str]:
    """Run semgrep on `path`. Returns (parsed_counts, error_str).

    error_str is "" on success, "timeout" on subprocess timeout, otherwise
    a short error description (semgrep nonzero exit, JSON parse failure, …).

    semgrep exits 0 when findings is empty AND when findings exist (it's a
    scanner, not a linter that fails the build). Nonzero exits are real
    errors (bad rulepack, IO problem). --metrics=off keeps it offline-ish
    after the rule download. --no-rewrite-rule-ids keeps `check_id` as the
    full path so we can group by rule reliably.
    """
    # When this script is launched via `uv run`, the project venv is on PATH
    # so plain `semgrep` resolves to .venv/bin/semgrep — no need for a nested
    # `uv run` wrapper (which would slow startup by ~1s per scan).
    cmd = [
        "semgrep",
        "--config", rulepack,
        "--json", "--quiet",
        "--metrics=off",
        "--no-rewrite-rule-ids",
        "--timeout", "60",            # per-rule timeout (seconds)
        "--timeout-threshold", "3",   # bail on a rule after 3 file-level timeouts
        path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return {}, "timeout"
    if proc.returncode not in (0, 1):
        # 0 = clean, 1 = findings present (older versions); 2/3/… are errors
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        return {}, ("semgrep_exit=" + str(proc.returncode) + ": " + (err[-1] if err else ""))[:200]
    try:
        blob = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as e:
        return {}, f"json: {e}"[:200]
    return _parse_semgrep_json(blob), ""


# ─────────────────────────── repo-level processing ─────────────────────────

async def _fetch_and_scan(
    repo: str, sha: str, analyzed_year: str, rulepack: str,
    timeout_s: int, base_dir: str,
    sem: asyncio.Semaphore, client: httpx.Client,
) -> RepoFindings:
    """Sparse-checkout `repo`@`sha`, run semgrep, clean up."""
    rf = RepoFindings(
        repo=repo, analyzed_sha=sha, analyzed_year=analyzed_year, rulepack=rulepack,
    )
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    loop = asyncio.get_running_loop()

    async with sem:
        t0 = time.monotonic()
        try:
            # Try tarball first (fast for small repos), sparse-clone fallback.
            try:
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _download_tarball, repo, dest, client, sha or "HEAD",
                )
                rf.download_s = dl_elapsed
            except Exception as e:
                log.debug("tarball failed for %s: %s — sparse-cloning", repo, e)
                if os.path.isdir(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                dl_elapsed, _ = await loop.run_in_executor(
                    None, _sparse_clone, repo, dest, 0, sha or None,
                )
                rf.download_s = dl_elapsed

            if not os.path.isdir(dest):
                rf.error = "download failed"
                rf.findings_total = None
                return rf

            t_scan = time.monotonic()
            counts, err = await loop.run_in_executor(
                None, _run_semgrep, dest, rulepack, timeout_s,
            )
            rf.scan_s = time.monotonic() - t_scan

            if err:
                rf.error = err
                # Per spec: timeouts get findings_total="" so we know to retry.
                rf.findings_total = None
            else:
                for k, v in counts.items():
                    setattr(rf, k, v)

        except subprocess.TimeoutExpired:
            rf.error = "timeout"
            rf.findings_total = None
        except Exception as e:
            rf.error = str(e)[:120]
            rf.findings_total = None
        finally:
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            rf.elapsed_s = round(time.monotonic() - t0, 2)

    return rf


async def scan_repos(
    repos: list[str], shas: dict[str, tuple[str, str]],
    rulepack: str, concurrency: int, timeout_s: int,
    max_disk_gb: float = 0.0,
    output_path: str | None = None,
    repo_ids_global: dict[str, str] | None = None,
) -> list[RepoFindings]:
    """Process every repo concurrently. `shas` maps repo → (year, sha).

    If ``max_disk_gb`` > 0, abort gracefully when free /tmp dips below
    that threshold (waits for in-flight scans to finish).
    """
    sem = asyncio.Semaphore(concurrency)
    base_dir = tempfile.mkdtemp(prefix="semgrep-")
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

    results: list[RepoFindings] = []
    client = httpx.Client(http2=True)
    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(repos))

            async def _one(repo: str) -> RepoFindings:
                year, sha = shas.get(repo, ("current", ""))
                rf = await _fetch_and_scan(
                    repo, sha, year, rulepack, timeout_s,
                    base_dir, sem, client,
                )
                progress.update(
                    task, advance=1,
                    description=repo[:name_width].ljust(name_width),
                )
                return rf

            tasks = [_one(r) for r in repos]
            aborted = False
            for coro in asyncio.as_completed(tasks):
                rf = await coro
                results.append(rf)
                # Flush to disk per-result so a crash doesn't discard the
                # whole batch. _write_results itself is idempotent (upsert
                # by (repo, sha, metric)).
                if output_path and repo_ids_global is not None:
                    try:
                        _write_results(output_path, [rf], repo_ids_global)
                    except Exception as exc:
                        log.warning("flush failed for %s: %s", rf.repo, exc)
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
    repos: list[str], path: str, rulepack: str,
    sha_map: dict[str, tuple[str, str]], ttl_days: int,
) -> tuple[list[str], int]:
    """Skip repos already analysed for this (repo, sha, rulepack) within ttl_days.

    A repo is considered fresh iff the long-format file holds at least one
    `<rulepack_prefix>.*` metric for that repo's *target SHA* (per the
    sha_map), and that row's checked_at timestamp is within the TTL window.
    Repos with a different target SHA or no rows for this rulepack are not
    skipped — they get re-scanned at the new SHA.
    """
    if ttl_days <= 0 or not os.path.exists(path):
        return repos, 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    prefix = rulepack_prefix(rulepack) + "."
    fresh: set[str] = set()
    rows = _read_long(path)
    # Map (repo, sha) → most recent checked_at across this rulepack's metrics.
    seen: dict[tuple[str, str], datetime.datetime] = {}
    for (repo, sha, metric), row in rows.items():
        if not metric.startswith(prefix):
            continue
        ts = row.get("checked_at", "")
        if not ts:
            continue
        try:
            fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        key = (repo, sha)
        if key not in seen or fetched > seen[key]:
            seen[key] = fetched
    for repo in repos:
        _year, target_sha = sha_map.get(repo, ("current", ""))
        if not target_sha:
            continue
        last = seen.get((repo, target_sha))
        if last is not None and last >= cutoff:
            fresh.add(repo)
    filtered = [r for r in repos if r not in fresh]
    return filtered, len(repos) - len(filtered)


def _write_results(
    path: str, results: list[RepoFindings], repo_ids: dict[str, str],
) -> int:
    """Upsert each result's numeric metrics into the long-format CSV.

    For each successful scan we emit one row per metric in NUMERIC_METRICS
    under the rulepack-prefixed name (e.g. `p_default.findings_total`).
    Skip rules:
      - empty `analyzed_sha` (can't pin to a commit)
      - `findings_total is None` (timeout/error — don't poison the file)
    `top_rule` is logged but never persisted.
    Returns the number of (repo, sha) snapshots upserted.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshots = 0
    for rf in results:
        if not rf.analyzed_sha:
            log.debug("skip %s: no analyzed_sha (HEAD-only fallback)", rf.repo)
            continue
        if rf.findings_total is None:
            log.debug("skip %s: findings_total empty (%s)", rf.repo, rf.error or "no result")
            continue
        prefix = rulepack_prefix(rf.rulepack)
        metrics: dict[str, int] = {
            f"{prefix}.findings_total": rf.findings_total,
            f"{prefix}.findings_error": rf.findings_error,
            f"{prefix}.findings_warning": rf.findings_warning,
            f"{prefix}.findings_info": rf.findings_info,
            f"{prefix}.findings_security": rf.findings_security,
            f"{prefix}.findings_correctness": rf.findings_correctness,
            f"{prefix}.findings_performance": rf.findings_performance,
            f"{prefix}.top_rule_count": rf.top_rule_count,
        }
        if rf.top_rule:
            log.info(
                "%s [%s] top_rule=%s (×%d)",
                rf.repo, rf.rulepack, rf.top_rule, rf.top_rule_count,
            )
        _upsert_snapshot(
            path,
            repo=rf.repo,
            repo_id=repo_ids.get(rf.repo, ""),
            commit_sha=rf.analyzed_sha,
            metrics=metrics,
            checked_at=now,
        )
        snapshots += 1
    return snapshots


# ──────────────────────────────── reporting ────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _short_rule(rule: str, n: int = 32) -> str:
    """Trim full rule path to the trailing component for display.

    `python.lang.security.audit.subprocess-shell-true.subprocess-shell-true`
    → `subprocess-shell-true`.
    """
    if not rule:
        return ""
    short = rule.rsplit(".", 1)[-1]
    return short if len(short) <= n else short[: n - 1] + "…"


def _print_results_table(results: list[RepoFindings]) -> None:
    ok = [r for r in results if not r.error or r.findings_total is not None]
    if not ok and not results:
        return
    tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Repo")
    tbl.add_column("Yr", justify="right")
    tbl.add_column("Tot", justify="right")
    tbl.add_column("Err", justify="right")
    tbl.add_column("Warn", justify="right")
    tbl.add_column("Info", justify="right")
    tbl.add_column("Sec", justify="right")
    tbl.add_column("Top rule", overflow="fold")
    tbl.add_column("×", justify="right")
    tbl.add_column("t (s)", justify="right")

    for rf in sorted(results, key=lambda r: -(r.findings_total or 0)):
        total_s = "—" if rf.findings_total is None else f"{rf.findings_total:,}"
        tbl.add_row(
            rf.repo,
            rf.analyzed_year,
            total_s,
            f"[red]{rf.findings_error:,}[/red]" if rf.findings_error else "0",
            f"{rf.findings_warning:,}",
            f"{rf.findings_info:,}",
            f"[yellow]{rf.findings_security:,}[/yellow]" if rf.findings_security else "0",
            _short_rule(rf.top_rule) or ("[red]" + rf.error + "[/red]" if rf.error else "—"),
            f"{rf.top_rule_count:,}" if rf.top_rule_count else "—",
            f"{rf.elapsed_s:.1f}",
        )

    console.print(tbl)


# ──────────────────────────────── main ────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run semgrep SAST per risk-scope (A/B value-class) repo and emit per-repo finding counts.",
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
        help=f"Parallel scan workers (default: {DEFAULT_CONCURRENCY}; semgrep is CPU-bound). "
             "Override with $SEMGREP_WORKERS.",
    )
    parser.add_argument(
        "--max-disk-gb", type=float, default=2.0,
        help="Abort gracefully if free /tmp dips below this "
             "(default: 2.0; set 0 to disable).",
    )
    parser.add_argument(
        "--rulepack", default=DEFAULT_RULEPACK,
        help=f"Semgrep rule pack (default: {DEFAULT_RULEPACK}; e.g. p/security-audit, p/cwe-top-25).",
    )
    parser.add_argument(
        "--ttl-days", type=int, default=DEFAULT_TTL_DAYS,
        help=f"Skip repos analysed (with the SAME rulepack) within N days (default: {DEFAULT_TTL_DAYS}).",
    )
    parser.add_argument(
        "--timeout-s", type=int, default=DEFAULT_TIMEOUT_S,
        help=f"Per-repo subprocess timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--repos", nargs="+", default=None,
        help="Override the risk-scope set with explicit repo slugs (e.g. urllib3/urllib3).",
    )
    parser.add_argument("--force", action="store_true", help="Bypass --ttl-days.")
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
        f"[bold]Semgrep SAST[/bold] [{args.rulepack}]  "
        f"[dim]limit={args.limit or 'all'} · seed={args.seed} · "
        f"concurrency={args.concurrency} · timeout={args.timeout_s}s · "
        f"{started:%Y-%m-%d %H:%M:%S}[/dim]"
    )
    print_disk_banner(console=console)
    console.print()

    # Repo selection. Always load risk-scope set so we can map repo → repo_id
    # for the long-format writer (even when the user passes --repos manually).
    risk_repos = load_risk_repos()
    repo_ids: dict[str, str] = {e.repo: e.repo_id for e in risk_repos}
    if args.repos:
        repos = [r.strip().lower() for r in args.repos]
    else:
        repos_all = [e.repo for e in risk_repos]
        rng = random.Random(args.seed)
        if args.limit and args.limit < len(repos_all):
            repos = rng.sample(repos_all, args.limit)
        else:
            repos = list(repos_all)

    # Resolve target SHAs first — TTL filter needs them to key freshness on
    # (repo, sha) so a new SHA doesn't get skipped.
    sha_map = _load_target_year_shas()
    targets: dict[str, tuple[str, str]] = {
        r: sha_map.get(r, ("current", "")) for r in repos
    }

    ttl = 0 if args.force else args.ttl_days
    repos, ttl_skipped = _filter_by_ttl(
        repos, args.output, args.rulepack, targets, ttl,
    )
    if not repos:
        console.print(
            f"[dim]All sampled repos fresh (TTL {args.ttl_days}d, rulepack "
            f"{args.rulepack}) — nothing to do.[/dim]"
        )
        return

    # Trim targets to the post-TTL set so concurrency stats line up.
    targets = {r: targets[r] for r in repos}

    console.print(
        f"[dim]processing {len(repos)} repos[/dim]"
        f"{f' · skipped {ttl_skipped} fresh' if ttl_skipped else ''}"
    )
    console.print()

    t_start = time.monotonic()
    results = asyncio.run(scan_repos(
        repos, targets, args.rulepack, args.concurrency, args.timeout_s,
        max_disk_gb=args.max_disk_gb,
        output_path=args.output,
        repo_ids_global=repo_ids,
    ))
    elapsed = time.monotonic() - t_start

    if results:
        _write_results(args.output, results, repo_ids)

    # Inline results.
    _print_results_table(results)
    console.print()

    # Summary stats.
    ok = [r for r in results if not r.error]
    errors = [r for r in results if r.error]
    times = [r.elapsed_s for r in results if r.findings_total is not None]

    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim", min_width=14)
    summary.add_column()
    summary.add_row("Scanned", f"[bold]{len(ok)}[/bold] / {len(results)} repos")
    if errors:
        summary.add_row(
            "Errors",
            f"[red]{len(errors)}[/red] [dim]("
            + ", ".join(f"{r.repo}: {r.error[:40]}" for r in errors[:3])
            + ")[/dim]",
        )
    summary.add_row("Wallclock", f"[bold]{elapsed:.1f}s[/bold]")
    if times:
        summary.add_row(
            "Per-repo (s)",
            f"min {min(times):.1f} · p50 {_percentile(times, 0.5):.1f} · "
            f"p90 {_percentile(times, 0.9):.1f} · max {max(times):.1f}",
        )
        if len(ok) > 0:
            scale = ELIGIBLE_TOTAL / len(ok)
            summary.add_row(
                "Full-set est.",
                f"~{elapsed * scale / 60:.1f} min "
                f"[dim](×{scale:.1f} for {ELIGIBLE_TOTAL} repos at concurrency={args.concurrency})[/dim]",
            )
    if ok:
        total_findings = sum((r.findings_total or 0) for r in ok)
        with_findings = sum(1 for r in ok if (r.findings_total or 0) > 0)
        summary.add_row(
            "Findings",
            f"{total_findings:,} across {len(ok)} repos "
            f"[dim]({with_findings} non-zero, "
            f"avg {total_findings / len(ok):.1f}/repo)[/dim]",
        )
    summary.add_row("Output CSV", args.output)
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
