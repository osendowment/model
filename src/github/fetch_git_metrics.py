"""Analyze code complexity for GitHub repos via sparse checkout + scc.

Uses scc (https://github.com/boyter/scc) for fast multi-language static
analysis: LOC, ULOC, complexity (branch statement count), and COCOMO estimates.

Strategy:
  - Small repos (< 1MB): tarball download (fastest, avoids git protocol overhead)
  - Large repos (≥ 1MB): sparse checkout (fetches only source code files)

Usage:
    python -m src.github.fetch_git_metrics --limit 40
    python -m src.github.fetch_git_metrics --ttl 0         # force refresh
    python -m src.github.fetch_git_metrics --year 2025      # snapshot at end of 2025
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import io
import json
import logging
import os
import random
import shutil
import subprocess
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import aiohttp
import httpx
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn,
)
from rich.table import Table

from src.github.github_client import GITHUB_API, get_revolver
from src.github.display import _ETAColumn
from src.pipeline.repos import load_ab_repos

log = logging.getLogger(__name__)
console = Console()

REPOS_FILE = "data/value-data.csv"  # A/B class subset loaded via src.repos.load_ab_repos
OUTPUT_FILE = "data/github/git/complexity.csv"
YEARLY_OUTPUT_DIR = "data/github/git"
SHA_FILE = "data/github/git/commits-years.csv"
YEARLY_METRICS = ["files", "loc", "sloc", "uloc", "scc_complexity", "scc_density"]
YEARLY_METRIC_FILES = {
    "files":          "files.csv",
    "loc":            "loc.csv",
    "sloc":           "sloc.csv",
    "uloc":           "uloc.csv",
    "scc_complexity": "scc-complexity.csv",
    "scc_density":    "scc-density.csv",
}
DEFAULT_TTL_DAYS = 90
AVG_ANNUAL_SALARY = 56_286  # scc default, US average developer salary
AVG_MONTHLY_SALARY = AVG_ANNUAL_SALARY / 12

OUTPUT_FIELDS = [
    "repo", "files", "loc", "uloc", "complexity",
    "complexity_density", "cocomo_months", "cocomo_people",
    "cocomo_cost", "fetched_at",
]

# Source-code languages scc recognizes — names must match scc's output.
# Used to filter scc results to real code (skip docs, configs, data files).
SOURCE_LANGS: set[str] = {
    "C", "C Header", "C++", "C++ Header", "C#", "Objective C", "Objective C++",
    "Java", "Kotlin", "Scala", "JavaScript", "TypeScript", "JSX", "TSX",
    "CoffeeScript", "Python", "Ruby", "Perl", "PHP", "Go", "Rust", "Swift",
    "Dart", "Lua", "R", "Julia", "Haskell", "Erlang", "Elixir", "Clojure",
    "Zig", "Nim", "Assembly", "D", "OCaml", "Fortran Modern", "FORTRAN Legacy",
    "Groovy", "Solidity", "GDScript", "Vue",
}

# Glob patterns for sparse-checkout — one per source-file extension.
SOURCE_EXTS: list[str] = [
    "*.c", "*.ec", "*.pgc", "*.h", "*.cc", "*.cpp", "*.cxx", "*.c++", "*.pcc",
    "*.ino", "*.hh", "*.hpp", "*.hxx", "*.inl", "*.ipp", "*.cs", "*.csx",
    "*.m", "*.mm", "*.java", "*.kt", "*.kts", "*.sc", "*.scala",
    "*.js", "*.cjs", "*.mjs", "*.ts", "*.cts", "*.mts", "*.jsx", "*.tsx",
    "*.coffee", "*.py", "*.pyw", "*.pyi", "*.rb", "*.pl", "*.plx", "*.pm",
    "*.php", "*.go", "*.rs", "*.swift", "*.dart", "*.lua", "*.r", "*.jl",
    "*.hs", "*.erl", "*.hrl", "*.ex", "*.exs", "*.clj", "*.cljc",
    "*.zig", "*.nim", "*.s", "*.asm", "*.d", "*.ml", "*.mli",
    "*.f03", "*.f08", "*.f90", "*.f95", "*.f", "*.for", "*.ftn", "*.f77",
    "*.groovy", "*.grt", "*.gtpl", "*.gvy", "*.sol", "*.gd", "*.vue",
]


@dataclass
class RepoMetrics:
    repo: str
    files: int = 0
    lines: int = 0  # total lines (including comments/blanks)
    loc: int = 0  # scc "Code" — source lines excluding comments/blanks
    uloc: int = 0
    complexity: int = 0
    sha: str = ""  # commit SHA analyzed
    size_kb: int = 0  # GitHub API repo size in KB
    downloaded: int = 0  # actual bytes downloaded
    clone_time: float = 0.0
    analysis_time: float = 0.0
    error: str = ""

    @property
    def complexity_density(self) -> float:
        """Complexity per 100 lines of code."""
        return self.complexity / self.loc * 100 if self.loc else 0

    @property
    def cocomo_months(self) -> float:
        """COCOMO Basic organic model: effort in person-months.

        Effort = a * (KLOC)^b where a=2.4, b=1.05 for organic projects.
        """
        kloc = self.loc / 1000
        return 2.4 * (kloc ** 1.05) if kloc > 0 else 0

    @property
    def cocomo_people(self) -> float:
        """COCOMO Basic organic model: average team size.

        Duration = c * (Effort)^d where c=2.5, d=0.38.
        People = Effort / Duration.
        """
        effort = self.cocomo_months
        if effort <= 0:
            return 0
        duration = 2.5 * (effort ** 0.38)
        return effort / duration if duration > 0 else 0

    @property
    def cocomo_cost(self) -> float:
        """COCOMO estimated cost: effort * monthly salary."""
        return self.cocomo_months * AVG_MONTHLY_SALARY


# Repos < 1MB use tarball (fast, no git overhead); ≥ 1MB use sparse checkout
TARBALL_THRESHOLD_KB = 1_000


def _make_progress() -> Progress:
    """Create a standard progress bar for repo processing."""
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


def _download_tarball(repo: str, dest: str, client: httpx.Client, ref: str = "HEAD") -> tuple[float, int]:
    """Download and extract repo tarball via codeload. Returns (elapsed_s, size_bytes)."""
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    t0 = time.monotonic()
    # Retry network errors only
    last_err: Exception | None = None
    buf: io.BytesIO | None = None
    for attempt in range(3):
        try:
            with client.stream("GET", url, follow_redirects=True, timeout=30) as resp:
                resp.raise_for_status()
                buf = io.BytesIO()
                for chunk in resp.iter_bytes(65536):
                    buf.write(chunk)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    else:
        raise last_err
    size = buf.tell()
    buf.seek(0)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar:
            try:
                tar.extract(member, dest, filter="data")
            except (IsADirectoryError, NotADirectoryError, PermissionError):
                continue
    return time.monotonic() - t0, size


def _run_git(args: list[str], timeout: int, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, killing the entire process group on timeout."""
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # own process group
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)  # SIGKILL entire group
        proc.wait()
        raise


def _sparse_clone(repo: str, dest: str, size_kb: int = 0, ref: str | None = None) -> tuple[float, int]:
    """Sparse checkout: clone metadata only, then fetch source code files. Returns (elapsed_s, approx_size_bytes)."""
    url = f"https://github.com/{repo}.git"
    # Scale timeout: 60s base + 60s per 100MB of repo size
    clone_timeout = 60 + (size_kb // 100_000) * 60
    t0 = time.monotonic()
    if ref:
        # Fetch a specific commit by SHA — combine init+remote+sparse into one shell call
        os.makedirs(dest, exist_ok=True)
        exts_args = " ".join(f"'{e}'" for e in SOURCE_EXTS)
        init_script = (
            f"git init --quiet . && "
            f"git remote add origin '{url}' && "
            f"git sparse-checkout set --no-cone {exts_args}"
        )
        _run_git(["sh", "-c", init_script], timeout=10, cwd=dest)
        _run_git(
            ["git", "fetch", "--depth", "1", "--quiet", "--no-tags",
             "--filter=blob:none", "origin", ref],
            timeout=clone_timeout, cwd=dest,
        )
        _run_git(
            ["git", "checkout", "--quiet", "FETCH_HEAD"],
            timeout=clone_timeout, cwd=dest,
        )
    else:
        _run_git(
            ["git", "clone", "--depth", "1", "--quiet", "--no-tags",
             "--filter=blob:none", "--sparse", url, dest],
            timeout=clone_timeout,
        )
        _run_git(
            ["git", "sparse-checkout", "set", "--no-cone"] + SOURCE_EXTS,
            timeout=clone_timeout, cwd=dest,
        )
    elapsed = time.monotonic() - t0
    size = 0
    git_objects = os.path.join(dest, ".git", "objects", "pack")
    if os.path.isdir(git_objects):
        for f in os.listdir(git_objects):
            if f.endswith(".pack"):
                size += os.path.getsize(os.path.join(git_objects, f))
    return elapsed, size


def _analyze_repo(path: str) -> tuple[int, int, int, int, int]:
    """Run scc on a repo directory. Returns (files, lines, code, uloc, complexity)."""
    result = _run_git(
        ["scc", "--uloc", "--format", "json", path],
        timeout=120,
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


async def _process_repo(
    repo: str, base_dir: str,
    tarball_sem: asyncio.Semaphore, sparse_sem: asyncio.Semaphore,
    client: httpx.Client, size_kb: int = 0, ref: str | None = None,
) -> RepoMetrics:
    """Download and analyze a single repo."""
    metrics = RepoMetrics(repo=repo, sha=ref or "")
    dest = os.path.join(base_dir, repo.replace("/", "__"))
    use_tarball = size_kb < TARBALL_THRESHOLD_KB
    sem = tarball_sem if use_tarball else sparse_sem

    async with sem:
        try:
            loop = asyncio.get_event_loop()
            if use_tarball:
                elapsed, dl_bytes = await loop.run_in_executor(
                    None, _download_tarball, repo, dest, client, ref or "HEAD",
                )
            else:
                elapsed, dl_bytes = await loop.run_in_executor(
                    None, _sparse_clone, repo, dest, size_kb, ref,
                )
            metrics.clone_time = elapsed
            metrics.size_kb = size_kb
            metrics.downloaded = dl_bytes

            if not os.path.isdir(dest):
                metrics.error = "download failed"
                return metrics

            t0 = time.monotonic()
            files, lines, code, uloc, complexity = await loop.run_in_executor(
                None, _analyze_repo, dest,
            )
            metrics.analysis_time = time.monotonic() - t0
            metrics.files = files
            metrics.lines = lines
            metrics.loc = code
            metrics.uloc = uloc
            metrics.complexity = complexity

        except subprocess.TimeoutExpired:
            metrics.error = "timeout"
        except Exception as e:
            metrics.error = str(e)[:60]
        finally:
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)

    return metrics


async def analyze_repos(
    repos: list[str], concurrency: int = 32,
    sizes: dict[str, int] | None = None,
    refs: dict[str, str] | None = None,
    on_result: callable | None = None,
    shared_client: httpx.Client | None = None,
) -> list[RepoMetrics]:
    """Analyze multiple repos concurrently. Tarball for small, sparse for large.

    on_result: optional callback(metrics, completed_count) called after each repo.
    shared_client: reuse an existing httpx client (caller manages lifecycle).
    """
    # Tarball is pure I/O — allow higher concurrency; sparse spawns git processes
    tarball_sem = asyncio.Semaphore(min(concurrency * 2, 64))
    sparse_sem = asyncio.Semaphore(concurrency)
    sizes = sizes or {}
    refs = refs or {}
    base_dir = tempfile.mkdtemp(prefix="complexity-")
    name_width = min(max((len(r) for r in repos), default=20), 30)
    progress = _make_progress()

    results: list[RepoMetrics] = []
    owns_client = shared_client is None
    client = shared_client or httpx.Client(http2=True)
    pool = ThreadPoolExecutor(max_workers=concurrency * 2 + 4)
    loop = asyncio.get_event_loop()
    loop.set_default_executor(pool)

    try:
        with progress:
            task = progress.add_task(" " * name_width, total=len(repos))

            async def _run(repo: str) -> RepoMetrics:
                m = await _process_repo(
                    repo, base_dir, tarball_sem, sparse_sem, client,
                    sizes.get(repo, 0), refs.get(repo),
                )
                desc = repo[:name_width].ljust(name_width)
                progress.update(task, advance=1, description=desc)
                return m

            tasks = [_run(r) for r in repos]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                results.append(result)
                if on_result:
                    on_result(result, len(results))
    finally:
        pool.shutdown(wait=False)
        if owns_client:
            client.close()
        shutil.rmtree(base_dir, ignore_errors=True)

    return results


def _load_repos(filepath: str) -> tuple[list[str], dict[str, int]]:
    """Load A/B repos and their sizes from value-data.csv (enriched with top-repos.csv).

    `filepath` is treated as the value-data path. Sizes come from top-repos.csv
    via the helper; repos missing there get size 0 and fall back to sparse-clone
    (the tarball threshold is 1MB, so unknown sizes default to safe behavior).
    """
    entries = load_ab_repos(value_file=filepath)
    repos = [e.repo for e in entries]
    sizes = {e.repo: e.size_kb for e in entries if e.size_kb}
    return repos, sizes


def _filter_by_ttl(repos: list[str], output: str, ttl_days: int) -> tuple[list[str], int]:
    """Filter out repos fetched within TTL. Returns (repos_to_fetch, skipped)."""
    if not os.path.exists(output) or ttl_days <= 0:
        return repos, 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    fresh: set[str] = set()
    with open(output, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("fetched_at", "")
            if ts:
                try:
                    fetched = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if fetched >= cutoff:
                        fresh.add(row["repo"])
                except ValueError:
                    pass
    filtered = [r for r in repos if r not in fresh]
    return filtered, len(repos) - len(filtered)


def _load_existing(filepath: str) -> dict[str, dict]:
    """Load existing complexity rows keyed by repo."""
    if not os.path.exists(filepath):
        return {}
    existing: dict[str, dict] = {}
    with open(filepath, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing[row["repo"]] = row
    return existing


def _write_csv(filepath: str, results: list[RepoMetrics], existing: dict[str, dict]) -> None:
    """Merge new results with existing data and write CSV."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)

    for m in results:
        if m.error:
            continue
        existing[m.repo] = {
            "repo": m.repo,
            "files": m.files,
            "loc": m.loc,
            "uloc": m.uloc,
            "complexity": m.complexity,
            "complexity_density": round(m.complexity_density, 1),
            "cocomo_months": round(m.cocomo_months, 1),
            "cocomo_people": round(m.cocomo_people, 1),
            "cocomo_cost": round(m.cocomo_cost),
            "fetched_at": now,
        }

    rows = sorted(existing.values(), key=lambda r: -int(r.get("loc", 0)))
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _diff_color(text: str, current: float, previous: float | None) -> str:
    """Color text green if higher than previous, red if lower, plain if same or no previous."""
    if previous is None or current == previous:
        return text
    if current > previous:
        return f"[green]{text}[/green]"
    return f"[red]{text}[/red]"



def _fmt_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f}G"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f}M"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f}K"
    return f"{size_bytes}B"


def _parse_link_last_page(link_header: str) -> int | None:
    """Extract `page=N` from a Link header's rel="last" entry. None if absent."""
    if not link_header:
        return None
    from urllib.parse import urlparse, parse_qs
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="last"' in part:
            url = part.split(";", 1)[0].strip().lstrip("<").rstrip(">")
            q = parse_qs(urlparse(url).query)
            pages = q.get("page", [])
            if pages:
                try:
                    return int(pages[0])
                except ValueError:
                    return None
    return None


async def _fetch_commit_shas_multi(
    pairs: list[tuple[str, int]], concurrency: int = 32,
    sha_data: dict[tuple[str, str], dict[str, str]] | None = None,
    sha_file: str = "",
) -> dict[tuple[str, int], dict[str, str]]:
    """For each (repo, year) pair, fetch first/last commit SHA + total commits IN year.

    Per (repo, year):
      Call A: GET /commits?since=Y-01-01&until=Y+1-01-01&per_page=1
        → newest commit IN year (first record), commits-in-year (Link rel="last" page)
      Call B (only if commits > 0): same URL with &page={commits}
        → oldest commit IN year

    Returns {(repo, year): {"first_sha": str, "last_sha": str, "commits": int}}.
    Inactive years (no commits) get empty SHAs and commits=0.

    Flushes incrementally to `sha_file` every 500 fetches if both provided.
    """
    revolver = get_revolver()
    sem = asyncio.Semaphore(concurrency)
    results: dict[tuple[str, int], dict[str, str]] = {}
    total = len(pairs)
    _fetched = 0
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    progress = _make_progress()
    name_width = min(max((len(r) for r, _ in pairs), default=20), 30)

    async def _request(session, url: str) -> tuple[int, list[dict], str]:
        """Make one authenticated request via the revolver. Returns (status, body, link)."""
        token = revolver.best_token()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await session.get(url, headers=headers)
        try:
            if token:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset = resp.headers.get("X-RateLimit-Reset")
                revolver.update(
                    token,
                    int(remaining) if remaining else None,
                    float(reset) if reset else None,
                )
            link = resp.headers.get("Link", "")
            status = resp.status
            body = await resp.json() if status == 200 else []
        finally:
            await resp.release()
        return status, body, link

    async with aiohttp.ClientSession(
        headers={"Accept": "application/vnd.github+json"},
    ) as session:
        with progress:
            task = progress.add_task(" " * name_width, total=total)

            async def _fetch_one(repo: str, year: int) -> None:
                nonlocal _fetched
                since = f"{year}-01-01T00:00:00Z"
                until = f"{year + 1}-01-01T00:00:00Z"
                base = f"{GITHUB_API}/repos/{repo}/commits?since={since}&until={until}&per_page=1"
                first_sha, last_sha, commits = "", "", 0
                async with sem:
                    try:
                        status, body, link = await _request(session, base)
                        if status == 200 and body:
                            last_sha = body[0]["sha"]
                            commits = _parse_link_last_page(link) or len(body)
                            if commits > 1:
                                _, body2, _ = await _request(session, f"{base}&page={commits}")
                                if body2:
                                    first_sha = body2[0]["sha"]
                            else:
                                first_sha = last_sha  # 1 commit → first==last
                    except Exception as e:
                        log.debug("Failed to get SHAs for %s@%d: %s", repo, year, e)
                    finally:
                        results[(repo, year)] = {
                            "first_sha": first_sha,
                            "last_sha": last_sha,
                            "commits": str(commits),
                        }
                        if sha_data is not None:
                            sha_data[(repo, str(year))] = {
                                "first_sha": first_sha,
                                "last_sha": last_sha,
                                "commits": str(commits),
                                "fetched_at": fetched_at,
                            }
                        _fetched += 1
                        if sha_data is not None and sha_file and _fetched % 500 == 0:
                            _write_sha_data(sha_file, sha_data)
                        desc = repo[:name_width].ljust(name_width)
                        progress.update(task, advance=1, description=desc)

            await asyncio.gather(*[_fetch_one(r, y) for r, y in pairs])

    if sha_data is not None and sha_file:
        _write_sha_data(sha_file, sha_data)

    return results


def _load_yearly_metrics(dirpath: str) -> dict[tuple[str, str], dict[str, str]]:
    """Load yearly metrics from split per-metric CSVs. Returns {(repo, metric): {year: value}}."""
    data: dict[tuple[str, str], dict[str, str]] = {}
    for metric, fname in YEARLY_METRIC_FILES.items():
        path = os.path.join(dirpath, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            year_cols = [fn for fn in (reader.fieldnames or []) if fn != "repo"]
            for row in reader:
                year_vals = {y: row[y] for y in year_cols if row.get(y)}
                if year_vals:
                    data[(row["repo"], metric)] = year_vals
    return data


def _write_yearly_metrics(
    dirpath: str,
    data: dict[tuple[str, str], dict[str, str]],
) -> None:
    """Write yearly metrics to split per-metric CSVs (one wide file per metric)."""
    os.makedirs(dirpath, exist_ok=True)

    # Group by metric; collect union of year columns across all metrics
    by_metric: dict[str, dict[str, dict[str, str]]] = {m: {} for m in YEARLY_METRIC_FILES}
    year_set: set[str] = set()
    for (repo, metric), year_vals in data.items():
        if metric not in by_metric:
            continue
        by_metric[metric][repo] = year_vals
        year_set.update(year_vals.keys())
    year_cols = sorted(year_set)

    for metric, fname in YEARLY_METRIC_FILES.items():
        repos_data = by_metric[metric]
        path = os.path.join(dirpath, fname)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["repo"] + year_cols)
            for repo in sorted(repos_data):
                writer.writerow([repo] + [repos_data[repo].get(y, "") for y in year_cols])


SHA_FIELDS = ["repo", "year", "first_sha", "last_sha", "commits", "fetched_at"]


def _load_sha_data(filepath: str) -> dict[tuple[str, str], dict[str, str]]:
    """Load git/years.csv → {(repo, year_str): {first_sha, last_sha, commits, fetched_at}}.

    Backward-compatible: if the CSV still has only the old `last_sha` column,
    we read that and synthesize the new dict with empty first_sha/commits.
    """
    data: dict[tuple[str, str], dict[str, str]] = {}
    if not os.path.exists(filepath):
        return data
    with open(filepath, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["repo"], row["year"])
            data[key] = {
                "first_sha": row.get("first_sha", ""),
                "last_sha":  row.get("last_sha", ""),
                "commits":   row.get("commits", ""),
                "fetched_at": row.get("fetched_at", ""),
            }
    return data


def _write_sha_data(
    filepath: str,
    data: dict[tuple[str, str], dict[str, str]],
) -> None:
    """Write git/years.csv with columns: repo, year, first_sha, last_sha, commits, fetched_at."""
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHA_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for (repo, year), row in sorted(data.items()):
            writer.writerow({
                "repo": repo, "year": year,
                "first_sha": row.get("first_sha", ""),
                "last_sha":  row.get("last_sha", ""),
                "commits":   row.get("commits", ""),
                "fetched_at": row.get("fetched_at", ""),
            })


def resolve_snapshot_sha(
    sha_data: dict[tuple[str, str], dict[str, str]],
    repo: str, year: int,
) -> str:
    """Find a usable snapshot SHA for `repo` at end of `year`.

    Per the schema, `last_sha` is the last commit IN that year and is empty
    when the repo had no commits in that year. For LOC / sparse-checkout
    purposes we still want the most recent codebase state at-or-before
    year-end — so when last_sha is empty, walk back through earlier years
    until we find a populated last_sha. Returns "" if no SHA found at all
    (project hadn't started yet, or empty repo).
    """
    for y in range(year, year - 10, -1):  # cap lookback at 10y
        sha = sha_data.get((repo, str(y)), {}).get("last_sha") or ""
        if sha:
            return sha
    return ""


def _repos_with_year_data(
    data: dict[tuple[str, str], dict[str, str]], year: str,
) -> set[str]:
    """Return repos that have been analyzed for the given year (loc is non-empty)."""
    repos: set[str] = set()
    for (repo, metric), year_vals in data.items():
        if metric == "loc" and year in year_vals:
            repos.add(repo)
    return repos


def _upsert_metrics(
    yearly_data: dict[tuple[str, str], dict[str, str]],
    repo: str, year_str: str, m: RepoMetrics,
) -> None:
    """Write a RepoMetrics into yearly_data for a given year."""
    density = m.complexity / m.loc * 100 if m.loc else 0
    yearly_data.setdefault((repo, "files"), {})[year_str] = str(m.files)
    yearly_data.setdefault((repo, "loc"), {})[year_str] = str(m.lines)
    yearly_data.setdefault((repo, "sloc"), {})[year_str] = str(m.loc)
    yearly_data.setdefault((repo, "uloc"), {})[year_str] = str(m.uloc)
    yearly_data.setdefault((repo, "scc_complexity"), {})[year_str] = str(m.complexity)
    yearly_data.setdefault((repo, "scc_density"), {})[year_str] = f"{density:.2f}"


def _copy_metrics_from_sha(
    repo: str, sha: str, sizes: dict[str, int],
    sha_results: dict[str, RepoMetrics],
) -> RepoMetrics:
    """Create a RepoMetrics by copying from a previously analyzed SHA."""
    src = sha_results[sha]
    return RepoMetrics(
        repo=repo, sha=sha, size_kb=sizes.get(repo, 0),
        files=src.files, lines=src.lines, loc=src.loc,
        uloc=src.uloc, complexity=src.complexity,
    )


def _year_main(args: argparse.Namespace) -> None:
    """Year-mode: analyze repos at end-of-year snapshot for one or more years."""
    years: list[int] = sorted(args.year)

    repos_all, sizes = _load_repos(args.input)
    total_repos = len(repos_all)

    # If --limit, pick a consistent sample across all years
    if args.limit:
        repos_sample = random.sample(repos_all, min(args.limit, len(repos_all)))
    else:
        repos_sample = repos_all

    yearly_data = _load_yearly_metrics(args.yearly_output)
    sha_data = _load_sha_data(args.sha_file) if not args.force else {}

    # --- Step 1: Collect (repo, year) pairs needing SHA fetch vs analysis ---
    pairs_to_fetch: list[tuple[str, int]] = []  # need SHA from API
    for year in years:
        yr = str(year)
        for r in repos_sample:
            if args.force or (r, yr) not in sha_data:
                pairs_to_fetch.append((r, year))

    # Resolve a usable snapshot SHA per (repo, year) for sparse-checkout. The
    # schema records `last_sha` per year as the last commit IN that year and
    # leaves it empty when the year had no commits — but for LOC analysis we
    # still want a year-end codebase snapshot, so cascade backward to the most
    # recent populated year via resolve_snapshot_sha().
    all_shas: dict[tuple[str, int], str] = {}
    for year in years:
        for r in repos_sample:
            sha = resolve_snapshot_sha(sha_data, r, year)
            if sha:
                all_shas[(r, year)] = sha

    needs_analysis = not pairs_to_fetch
    for year in years:
        yr = str(year)
        analyzed = _repos_with_year_data(yearly_data, yr)
        for r in repos_sample:
            if (r, year) in all_shas and r not in analyzed:
                needs_analysis = True
                break
        if needs_analysis:
            break

    if not pairs_to_fetch and not needs_analysis:
        console.print("[dim]All repos have data for all requested years[/dim]")
        return

    years_str = ", ".join(str(y) for y in years)
    console.print(f"[bold]Code Complexity {years_str}[/bold]  [dim]{len(repos_sample)} repos × {len(years)} years[/dim]")
    console.print()

    # Fetch missing SHAs in one batch
    if pairs_to_fetch:
        console.print(f"[dim]Fetching {len(pairs_to_fetch):,} commit SHAs...[/dim]")
        new_shas = asyncio.run(_fetch_commit_shas_multi(
            pairs_to_fetch, concurrency=args.concurrency,
            sha_data=sha_data, sha_file=args.sha_file,
        ))
        # `new_shas` returns {(repo, year): {first_sha, last_sha, commits}}; for
        # LOC purposes we still need a snapshot SHA, so resolve via cascade.
        for (repo, year) in new_shas:
            sha = resolve_snapshot_sha(sha_data, repo, year)
            if sha:
                all_shas[(repo, year)] = sha

        # Repos with no commit IN year AND no prior populated year → mark zero
        # for yearly metrics (project hadn't started, or empty repo).
        no_commit: set[tuple[str, int]] = set()
        for repo, year in pairs_to_fetch:
            if (repo, year) not in all_shas:
                no_commit.add((repo, year))
                yr = str(year)
                # Make sure there's a row in sha_data so we don't keep refetching
                sha_data.setdefault((repo, yr), {
                    "first_sha": "", "last_sha": "", "commits": "0",
                    "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                })
                for metric in YEARLY_METRICS:
                    yearly_data.setdefault((repo, metric), {})[yr] = "0"

        _write_sha_data(args.sha_file, sha_data)
        _write_yearly_metrics(args.yearly_output, yearly_data)

        console.print(f"[dim]Found {len(new_shas):,} SHAs, {len(no_commit):,} pre-history[/dim]")
        console.print()
    else:
        console.print(f"[dim]All {len(all_shas):,} SHAs already cached[/dim]")
        console.print()

    if args.shas_only:
        console.print(f"[green]commits-years.csv written → {args.sha_file}[/green]")
        console.print("[dim]Skipping sparse-checkout + scc (use without --shas-only to continue).[/dim]")
        return

    # --- Step 2: Group unique SHAs by earliest year, deduplicate ---
    sha_year_map: dict[str, list[tuple[str, int]]] = {}  # sha -> all (repo, year) pairs
    for (repo, year), sha in all_shas.items():
        sha_year_map.setdefault(sha, []).append((repo, year))

    # For each unique SHA, determine the earliest year it appears and whether it needs analysis
    sha_results: dict[str, RepoMetrics] = {}

    # Pre-populate sha_results from yearly_data (already analyzed SHAs)
    for sha, pairs in sha_year_map.items():
        repo = pairs[0][0]
        # Find any year where this SHA was already analyzed (has loc data)
        source_year = None
        for r, y in pairs:
            yr = str(y)
            loc_val = yearly_data.get((r, "loc"), {}).get(yr)
            if loc_val is not None:
                source_year = yr
                repo = r
                break
        if source_year:
            m = RepoMetrics(repo=repo, sha=sha, size_kb=sizes.get(repo, 0))
            m.files = int(yearly_data.get((repo, "files"), {}).get(source_year, "0"))
            m.lines = int(yearly_data.get((repo, "loc"), {}).get(source_year, "0"))
            m.loc = int(yearly_data.get((repo, "sloc"), {}).get(source_year, "0"))
            m.uloc = int(yearly_data.get((repo, "uloc"), {}).get(source_year, "0"))
            m.complexity = int(yearly_data.get((repo, "scc_complexity"), {}).get(source_year, "0"))
            sha_results[sha] = m

    # Group SHAs needing analysis by their earliest year
    shas_by_year: dict[int, dict[str, str]] = {}  # year -> {repo: sha}
    for sha, pairs in sha_year_map.items():
        if sha in sha_results:
            continue
        earliest_year = min(y for _, y in pairs)
        # Pick the repo from the earliest year pair
        repo = next(r for r, y in pairs if y == earliest_year)
        shas_by_year.setdefault(earliest_year, {})[repo] = sha

    total_to_analyze = sum(len(v) for v in shas_by_year.values())
    total_deduped = len(all_shas) - total_to_analyze - sum(
        len(pairs) for sha, pairs in sha_year_map.items() if sha in sha_results
    )
    console.print(f"[dim]{len(all_shas):,} SHAs, {total_to_analyze:,} unique to analyze[/dim]")
    console.print()

    # --- Step 3: Process year by year — analyze new SHAs, then display all repos ---
    repo_order: list[str] | None = None
    prev_metrics: dict[str, RepoMetrics] = {}
    total_analyzed = 0
    total_elapsed = 0.0
    _flush_count = 0
    shared_client = httpx.Client(http2=True)

    for year in years:
        year_str = str(year)
        cached_before = _repos_with_year_data(yearly_data, year_str)

        # Analyze SHAs first appearing in this year
        year_refs = shas_by_year.get(year, {})
        year_errors: list[RepoMetrics] = []
        elapsed = 0.0

        if year_refs:
            def _on_result(m: RepoMetrics, completed: int) -> None:
                nonlocal _flush_count
                if m.error:
                    year_errors.append(m)
                    return
                sha_results[m.sha] = m
                # Upsert for all years sharing this SHA
                for r, y in sha_year_map.get(m.sha, []):
                    _upsert_metrics(yearly_data, r, str(y), m)
                    _flush_count += 1
                if _flush_count >= 500:
                    _write_yearly_metrics(args.yearly_output, yearly_data)
                    _flush_count = 0

            t_start = time.monotonic()
            asyncio.run(analyze_repos(
                list(year_refs.keys()),
                concurrency=args.concurrency,
                sizes=sizes, refs=year_refs,
                on_result=_on_result,
                shared_client=shared_client,
            ))
            elapsed = time.monotonic() - t_start
            total_analyzed += len(year_refs)
            total_elapsed += elapsed

        # Build this year's full results (analyzed + copied from sha_results)
        ok: list[RepoMetrics] = []
        errors: list[RepoMetrics] = list(year_errors)
        for repo in repos_sample:
            if repo in cached_before:
                continue
            sha = all_shas.get((repo, year))
            if not sha or sha not in sha_results:
                continue
            m = _copy_metrics_from_sha(repo, sha, sizes, sha_results)
            src = sha_results[sha]
            if src.repo == repo:
                m.clone_time = src.clone_time
                m.analysis_time = src.analysis_time
                m.downloaded = src.downloaded
            ok.append(m)

        if not ok and not errors:
            console.print(f"[dim]All repos have {year} data, nothing to analyze[/dim]")
            console.print()
            continue

        # Upsert any remaining (copied repos not yet in yearly_data)
        for m in ok:
            _upsert_metrics(yearly_data, m.repo, year_str, m)

        _write_yearly_metrics(args.yearly_output, yearly_data)
        _flush_count = 0

        # Display results table — first year sets order, subsequent years follow it
        metrics_by_repo = {m.repo: m for m in ok}
        by_time = sorted(ok, key=lambda m: -(m.clone_time + m.analysis_time))
        if repo_order is None:
            repo_order = [m.repo for m in by_time]
        ordered = [metrics_by_repo[r] for r in repo_order if r in metrics_by_repo]
        seen = set(repo_order)
        for m in by_time:
            if m.repo not in seen:
                ordered.append(m)
        show = ordered[:30]

        tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
        tbl.add_column("Repo")
        tbl.add_column("Size", justify="right")
        tbl.add_column("DL", justify="right")
        tbl.add_column("Files", justify="right")
        tbl.add_column("kLOC", justify="right")
        tbl.add_column("Cmplx", justify="right")
        tbl.add_column("Dnst", justify="right")
        tbl.add_column("Time", justify="right")

        for m in show:
            kloc = m.loc / 1000
            kloc_str = f"{kloc:,.0f}" if kloc >= 1 else f"{kloc:,.1f}"
            mb = round(m.size_kb / 1_000)
            repo_time = m.clone_time + m.analysis_time

            prev = prev_metrics.get(m.repo)
            size_s = _diff_color(str(mb), mb, round(prev.size_kb / 1_000) if prev else None)
            dl_s = _diff_color(_fmt_size(m.downloaded), m.downloaded, prev.downloaded if prev else None)
            files_s = _diff_color(f"{m.files:,}", m.files, prev.files if prev else None)
            kloc_s = _diff_color(kloc_str, m.loc, prev.loc if prev else None)
            cmplx_s = _diff_color(f"{m.complexity:,}", m.complexity, prev.complexity if prev else None)
            dnst_s = _diff_color(f"{m.complexity_density:.0f}", m.complexity_density, prev.complexity_density if prev else None)

            tbl.add_row(
                m.repo,
                size_s,
                dl_s,
                files_s,
                kloc_s,
                cmplx_s,
                dnst_s,
                f"[dim]{repo_time:.0f}[/dim]",
            )

        if len(ordered) > len(show):
            rest = ordered[len(show):]
            rest_kloc = sum(m.loc for m in rest) / 1000
            tbl.add_row(
                f"[dim]... {len(rest)} more[/dim]", "", "", "",
                f"[dim]{rest_kloc:,.0f}[/dim]", "", "", "",
            )

        console.print(tbl)
        console.print()

        prev_metrics = metrics_by_repo

        # Per-year summary
        n = len(ok)
        n_new = len(year_refs)
        n_copied = n - n_new + len(year_errors)

        summary = Table(show_header=False, padding=(0, 1), box=None)
        summary.add_column(style="dim", min_width=12)
        summary.add_column()
        summary.add_row("Year", f"[bold]{year}[/bold]")
        parts = [f"[bold]{n:,}[/bold] repos"]
        if n_new and n_new != n:
            parts.append(f"[dim]{n_new} analyzed[/dim]")
        if n_copied:
            parts.append(f"[dim]{n_copied} deduped[/dim]")
        if elapsed:
            parts.append(f"[dim]in {elapsed:.1f}s | {n_new * 60 / elapsed:.0f} repos/min[/dim]")
        summary.add_row("Processed", " ".join(parts))
        if errors:
            summary.add_row("Errors", f"[red]{len(errors):,}[/red] "
                             f"[dim]({', '.join(m.repo + ': ' + m.error for m in errors[:3])})[/dim]")
        if ok and elapsed:
            tot_mb_y = round(sum(m.size_kb for m in ok) / 1_000)
            tot_files_y = sum(m.files for m in ok)
            tot_kloc_y = sum(m.loc for m in ok) / 1000
            tot_dl_mb_y = sum(m.downloaded for m in ok) / 1_000_000
            dl_speed_y = tot_dl_mb_y / elapsed if elapsed else 0
            compression_y = tot_mb_y / tot_dl_mb_y if tot_dl_mb_y else 0
            summary.add_row("Processing", f"{n_new / elapsed:.1f} repo/s [dim]|[/dim] {tot_mb_y / elapsed:.1f} MB/s [dim]|[/dim] {tot_files_y / elapsed:.0f} files/s [dim]|[/dim] {tot_kloc_y / elapsed:.0f} kLOC/s")
            summary.add_row("Network", f"{tot_dl_mb_y:.0f} MB downloaded [dim]|[/dim] {dl_speed_y:.1f} MB/s [dim]|[/dim] {compression_y:.0f}x compression")
        console.print(summary)
        console.print()

    shared_client.close()

    # Overall summary
    overall = Table(show_header=False, padding=(0, 1), box=None)
    overall.add_column(style="dim", min_width=12)
    overall.add_column()
    overall.add_row("SHAs fetched", f"{len(all_shas):,} [dim]({len(pairs_to_fetch):,} API calls)[/dim]")
    if total_elapsed:
        overall.add_row("Analyzed", f"[bold]{total_analyzed:,}[/bold] unique SHAs [dim]in {total_elapsed:.1f}s | {total_analyzed * 60 / total_elapsed:.0f} repos/min[/dim]")
    else:
        overall.add_row("Analyzed", f"[bold]{total_analyzed:,}[/bold] unique SHAs")
    # Coverage: repos that have data for ALL requested years
    year_strs = [str(y) for y in years]
    year_sets = [_repos_with_year_data(yearly_data, yr) for yr in year_strs]
    full_coverage = year_sets[0]
    for s in year_sets[1:]:
        full_coverage = full_coverage & s
    overall.add_row("Coverage", f"[bold]{len(full_coverage):,}[/bold] / {total_repos:,} repos [dim]with all {len(years)} years[/dim]")
    overall.add_row("Output", args.yearly_output)
    console.print(overall)
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze code complexity for GitHub repos")
    parser.add_argument("--input", default=REPOS_FILE,
                        help=f"CSV with repos (default: {REPOS_FILE})")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"Output CSV (default: {OUTPUT_FILE})")
    parser.add_argument("--limit", type=int, help="Process N random eligible repos")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip repos fetched within N days (default: {DEFAULT_TTL_DAYS}, 0 to force refresh)")
    parser.add_argument("--concurrency", type=int, default=32,
                        help="Parallel download/analyze workers (default: 32)")
    parser.add_argument("--year", type=int, nargs="+",
                        help="Analyze repos at end-of-year snapshot (e.g. --year 2024 2025)")
    parser.add_argument("--yearly-output", default=YEARLY_OUTPUT_DIR,
                        help=f"Directory for split per-metric yearly CSVs (default: {YEARLY_OUTPUT_DIR})")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch all (repo, year) commit SHAs even if already cached "
                             "in commits-years.csv")
    parser.add_argument("--shas-only", action="store_true",
                        help="Stop after writing commits-years.csv — skip the sparse-checkout + "
                             "scc analysis. Use for the lightweight refresh that feeds "
                             "later steps (LOC/complexity, activity classes).")
    parser.add_argument("--sha-file", default=SHA_FILE,
                        help=f"SHA coordination CSV (default: {SHA_FILE})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.year:
        return _year_main(args)

    existing = _load_existing(args.output)
    existing_count = len(existing)

    repos, sizes = _load_repos(args.input)
    total_repos = len(repos)
    repos, skipped = _filter_by_ttl(repos, args.output, args.ttl)
    if args.limit:
        repos = random.sample(repos, min(args.limit, len(repos)))

    if not repos:
        console.print(f"[dim]All repos fresh (TTL {args.ttl}d), nothing to analyze[/dim]")
        return

    console.print(f"[bold]Code Complexity[/bold]  [dim]{len(repos)} repos, "
                  f"{args.concurrency} workers[/dim]")
    console.print()

    t_start = time.monotonic()
    results = asyncio.run(analyze_repos(repos, concurrency=args.concurrency, sizes=sizes))
    elapsed = time.monotonic() - t_start

    ok = [m for m in results if not m.error]
    errors = [m for m in results if m.error]

    if ok:
        _write_csv(args.output, ok, existing)

    # Results table
    by_loc = sorted(ok, key=lambda m: -(m.clone_time + m.analysis_time))
    show = by_loc[:30]

    tbl = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    tbl.add_column("Repo")
    tbl.add_column("Size", justify="right")
    tbl.add_column("DL", justify="right")
    tbl.add_column("Files", justify="right")
    tbl.add_column("kLOC", justify="right")
    tbl.add_column("Cmplx", justify="right")
    tbl.add_column("Dnst", justify="right")
    tbl.add_column("Time", justify="right")

    for m in show:
        kloc = m.loc / 1000
        kloc_str = f"{kloc:,.0f}" if kloc >= 1 else f"{kloc:,.1f}"
        mb = round(m.size_kb / 1_000)
        repo_time = m.clone_time + m.analysis_time
        tbl.add_row(
            m.repo,
            str(mb),
            _fmt_size(m.downloaded),
            f"{m.files:,}",
            kloc_str,
            f"{m.complexity:,}",
            f"{m.complexity_density:.0f}",
            f"[dim]{repo_time:.0f}[/dim]",
        )

    if len(by_loc) > len(show):
        rest = by_loc[len(show):]
        rest_kloc = sum(m.loc for m in rest) / 1000
        tbl.add_row(
            f"[dim]... {len(rest)} more[/dim]", "", "", "",
            f"[dim]{rest_kloc:,.0f}[/dim]", "", "", "",
        )

    tbl.add_section()
    n = len(ok)
    avg_mb = round(sum(m.size_kb for m in ok) / n / 1_000) if n else 0
    avg_dl = round(sum(m.downloaded for m in ok) / n) if n else 0
    avg_files = sum(m.files for m in ok) / n if n else 0
    avg_kloc = sum(m.loc for m in ok) / n / 1000 if n else 0
    avg_complexity = sum(m.complexity for m in ok) / n if n else 0
    avg_density = sum(m.complexity_density for m in ok) / n if n else 0
    avg_time = sum(m.clone_time + m.analysis_time for m in ok) / n if n else 0
    tbl.add_row(
        "[bold]Avg[/bold]",
        f"[bold]{avg_mb}[/bold]",
        f"[bold]{_fmt_size(avg_dl)}[/bold]",
        f"[bold]{avg_files:,.0f}[/bold]",
        f"[bold]{avg_kloc:,.0f}[/bold]",
        f"[bold]{avg_complexity:,.0f}[/bold]",
        f"[bold]{avg_density:.0f}[/bold]",
        f"[bold]{avg_time:.0f}[/bold]",
    )

    tbl.add_section()
    tot_mb = round(sum(m.size_kb for m in ok) / 1_000)
    tot_dl = sum(m.downloaded for m in ok)
    tot_files = sum(m.files for m in ok)
    tot_kloc = sum(m.loc for m in ok) / 1000
    tot_time = sum(m.clone_time + m.analysis_time for m in ok)
    tbl.add_row(
        "[bold]Total[/bold]",
        f"[bold]{tot_mb}[/bold]",
        f"[bold]{_fmt_size(tot_dl)}[/bold]",
        f"[bold]{tot_files:,}[/bold]",
        f"[bold]{tot_kloc:,.0f}[/bold]",
        "",
        "",
        f"[bold]{tot_time:.0f}[/bold]",
    )

    console.print(tbl)
    console.print()

    # Perf stats
    summary = Table(show_header=False, padding=(0, 1), box=None)
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("Analyzed", f"[bold]{len(ok):,}[/bold] repos [dim]in {elapsed:.1f}s | {n * 60 / elapsed:.0f} repos/min[/dim]")
    if errors:
        summary.add_row("Errors", f"[red]{len(errors):,}[/red] "
                         f"[dim]({', '.join(m.repo + ': ' + m.error for m in errors[:3])})[/dim]")
    if ok:
        repo_sec = len(ok) / elapsed if elapsed else 0
        mb_sec = tot_mb / elapsed if elapsed else 0
        files_sec = tot_files / elapsed if elapsed else 0
        kloc_sec = tot_kloc / elapsed if elapsed else 0
        summary.add_row("Processing", f"{repo_sec:.1f} repo/s [dim]|[/dim] {mb_sec:.1f} MB/s [dim]|[/dim] {files_sec:.0f} files/s [dim]|[/dim] {kloc_sec:.0f} kLOC/s")
        tot_dl_mb = sum(m.downloaded for m in ok) / 1_000_000
        dl_speed = tot_dl_mb / elapsed if elapsed else 0
        tot_size_mb = tot_mb
        compression = tot_size_mb / tot_dl_mb if tot_dl_mb else 0
        summary.add_row("Network", f"{tot_dl_mb:.0f} MB downloaded [dim]|[/dim] {dl_speed:.1f} MB/s [dim]|[/dim] {compression:.0f}x compression")
    summary.add_row("Coverage", f"{existing_count + len(ok):,} / {total_repos:,} repos [dim]({existing_count:,} existing)[/dim]")
    summary.add_row("Output", args.output)
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
