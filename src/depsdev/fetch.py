"""Fetch deps.dev project + Best-Practices badge data per eligible repo.

For each eligible repo (`data/eligibility-data.csv`, eligibility=True), we
hit two free APIs:

1. **deps.dev** (https://api.deps.dev) — Google's dependency-graph index.
   - `GET /v3/projects/github.com%2F{owner}%2F{repo}` → project info, including
     a mirrored OpenSSF Scorecard (`scorecard.overallScore`, `scorecard.date`)
     that's typically fresher than our local `data/openssf/scores.csv`.
   - `GET /v3alpha/systems/{ECO}/packages/{pkg}/versions/{default}:dependents`
     → `dependentCount` for a package. We sum across all (eco, pkg) pairs
     mapped to the repo via `data/{npm,pypi,crates}/results.csv`. Debian
     packages (cpp) are skipped — deps.dev doesn't index Debian.

2. **OpenSSF Best Practices Badge** (https://www.bestpractices.dev) — separate
   service; same script for convenience.
   - `GET /projects.json?url=https://github.com/{owner}/{repo}` → project list.
     We pick the first match and capture `badge_level` and `tiered_percentage`.

For each eligible repo, we capture:
    depsdev_scorecard_overall   (0..10, fresh OpenSSF mirror)
    depsdev_scorecard_date      (ISO date)
    depsdev_dependent_count     (int, summed across mapped pkgs; "" if
                                 Debian-only or no published default version)
    depsdev_license             (SPDX-ish, from deps.dev project)
    depsdev_open_issues         (int, GitHub open-issue count from deps.dev)
    bestpractices_badge_id      (passing | silver | gold | "")
    bestpractices_tiered_percentage  (0..300, sum across tiers)
    fetched_at                  (ISO timestamp, this run)

Output: `data/depsdev/repos.csv` (sorted by repo).

Usage:
    uv run python -m src.depsdev.fetch                        # full run (899)
    uv run python -m src.depsdev.fetch --limit 5 -v           # quick test
    uv run python -m src.depsdev.fetch --concurrency 30
    uv run python -m src.depsdev.fetch --force                # ignore TTL

Polite by default: 20 concurrent workers, ~50ms gap per worker between
requests. Retries 429/5xx with exponential backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import random
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from yarl import URL

from src.osv.fetch_cves import load_repo_package_mapping
from src.pipeline.repos import load_eligible_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "depsdev" / "repos.csv"

FIELDS = [
    "repo", "repo_id",
    "depsdev_scorecard_overall", "depsdev_scorecard_date",
    "depsdev_dependent_count", "depsdev_license", "depsdev_open_issues",
    "bestpractices_badge_id", "bestpractices_tiered_percentage",
    "fetched_at",
]

DEPSDEV_BASE = "https://api.deps.dev"
BESTPRACTICES_URL = "https://www.bestpractices.dev/projects.json"

# Map our internal ecosystem name (from `osv/fetch_cves.ECOSYSTEM_FILES`) to
# the deps.dev system identifier used in URLs. deps.dev does NOT index Debian
# (cpp packages), so we drop those — the dependent count for cpp repos will
# be empty unless they happen to also publish to npm/pypi/crates.
ECO_TO_DEPSDEV_SYSTEM: dict[str, str] = {
    "npm": "npm",
    "PyPI": "pypi",
    "crates.io": "cargo",
    # "Debian" intentionally omitted — deps.dev returns "package not found"
}

TTL_DAYS_DEFAULT = 30
REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 4
WORKER_MIN_INTERVAL_S = 0.05  # ~20 req/sec per worker (deps.dev tolerates this)

# bestpractices.dev rate-limits hard (~5 req/sec global). We keep one
# global semaphore + min-interval gate around its requests so concurrency
# stays ≤ BESTPRACTICES_MAX_INFLIGHT and request gaps ≥ BESTPRACTICES_GAP_S.
BESTPRACTICES_MAX_INFLIGHT = 1
BESTPRACTICES_GAP_S = 0.25  # ~4 req/sec global to that host


# ── deps.dev fetch ───────────────────────────────────────────────────────────


async def _request_json(
    session: aiohttp.ClientSession,
    url: str | URL,
    *,
    label: str,
) -> dict | None:
    """GET `url`, return parsed JSON. Retries 429/5xx. Returns None on 404
    (legitimate "no such project/package") or after exhausting retries.

    A None from this layer means "no data" — the caller maps that to empty
    output cells. We deliberately do NOT distinguish 404 from transport
    failure in the caller's behaviour: both mean "leave the field blank".
    """
    backoff = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 404:
                    log.debug("%s 404 — %s", label, url)
                    return None
                if resp.status == 429 or 500 <= resp.status < 600:
                    log.debug(
                        "%s status=%d (attempt %d/%d) — backing off %.1fs",
                        label, resp.status, attempt, MAX_RETRIES, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                # Other 4xx — log and bail (don't retry, won't get better)
                body_snippet = (await resp.text())[:200]
                log.warning(
                    "%s status=%d body=%s — giving up", label, resp.status, body_snippet
                )
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug(
                "%s transport error (attempt %d/%d): %s — backing off %.1fs",
                label, attempt, MAX_RETRIES, exc, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
    log.warning("%s failed after %d retries: %s", label, MAX_RETRIES, url)
    return None


async def fetch_project(
    session: aiohttp.ClientSession, repo: str
) -> dict | None:
    """GET deps.dev project data for `github.com/{repo}`. None if not indexed."""
    project_id = quote(f"github.com/{repo}", safe="")
    url = f"{DEPSDEV_BASE}/v3/projects/{project_id}"
    return await _request_json(session, url, label=f"depsdev-project[{repo}]")


async def fetch_default_version(
    session: aiohttp.ClientSession, system: str, package: str
) -> str | None:
    """Find the `isDefault=True` version for a package. None if missing.

    deps.dev's `/dependents` endpoint requires a specific version — we use
    the registry-default (latest stable) as the canonical anchor.
    """
    pkg_enc = quote(package, safe="")
    url = f"{DEPSDEV_BASE}/v3alpha/systems/{system}/packages/{pkg_enc}"
    data = await _request_json(
        session, url, label=f"depsdev-pkg[{system}/{package}]"
    )
    if not data:
        return None
    for v in data.get("versions") or []:
        if v.get("isDefault"):
            return (v.get("versionKey") or {}).get("version")
    return None


async def fetch_dependent_count(
    session: aiohttp.ClientSession, system: str, package: str
) -> int | None:
    """Return total `dependentCount` for the package's default version.

    Returns None if either the package or its default-version dependents
    endpoint is missing — caller treats that as "no data for this package".
    """
    version = await fetch_default_version(session, system, package)
    if not version:
        return None
    pkg_enc = quote(package, safe="")
    ver_enc = quote(version, safe="")
    url = (
        f"{DEPSDEV_BASE}/v3alpha/systems/{system}/packages/{pkg_enc}"
        f"/versions/{ver_enc}:dependents"
    )
    data = await _request_json(
        session, url, label=f"depsdev-deps[{system}/{package}@{version}]"
    )
    if not data:
        return None
    n = data.get("dependentCount")
    return int(n) if isinstance(n, (int, float)) else None


class BestPracticesLimiter:
    """Global throttle for bestpractices.dev — semaphore + min-interval gate.

    The service rate-limits hard (~5 req/s global). One inflight at a time
    plus a 250ms gap keeps us under the threshold across all workers.
    """

    def __init__(self, max_inflight: int, gap_s: float) -> None:
        self._sem = asyncio.Semaphore(max_inflight)
        self._gap_s = gap_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        await self._sem.acquire()
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._gap_s:
                await asyncio.sleep(self._gap_s - gap)
            self._last = time.monotonic()

    def release(self) -> None:
        self._sem.release()


async def fetch_bestpractices(
    session: aiohttp.ClientSession,
    repo: str,
    limiter: BestPracticesLimiter,
) -> tuple[str, str]:
    """Return (badge_level, tiered_percentage) from CII Best-Practices.

    badge_level is one of: "" (not enrolled), "in_progress", "passing",
    "silver", "gold". tiered_percentage is a string-stringified int 0..300
    (or "" if not enrolled). We pick the first matching project — projects
    very rarely have multiple Best-Practices entries.

    Goes through `limiter` so all workers share one ≤4 req/s pipe to the host.
    """
    await limiter.acquire()
    try:
        # URL-encode the value to avoid the server's 302 redirect (it
        # rewrites raw `https://...` into the percent-encoded form), which
        # doubles our request count and worsens 429s. We pass `encoded=True`
        # to yarl so aiohttp doesn't normalize the percent-encoding back.
        target = quote(f"https://github.com/{repo}", safe="")
        url = URL(f"{BESTPRACTICES_URL}?url={target}", encoded=True)
        data = await _request_json(
            session, url, label=f"bestpractices[{repo}]"
        )
    finally:
        limiter.release()
    if not data:
        return "", ""
    if not isinstance(data, list) or not data:
        return "", ""
    p = data[0]
    badge = (p.get("badge_level") or "").strip()
    pct = p.get("tiered_percentage")
    pct_str = str(pct) if pct is not None else ""
    return badge, pct_str


# ── per-repo orchestration ───────────────────────────────────────────────────


def _extract_scorecard(project: dict) -> tuple[str, str]:
    """Return (overall_score, date) from a deps.dev project payload.

    Both come back as strings — score is a float "0.0".."10.0" rendered with
    Python's default repr (so "7.5" not "7.50"). Empty when scorecard
    missing or fields absent.
    """
    s = project.get("scorecard") or {}
    overall = s.get("overallScore")
    date = (s.get("date") or "").strip()
    # overallScore can legitimately be 0.0 — only filter out None
    overall_str = "" if overall is None else str(overall)
    # Trim time portion if it's midnight (always is for deps.dev scorecards)
    if "T" in date:
        date = date.split("T", 1)[0]
    return overall_str, date


async def _process_repo(
    repo: str,
    packages: list[tuple[str, str]],
    session: aiohttp.ClientSession,
    repo_id_map: dict[str, str],
    last_request_ref: list[float],
    throttle_lock: asyncio.Lock,
    bp_limiter: BestPracticesLimiter,
) -> dict[str, str]:
    """Fetch project info + dependents (per package) + best-practices badge.

    Throttled to ≤1 request per WORKER_MIN_INTERVAL_S per worker for deps.dev.
    Best-Practices uses a separate global limiter (passed in) since the
    service rate-limits more aggressively.
    """

    async def _throttled_get(coro_factory):
        async with throttle_lock:
            gap = time.monotonic() - last_request_ref[0]
            if gap < WORKER_MIN_INTERVAL_S:
                await asyncio.sleep(WORKER_MIN_INTERVAL_S - gap)
            last_request_ref[0] = time.monotonic()
        return await coro_factory()

    # 1. Project (scorecard, license, open issues)
    project = await _throttled_get(lambda: fetch_project(session, repo))

    if project is None:
        scorecard_overall, scorecard_date = "", ""
        depsdev_license, depsdev_open_issues = "", ""
    else:
        scorecard_overall, scorecard_date = _extract_scorecard(project)
        depsdev_license = (project.get("license") or "").strip()
        oi = project.get("openIssuesCount")
        depsdev_open_issues = str(oi) if isinstance(oi, (int, float)) else ""

    # 2. Dependent count — sum across mapped packages in supported systems.
    # Skip Debian (cpp) since deps.dev doesn't index it. If the repo has only
    # Debian packages, dependent_count stays "" (legitimate "no data").
    dep_total: int | None = None
    for eco, pkg in packages:
        system = ECO_TO_DEPSDEV_SYSTEM.get(eco)
        if not system:
            continue
        n = await _throttled_get(
            lambda system=system, pkg=pkg: fetch_dependent_count(session, system, pkg)
        )
        if n is None:
            continue
        dep_total = (dep_total or 0) + n

    dep_total_str = str(dep_total) if dep_total is not None else ""

    # 3. Best-Practices badge (separate API, separate global limiter)
    badge, pct = await fetch_bestpractices(session, repo, bp_limiter)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return {
        "repo": repo,
        "repo_id": repo_id_map.get(repo, ""),
        "depsdev_scorecard_overall": scorecard_overall,
        "depsdev_scorecard_date": scorecard_date,
        "depsdev_dependent_count": dep_total_str,
        "depsdev_license": depsdev_license,
        "depsdev_open_issues": depsdev_open_issues,
        "bestpractices_badge_id": badge,
        "bestpractices_tiered_percentage": pct,
        "fetched_at": now,
    }


# ── CSV I/O ──────────────────────────────────────────────────────────────────


def _load_existing() -> dict[str, dict[str, str]]:
    """Read existing repos.csv keyed by repo. Empty dict if file missing."""
    out: dict[str, dict[str, str]] = {}
    if not OUTPUT_FILE.exists():
        return out
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in FIELDS:
                row.setdefault(k, "")
            out[row["repo"]] = row
    return out


def _write(rows: dict[str, dict[str, str]]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])
    tmp.replace(OUTPUT_FILE)


def _is_fresh(row: dict[str, str], ttl_days: int) -> bool:
    """True iff the row was fetched within ttl_days AND has a valid timestamp.

    Unlike the OSV fetcher, we don't gate freshness on a specific value being
    populated — deps.dev may legitimately have no data for a given repo
    (e.g. badge not enrolled), so an empty value isn't necessarily a failure.
    Instead we just gate on `fetched_at` being recent.
    """
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)
    return dt >= cutoff


# ── batch ────────────────────────────────────────────────────────────────────


async def _worker(
    name: str,
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    results: dict[str, dict[str, str]],
    repo_id_map: dict[str, str],
    repo_pkg_map: dict[str, list[tuple[str, str]]],
    bp_limiter: BestPracticesLimiter,
    progress: Progress,
    task_id: int,
) -> None:
    """Pull repos off the queue, fetch deps.dev + best-practices, write to results."""
    last_request_ref = [0.0]
    throttle_lock = asyncio.Lock()
    while True:
        try:
            repo = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        packages = repo_pkg_map.get(repo, [])
        try:
            row = await _process_repo(
                repo, packages, session, repo_id_map,
                last_request_ref, throttle_lock, bp_limiter,
            )
        except Exception as exc:
            log.exception("worker %s crashed on %s: %s", name, repo, exc)
            row = {
                "repo": repo,
                "repo_id": repo_id_map.get(repo, ""),
                **{c: "" for c in FIELDS if c not in ("repo", "repo_id", "fetched_at")},
                "fetched_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds"),
            }
        results[repo] = row
        progress.advance(task_id)
        queue.task_done()


async def _periodic_flush(
    existing: dict[str, dict[str, str]],
    new_results: dict[str, dict[str, str]],
    *,
    interval_s: float,
) -> None:
    """Merge & write every interval_s seconds while workers run."""
    try:
        while True:
            await asyncio.sleep(interval_s)
            merged = {**existing, **new_results}
            _write(merged)
    except asyncio.CancelledError:
        return


async def batch_fetch(
    repos_with_ids: list[tuple[str, str]],
    repo_pkg_map: dict[str, list[tuple[str, str]]],
    *,
    force: bool,
    limit: int | None,
    seed: int,
    ttl_days: int,
    concurrency: int,
) -> dict[str, dict[str, str]]:
    """Fetch deps.dev + best-practices data, write CSV, return final rows."""
    existing = _load_existing()
    repo_id_map = {r: rid for r, rid in repos_with_ids}

    if force:
        fresh: set[str] = set()
    else:
        fresh = {r for r, row in existing.items() if _is_fresh(row, ttl_days)}

    all_repos = [r for r, _ in repos_with_ids]
    candidates = [r for r in all_repos if r not in fresh]
    fresh_skipped = len(all_repos) - len(candidates)
    if limit and limit < len(candidates):
        rng = random.Random(seed)
        to_fetch = rng.sample(candidates, limit)
        limit_skipped = len(candidates) - limit
    else:
        to_fetch = candidates
        limit_skipped = 0

    console.print(
        f"[bold]depsdev[/bold]: {len(all_repos)} eligible · "
        f"{len(to_fetch)} to fetch · "
        f"{fresh_skipped} fresh-skipped · {limit_skipped} limit-skipped"
    )
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return existing

    queue: asyncio.Queue[str] = asyncio.Queue()
    for repo in to_fetch:
        queue.put_nowait(repo)

    new_results: dict[str, dict[str, str]] = {}
    t_start = time.monotonic()

    headers = {
        "Accept": "application/json",
        "User-Agent": "osendowment-depsdev-fetcher/1.0",
    }
    bp_limiter = BestPracticesLimiter(
        BESTPRACTICES_MAX_INFLIGHT, BESTPRACTICES_GAP_S
    )
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("depsdev", total=len(to_fetch))
            workers = [
                asyncio.create_task(
                    _worker(
                        f"w{i}", queue, session, new_results,
                        repo_id_map, repo_pkg_map, bp_limiter,
                        progress, task_id,
                    )
                )
                for i in range(concurrency)
            ]
            flush_task = asyncio.create_task(
                _periodic_flush(existing, new_results, interval_s=15.0)
            )
            await asyncio.gather(*workers)
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

    existing.update(new_results)
    _write(existing)

    elapsed = time.monotonic() - t_start
    rate = len(new_results) / elapsed if elapsed > 0 else 0
    console.print(
        f"[green]done[/green] in {elapsed:.1f}s · "
        f"{len(new_results)} fetched ({rate:.1f}/s) → {OUTPUT_FILE}"
    )
    return existing


# ── summary ──────────────────────────────────────────────────────────────────


def print_summary(
    rows: dict[str, dict[str, str]],
    *,
    processed: list[str],
) -> None:
    """Print: coverage, top-10 by dependent_count, badge distribution."""
    relevant = [rows[r] for r in processed if r in rows]
    total = len(relevant)

    overview = Table(title="deps.dev fetch — coverage", show_header=False)
    overview.add_column("metric", style="dim")
    overview.add_column("value", justify="right", style="bold")
    overview.add_row("Repos processed", str(total))
    for col in (
        "depsdev_scorecard_overall",
        "depsdev_dependent_count",
        "depsdev_license",
        "depsdev_open_issues",
        "bestpractices_badge_id",
    ):
        n = sum(1 for r in relevant if (r.get(col) or "").strip())
        pct = 100 * n / total if total else 0
        overview.add_row(col, f"{n:,} ({pct:.1f}%)")
    console.print(overview)

    # Top-10 by dependent count
    by_dep: list[tuple[str, int]] = []
    for r in relevant:
        v = (r.get("depsdev_dependent_count") or "").strip()
        if not v:
            continue
        try:
            by_dep.append((r["repo"], int(v)))
        except ValueError:
            pass
    by_dep.sort(key=lambda t: (-t[1], t[0]))
    if by_dep:
        top = Table(title="Top 10 repos by deps.dev dependent_count")
        top.add_column("repo", style="cyan")
        top.add_column("dependents", justify="right", style="bold")
        for repo, n in by_dep[:10]:
            top.add_row(repo, f"{n:,}")
        console.print(top)

    # Badge distribution
    badges = Counter(
        ((r.get("bestpractices_badge_id") or "").strip() or "(none)")
        for r in relevant
    )
    badge_table = Table(title="Best-Practices badge distribution")
    badge_table.add_column("badge", style="cyan")
    badge_table.add_column("repos", justify="right")
    for badge in sorted(badges, key=lambda b: -badges[b]):
        badge_table.add_row(badge, str(badges[badge]))
    console.print(badge_table)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _safe_fromiso(ts: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=0,
                   help="Process only N random eligible repos (0 = all)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for --limit sampling (default: 42)")
    p.add_argument("--ttl-days", type=int, default=TTL_DAYS_DEFAULT,
                   help=f"Skip repos fetched within N days (default: {TTL_DAYS_DEFAULT})")
    p.add_argument("--concurrency", type=int, default=20,
                   help="Max concurrent HTTP workers (default: 20)")
    p.add_argument("--force", action="store_true",
                   help="Ignore TTL — refetch every eligible repo")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG-level logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    eligible = load_eligible_repos()
    repos_with_ids: list[tuple[str, str]] = sorted(
        {(e.repo, e.repo_id) for e in eligible if e.repo}
    )
    repo_pkg_map = load_repo_package_mapping()

    n_with_supported_pkg = sum(
        1 for r, _ in repos_with_ids
        if any(ECO_TO_DEPSDEV_SYSTEM.get(eco) for eco, _ in repo_pkg_map.get(r, []))
    )

    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    console.rule("[bold]deps.dev fetcher[/bold]")
    banner = Table(show_header=False, box=None, padding=(0, 1))
    banner.add_column(style="dim")
    banner.add_column()
    banner.add_row("script", "src.depsdev.fetch")
    banner.add_row("eligible repos", str(len(repos_with_ids)))
    banner.add_row(
        "with deps.dev-indexed pkgs",
        f"{n_with_supported_pkg} (npm/pypi/cargo)",
    )
    banner.add_row("output", str(OUTPUT_FILE))
    banner.add_row("concurrency", str(args.concurrency))
    banner.add_row("ttl-days", str(args.ttl_days))
    banner.add_row("limit", str(args.limit) if args.limit else "(all)")
    banner.add_row("seed", str(args.seed))
    banner.add_row("force", "yes" if args.force else "no")
    banner.add_row("started", started)
    console.print(banner)

    final_rows = asyncio.run(batch_fetch(
        repos_with_ids,
        repo_pkg_map,
        force=args.force,
        limit=args.limit or None,
        seed=args.seed,
        ttl_days=args.ttl_days,
        concurrency=args.concurrency,
    ))

    # Summary focuses on repos we actually touched this run.
    now = datetime.datetime.now(datetime.timezone.utc)
    recent_cutoff = now - datetime.timedelta(minutes=10)
    processed = [
        r for r, row in final_rows.items()
        if (ts := row.get("fetched_at"))
        and _safe_fromiso(ts) >= recent_cutoff
    ]
    print_summary(final_rows, processed=processed)


if __name__ == "__main__":
    main()
