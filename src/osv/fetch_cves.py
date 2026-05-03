"""Fetch CVE counts per eligible repo from OSV.dev (2021–2025).

OSV.dev does not index `pkg:github/*` purls — they return zero results. So
instead, we look up each repo by the (ecosystem, package_name) tuples that
map to it across our per-ecosystem `results.csv` files:

    data/npm/results.csv     → ecosystem `npm`
    data/pypi/results.csv    → ecosystem `PyPI`
    data/crates/results.csv  → ecosystem `crates.io`
    data/cpp/results.csv     → ecosystem `Debian` (binary package name)

C/C++ packages are queried against OSV's `Debian` ecosystem — a query
without a release suffix (e.g. `Debian` vs `Debian:13`) aggregates across
all Debian releases and returns the most CVEs. Most cpp packages in our
set (glibc, curl, openssl, ffmpeg, …) have a Debian binary of the same
name, so this gives broad coverage. Cpp packages with no Debian binary
get `cve_count_5y=0` (legitimate zero — not a failure).

For each (ecosystem, package), we POST to https://api.osv.dev/v1/query:

    {"package": {"name": "<package>", "ecosystem": "<eco>"}}

Vulns from all packages mapped to a repo are aggregated, then deduped:
identity = `{id} ∪ aliases[]`, canonical key = lex-smallest member.
Then we filter to `published` year ∈ 2021–2025 inclusive.

Output: data/osv/cves.csv with columns
    repo, repo_id, cve_count_5y, cve_ids_5y, packages_queried, fetched_at

`packages_queried` is a comma-separated list of `<eco>:<pkg>` strings —
makes traceability easy. Failed lookups get `cve_count_5y=""` so a future
run will retry. Repos with no package mapping (cpp / system-tools orphans)
are skipped entirely.

Usage:
    uv run python -m src.osv.fetch_cves                        # full run
    uv run python -m src.osv.fetch_cves --limit 10 -v          # quick test
    uv run python -m src.osv.fetch_cves --force                # ignore TTL
    uv run python -m src.osv.fetch_cves --concurrency 10       # be politer
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

from src.pipeline.repos import load_eligible_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "osv" / "cves.csv"
FIELDS = [
    "repo",
    "repo_id",
    "cve_count_5y",
    "cve_ids_5y",
    "packages_queried",
    "fetched_at",
]

# Per-ecosystem `results.csv` → OSV ecosystem name. The OSV ecosystem
# strings are case-sensitive and follow the OSV schema spec.
# `Debian` (no release suffix) aggregates vulns across all Debian
# releases — gives broader coverage than e.g. `Debian:13` alone.
ECOSYSTEM_FILES: list[tuple[Path, str]] = [
    (DATA_DIR / "npm" / "results.csv", "npm"),
    (DATA_DIR / "pypi" / "results.csv", "PyPI"),
    (DATA_DIR / "crates" / "results.csv", "crates.io"),
    (DATA_DIR / "cpp" / "results.csv", "Debian"),
]

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
TTL_DAYS_DEFAULT = 30
YEAR_MIN = 2021
YEAR_MAX = 2025

REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3
WORKER_MIN_INTERVAL_S = 1.0  # ~1 req/sec per worker → polite throttle


# ── repo → packages mapping ──────────────────────────────────────────────────


def load_repo_package_mapping() -> dict[str, list[tuple[str, str]]]:
    """Build {repo_lowercased → [(ecosystem, package_name), ...]}.

    Sources: every per-ecosystem `results.csv` listed in `ECOSYSTEM_FILES`.
    A repo can map to many packages across multiple ecosystems (e.g. a
    monorepo publishing several npm packages, or a project with both an
    npm and a PyPI binding). Entries are deduped within (eco, pkg) pairs
    but ordering is preserved — we list npm packages first, then PyPI,
    then crates, in the order they appear in the CSVs.
    """
    mapping: dict[str, list[tuple[str, str]]] = {}
    seen: dict[str, set[tuple[str, str]]] = {}
    for path, eco in ECOSYSTEM_FILES:
        if not path.exists():
            log.warning("missing ecosystem file: %s — skipping", path)
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ghr = (row.get("github_repo") or "").strip().lower()
                pkg = (row.get("package") or "").strip()
                if not ghr or not pkg:
                    continue
                key = (eco, pkg)
                bucket = seen.setdefault(ghr, set())
                if key in bucket:
                    continue
                bucket.add(key)
                mapping.setdefault(ghr, []).append(key)
    return mapping


# ── OSV fetch ────────────────────────────────────────────────────────────────


async def fetch_package_vulns(
    session: aiohttp.ClientSession, ecosystem: str, package: str
) -> list[dict] | None:
    """POST OSV query for one (ecosystem, package) → vulns list, or None on failure.

    Retries 429/5xx with exponential backoff (1s, 2s, 4s, 8s) up to MAX_RETRIES.
    Returns [] when OSV legitimately reports zero vulns. Returns None when we
    couldn't get an answer — the caller decides what to do (we treat any
    None among a repo's packages as "this repo's lookup is incomplete").
    """
    payload = {"package": {"name": package, "ecosystem": ecosystem}}
    backoff = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                OSV_QUERY_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("vulns") or []
                if resp.status == 429 or 500 <= resp.status < 600:
                    log.debug(
                        "osv %s/%s status=%d (attempt %d/%d) — backing off %.1fs",
                        ecosystem, package, resp.status, attempt, MAX_RETRIES, backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                # 4xx other than 429: don't retry, log and bail
                body_snippet = (await resp.text())[:200]
                log.warning(
                    "osv %s/%s status=%d body=%s — giving up",
                    ecosystem, package, resp.status, body_snippet,
                )
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug(
                "osv %s/%s transport error (attempt %d/%d): %s — backing off %.1fs",
                ecosystem, package, attempt, MAX_RETRIES, exc, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
    log.warning("osv %s/%s failed after %d retries", ecosystem, package, MAX_RETRIES)
    return None


# ── parsing ──────────────────────────────────────────────────────────────────


def _published_year(vuln: dict) -> int | None:
    """Extract year from `published` (ISO 8601). None if missing/unparseable."""
    raw = (vuln.get("published") or "").strip()
    if not raw:
        return None
    try:
        # OSV uses RFC 3339 / ISO 8601. Python 3.11+ handles trailing Z directly.
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).year
    except ValueError:
        # Fallback: just trust the leading 4 chars if they look like a year
        head = raw[:4]
        return int(head) if head.isdigit() else None


def dedupe_in_window(vulns: list[dict]) -> list[str]:
    """Return canonical dedup keys for vulns published in [YEAR_MIN, YEAR_MAX].

    Identity = {id} ∪ aliases. Canonical key = lexicographically smallest member.
    Two vulns sharing any identifier collapse to one entry. The list is sorted
    so the CSV output is stable.
    """
    # parent[x] = canonical representative of x
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Always pick the lexicographically smaller as root
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    # Track which vulns are in window, plus their identifier sets.
    in_window: list[set[str]] = []
    for v in vulns:
        year = _published_year(v)
        if year is None or year < YEAR_MIN or year > YEAR_MAX:
            continue
        ids: set[str] = set()
        if v.get("id"):
            ids.add(v["id"])
        for alias in v.get("aliases") or []:
            if alias:
                ids.add(alias)
        if not ids:
            continue
        in_window.append(ids)
        for i in ids:
            parent.setdefault(i, i)

    # Union identifiers within each vuln so they share a root
    for ids in in_window:
        ids_list = list(ids)
        for i in ids_list[1:]:
            union(ids_list[0], i)

    # Each in-window vuln contributes its root; dedupe across vulns
    canonical_keys: set[str] = set()
    for ids in in_window:
        canonical_keys.add(find(next(iter(ids))))

    return sorted(canonical_keys)


# ── CSV I/O ──────────────────────────────────────────────────────────────────


def _load_existing() -> dict[str, dict[str, str]]:
    """Read cves.csv keyed by repo. Empty dict if file missing.

    Old rows missing `packages_queried` get an empty value — they'll be
    overwritten on next fetch.
    """
    out: dict[str, dict[str, str]] = {}
    if not OUTPUT_FILE.exists():
        return out
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Forward-compat: ensure all fields exist even if old CSV has fewer.
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
    """True iff the row's `fetched_at` is within ttl_days AND has a count.

    Empty `cve_count_5y` means a previous fetch failed — never treat as fresh,
    so a re-run will retry it.
    """
    if not (row.get("cve_count_5y") or "").strip():
        return False
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


async def _process_repo(
    repo: str,
    packages: list[tuple[str, str]],
    session: aiohttp.ClientSession,
    repo_id_map: dict[str, str],
    last_request_ref: list[float],
    throttle_lock: asyncio.Lock,
) -> dict[str, str]:
    """Fetch vulns for every (eco, pkg) mapped to `repo`, aggregate & dedupe.

    All package fetches are issued sequentially under a single shared
    throttle so this worker stays at ≤1 req/sec. If any package lookup
    fails (returns None), we still aggregate the rest but mark the row
    with empty `cve_count_5y` so it gets retried next run.
    """
    all_vulns: list[dict] = []
    any_failed = False
    for eco, pkg in packages:
        # Per-worker throttle: ensure ≥WORKER_MIN_INTERVAL_S between requests
        async with throttle_lock:
            gap = time.monotonic() - last_request_ref[0]
            if gap < WORKER_MIN_INTERVAL_S:
                await asyncio.sleep(WORKER_MIN_INTERVAL_S - gap)
            last_request_ref[0] = time.monotonic()
        try:
            vulns = await fetch_package_vulns(session, eco, pkg)
        except Exception as exc:  # last-ditch
            log.exception("crashed on %s %s/%s: %s", repo, eco, pkg, exc)
            vulns = None
        if vulns is None:
            any_failed = True
        else:
            all_vulns.extend(vulns)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    pkgs_str = ",".join(f"{eco}:{pkg}" for eco, pkg in packages)

    if any_failed:
        return {
            "repo": repo,
            "repo_id": repo_id_map.get(repo, ""),
            "cve_count_5y": "",  # empty → retry next run
            "cve_ids_5y": "",
            "packages_queried": pkgs_str,
            "fetched_at": now,
        }
    keys = dedupe_in_window(all_vulns)
    return {
        "repo": repo,
        "repo_id": repo_id_map.get(repo, ""),
        "cve_count_5y": str(len(keys)),
        "cve_ids_5y": ",".join(keys),
        "packages_queried": pkgs_str,
        "fetched_at": now,
    }


async def _worker(
    name: str,
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    results: dict[str, dict[str, str]],
    repo_id_map: dict[str, str],
    repo_pkg_map: dict[str, list[tuple[str, str]]],
    progress: Progress,
    task_id: int,
) -> None:
    """Pull repos off the queue, fetch all their packages, write to results."""
    last_request_ref = [0.0]  # mutable cell for the throttle gate
    throttle_lock = asyncio.Lock()
    while True:
        try:
            repo = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        packages = repo_pkg_map.get(repo, [])
        if not packages:
            # Defensive: should never reach here — we filter out unmapped
            # repos before queueing. But if it does, write a skip row.
            results[repo] = {
                "repo": repo,
                "repo_id": repo_id_map.get(repo, ""),
                "cve_count_5y": "",
                "cve_ids_5y": "",
                "packages_queried": "",
                "fetched_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds"),
            }
            progress.advance(task_id)
            queue.task_done()
            continue

        try:
            row = await _process_repo(
                repo, packages, session, repo_id_map,
                last_request_ref, throttle_lock,
            )
        except Exception as exc:
            log.exception("worker %s crashed on %s: %s", name, repo, exc)
            row = {
                "repo": repo,
                "repo_id": repo_id_map.get(repo, ""),
                "cve_count_5y": "",
                "cve_ids_5y": "",
                "packages_queried": ",".join(
                    f"{eco}:{pkg}" for eco, pkg in packages
                ),
                "fetched_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds"),
            }
        results[repo] = row
        progress.advance(task_id)
        queue.task_done()


async def batch_fetch(
    repos_with_ids: list[tuple[str, str]],
    repo_pkg_map: dict[str, list[tuple[str, str]]],
    *,
    force: bool,
    limit: int | None,
    ttl_days: int,
    concurrency: int,
) -> tuple[dict[str, dict[str, str]], int]:
    """Fetch and write CVE counts. Returns (final_rows, n_unmapped_skipped)."""
    existing = _load_existing()
    repo_id_map = {r: rid for r, rid in repos_with_ids}

    if force:
        fresh: set[str] = set()
    else:
        fresh = {r for r, row in existing.items() if _is_fresh(row, ttl_days)}

    all_repos = [r for r, _ in repos_with_ids]
    mapped_repos = [r for r in all_repos if r in repo_pkg_map]
    unmapped_skipped = len(all_repos) - len(mapped_repos)

    candidates = [r for r in mapped_repos if r not in fresh]
    fresh_skipped = len(mapped_repos) - len(candidates)
    if limit and limit < len(candidates):
        to_fetch = random.sample(candidates, limit)
        limit_skipped = len(candidates) - limit
    else:
        to_fetch = candidates
        limit_skipped = 0

    console.print(
        f"[bold]osv-cves[/bold]: {len(all_repos)} eligible · "
        f"{unmapped_skipped} no-package-mapping (skipped) · "
        f"{len(mapped_repos)} mapped · {len(to_fetch)} to fetch · "
        f"{fresh_skipped} fresh-skipped · {limit_skipped} limit-skipped"
    )
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return existing, unmapped_skipped

    queue: asyncio.Queue[str] = asyncio.Queue()
    for repo in to_fetch:
        queue.put_nowait(repo)

    new_results: dict[str, dict[str, str]] = {}
    t_start = time.monotonic()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "osendowment-osv-fetcher/1.0",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("osv", total=len(to_fetch))
            workers = [
                asyncio.create_task(
                    _worker(
                        f"w{i}", queue, session, new_results,
                        repo_id_map, repo_pkg_map, progress, task_id,
                    )
                )
                for i in range(concurrency)
            ]
            # Periodically flush partial results to disk so an aborted run
            # doesn't lose work.
            flush_task = asyncio.create_task(
                _periodic_flush(existing, new_results, interval_s=15.0)
            )
            await asyncio.gather(*workers)
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

    # Merge new results into existing and write final.
    existing.update(new_results)
    _write(existing)

    elapsed = time.monotonic() - t_start
    console.print(
        f"[green]done[/green] in {elapsed:.1f}s · "
        f"{len(new_results)} fetched → {OUTPUT_FILE}"
    )
    return existing, unmapped_skipped


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


# ── summary ──────────────────────────────────────────────────────────────────


def _bucket(n: int) -> str:
    if n == 0:
        return "0"
    if n <= 2:
        return "1-2"
    if n <= 10:
        return "3-10"
    if n <= 50:
        return "11-50"
    return "50+"


def print_summary(
    rows: dict[str, dict[str, str]],
    *,
    processed: list[str],
    unmapped_skipped: int,
) -> None:
    """Print: top-10 by CVE count, distribution, totals."""
    relevant = [rows[r] for r in processed if r in rows]
    counts = []
    failures = 0
    for r in relevant:
        c = (r.get("cve_count_5y") or "").strip()
        if not c:
            failures += 1
            continue
        try:
            counts.append((r["repo"], int(c)))
        except ValueError:
            failures += 1

    counts.sort(key=lambda t: (-t[1], t[0]))
    with_cve = sum(1 for _, n in counts if n >= 1)

    overview = Table(title="OSV CVE fetch — overview", show_header=False)
    overview.add_column("metric", style="dim")
    overview.add_column("value", justify="right", style="bold")
    overview.add_row("Repos processed (this run)", str(len(processed)))
    overview.add_row("Successful lookups", str(len(counts)))
    overview.add_row("Failed lookups (count='')", str(failures))
    overview.add_row("Repos with ≥1 CVE", str(with_cve))
    overview.add_row("No package mapping (skipped)", str(unmapped_skipped))
    console.print(overview)

    dist = Counter(_bucket(n) for _, n in counts)
    dist_table = Table(title="CVE count distribution (2021–2025)")
    dist_table.add_column("bucket", style="cyan")
    dist_table.add_column("repos", justify="right")
    for bucket in ("0", "1-2", "3-10", "11-50", "50+"):
        dist_table.add_row(bucket, str(dist.get(bucket, 0)))
    console.print(dist_table)

    if counts:
        top = Table(title="Top 10 repos by CVE count (2021–2025)")
        top.add_column("repo", style="cyan")
        top.add_column("CVEs", justify="right", style="bold")
        for repo, n in counts[:10]:
            top.add_row(repo, str(n))
        console.print(top)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None,
                   help="Process only N random eligible repos (testing)")
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

    n_mapped = sum(1 for r, _ in repos_with_ids if r in repo_pkg_map)
    n_total_pkgs = sum(
        len(repo_pkg_map[r]) for r, _ in repos_with_ids if r in repo_pkg_map
    )

    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    console.rule("[bold]OSV CVE fetcher[/bold]")
    banner = Table(show_header=False, box=None, padding=(0, 1))
    banner.add_column(style="dim")
    banner.add_column()
    banner.add_row("script", "src.osv.fetch_cves")
    banner.add_row("eligible repos", str(len(repos_with_ids)))
    banner.add_row("mapped to packages", f"{n_mapped} (of {len(repos_with_ids)})")
    banner.add_row("total package queries", str(n_total_pkgs))
    banner.add_row("ecosystems", ", ".join(eco for _, eco in ECOSYSTEM_FILES))
    banner.add_row("output", str(OUTPUT_FILE))
    banner.add_row("concurrency", str(args.concurrency))
    banner.add_row("ttl-days", str(args.ttl_days))
    banner.add_row("limit", str(args.limit) if args.limit else "(all)")
    banner.add_row("force", "yes" if args.force else "no")
    banner.add_row("year window", f"{YEAR_MIN}–{YEAR_MAX}")
    banner.add_row("started", started)
    console.print(banner)

    final_rows, unmapped_skipped = asyncio.run(batch_fetch(
        repos_with_ids,
        repo_pkg_map,
        force=args.force,
        limit=args.limit,
        ttl_days=args.ttl_days,
        concurrency=args.concurrency,
    ))

    # For the summary, focus on the repos we actually touched this run
    # (existing fresh ones aren't very interesting for a per-run report).
    now = datetime.datetime.now(datetime.timezone.utc)
    recent_cutoff = now - datetime.timedelta(minutes=10)
    processed = [
        r for r, row in final_rows.items()
        if (ts := row.get("fetched_at"))
        and _safe_fromiso(ts) >= recent_cutoff
    ]

    print_summary(
        final_rows,
        processed=processed,
        unmapped_skipped=unmapped_skipped,
    )


def _safe_fromiso(ts: str) -> datetime.datetime:
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


if __name__ == "__main__":
    main()
