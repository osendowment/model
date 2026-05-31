"""Fetch per-CVE rows per risk repo from OSV.dev (2021–2025).

OSV.dev does not index `pkg:github/*` purls — they return zero results. So
instead, we look up each repo by the (ecosystem, package_name) tuples that
map to it across our per-ecosystem `results.csv` files:

    data/sources/npm/results.csv     → ecosystem `npm`
    data/sources/pypi/results.csv    → ecosystem `PyPI`
    data/sources/crates/results.csv  → ecosystem `crates.io`
    data/sources/cpp/results.csv     → ecosystem `Debian` (binary package name)

C/C++ packages are queried against OSV's `Debian` ecosystem — a query
without a release suffix (e.g. `Debian` vs `Debian:13`) aggregates across
all Debian releases and returns the most CVEs. Most cpp packages in our
set (glibc, curl, openssl, ffmpeg, …) have a Debian binary of the same
name, so this gives broad coverage.

For each (ecosystem, package), we POST to https://api.osv.dev/v1/query:

    {"package": {"name": "<package>", "ecosystem": "<eco>"}}

Vulns from all packages mapped to a repo are aggregated, then deduped:
identity = `{id} ∪ aliases[]`, canonical key = lex-smallest member.
Then we filter to `published` year ∈ 2021–2025 inclusive.

Output (long format, one row per (repo × CVE) pair):

    data/sources/osv/cves.csv      cols: repo, repo_id, date, cve

…where `date` is the CVE's `published` date as ISO `YYYY-MM-DD` and
`cve` is the canonical id (lex-smallest of `{id} ∪ aliases`). Repos
with zero CVEs in the 5-year window contribute zero rows.

Sidecar (so downstream can distinguish "scanned, 0 CVEs" from "never queried"):

    data/sources/osv/queried.csv   cols: repo, repo_id, packages_queried, fetched_at

Each successful repo lookup writes one row here. Failed lookups (any
package returned None) do NOT update the sidecar — so a re-run retries.

Re-run behaviour:
- A repo is skipped if its `queried.csv` row's `fetched_at` is within
  `--ttl-days` (default 7) and the lookup wasn't a failure.
- For repos we re-fetch, existing rows in `cves.csv` for that repo are
  replaced wholesale with the fresh result (an upsert keyed by repo).
- Repos we don't touch keep their existing `cves.csv` rows untouched.

Usage:
    uv run python -m src.sources.osv.fetch_cves                        # full run
    uv run python -m src.sources.osv.fetch_cves --limit 20 -v          # quick test
    uv run python -m src.sources.osv.fetch_cves --force                # ignore TTL
    uv run python -m src.sources.osv.fetch_cves --concurrency 8        # be politer
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import random
import time
from collections import Counter, defaultdict
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

from src.common.repos import canonical_repo_map, load_risk_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "osv" / "cves.csv"
QUERIED_FILE = DATA_DIR / "sources" / "osv" / "queried.csv"

# Long-format CVE rows: one (repo × CVE) pair per row.
CVE_FIELDS = ["repo", "repo_id", "date", "cve"]

# Sidecar: repos we successfully scanned (used to distinguish 0-CVE
# from never-queried).
QUERIED_FIELDS = ["repo", "repo_id", "packages_queried", "fetched_at"]

# Per-ecosystem `results.csv` → OSV ecosystem name. The OSV ecosystem
# strings are case-sensitive and follow the OSV schema spec.
# `Debian` (no release suffix) aggregates vulns across all Debian
# releases — gives broader coverage than e.g. `Debian:13` alone.
ECOSYSTEM_FILES: list[tuple[Path, str]] = [
    (DATA_DIR / "sources" / "npm" / "results.csv", "npm"),
    (DATA_DIR / "sources" / "pypi" / "results.csv", "PyPI"),
    (DATA_DIR / "sources" / "crates" / "results.csv", "crates.io"),
    (DATA_DIR / "sources" / "cpp" / "results.csv", "Debian"),
]

OSV_QUERY_URL = "https://api.osv.dev/v1/query"
TTL_DAYS_DEFAULT = 7
YEAR_MIN = 2021
YEAR_MAX = 2025

# Curated override: for a listed repo, REPLACE its OSV package list with these
# (ecosystem, package) rows. Fixes repos the auto-derived mapping gets wrong —
# e.g. python/cpython, which cpp/results.csv maps to the removed Debian
# `python` (Py2) package, missing all interpreter CVEs.
CVE_PKG_OVERRIDES_FILE = DATA_DIR / "risk" / "cve-package-overrides.csv"

REQUEST_TIMEOUT_S = 30
MAX_RETRIES = 3
MAX_PAGES = 50  # OSV returns ≤1000 vulns/page; 50 pages = 50k vuln safety cap
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
    # Resolve renamed repos to their canonical name so the mapping keys
    # match the canonical slugs load_risk_repos iterates over.
    canon = canonical_repo_map()
    for path, eco in ECOSYSTEM_FILES:
        if not path.exists():
            log.warning("missing ecosystem file: %s — skipping", path)
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                raw = (row.get("github_repo") or "").strip().lower()
                ghr = canon.get(raw, raw)
                pkg = (row.get("package") or "").strip()
                if not ghr or not pkg:
                    continue
                key = (eco, pkg)
                bucket = seen.setdefault(ghr, set())
                if key in bucket:
                    continue
                bucket.add(key)
                mapping.setdefault(ghr, []).append(key)

    # Apply curated overrides last — replace the repo's package list wholesale.
    for repo, packages in _load_cve_pkg_overrides().items():
        mapping[repo] = packages
    return mapping


def _load_cve_pkg_overrides() -> dict[str, list[tuple[str, str]]]:
    """Read cve-package-overrides.csv → {repo → [(ecosystem, package), ...]}.

    Each listed repo's auto-derived OSV package list is REPLACED wholesale by
    its override rows (so a repo can map to several packages, e.g. the
    per-version python3.x Debian packages for python/cpython). Empty if missing.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not CVE_PKG_OVERRIDES_FILE.exists():
        return out
    canon = canonical_repo_map()
    with open(CVE_PKG_OVERRIDES_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("github_repo") or "").strip().lower()
            ghr = canon.get(raw, raw)
            eco = (row.get("ecosystem") or "").strip()
            pkg = (row.get("package") or "").strip()
            if not ghr or not eco or not pkg:
                continue
            key = (eco, pkg)
            bucket = out.setdefault(ghr, [])
            if key not in bucket:
                bucket.append(key)
    return out


# ── OSV fetch ────────────────────────────────────────────────────────────────


async def _fetch_vulns_page(
    session: aiohttp.ClientSession, payload: dict, ecosystem: str, package: str
) -> dict | None:
    """POST one OSV query page → parsed JSON dict, or None on failure.

    Retries 429/5xx with exponential backoff (1s, 2s, 4s, 8s) up to MAX_RETRIES.
    """
    backoff = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.post(
                OSV_QUERY_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
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


async def fetch_package_vulns(
    session: aiohttp.ClientSession, ecosystem: str, package: str
) -> list[dict] | None:
    """POST OSV query for one (ecosystem, package) → vulns list, or None on failure.

    OSV `/v1/query` caps each response at 1000 results and returns a
    `next_page_token` when more exist; we follow it so packages with >1000
    vulns (e.g. Debian `linux`) aren't silently truncated. Returns [] when
    OSV legitimately reports zero vulns. Returns None if any page failed.
    """
    all_vulns: list[dict] = []
    page_token: str | None = None
    for _page in range(MAX_PAGES):
        payload: dict = {"package": {"name": package, "ecosystem": ecosystem}}
        if page_token:
            payload["page_token"] = page_token
        data = await _fetch_vulns_page(session, payload, ecosystem, package)
        if data is None:
            return None
        all_vulns.extend(data.get("vulns") or [])
        page_token = data.get("next_page_token")
        if not page_token:
            return all_vulns
        await asyncio.sleep(0.5)  # polite pause between pages
    log.warning(
        "osv %s/%s hit MAX_PAGES=%d — results may be truncated",
        ecosystem, package, MAX_PAGES,
    )
    return all_vulns


# ── parsing ──────────────────────────────────────────────────────────────────


def _parse_published(vuln: dict) -> tuple[int | None, str | None]:
    """Extract (year, iso_date) from `published`. (None, None) if missing.

    `iso_date` is the full date `YYYY-MM-DD` in UTC. `year` is the year
    from that same parse (kept separate so we can filter cheaply).
    """
    raw = (vuln.get("published") or "").strip()
    if not raw:
        return None, None
    try:
        # OSV uses RFC 3339 / ISO 8601. Python 3.11+ handles trailing Z directly.
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Normalise to UTC date for the output column.
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.year, dt.date().isoformat()
    except ValueError:
        # Fallback: trust the leading 10 chars if they look like an ISO date.
        head = raw[:10]
        try:
            d = datetime.date.fromisoformat(head)
            return d.year, d.isoformat()
        except ValueError:
            year_head = raw[:4]
            return (int(year_head), None) if year_head.isdigit() else (None, None)


def dedupe_in_window(vulns: list[dict]) -> list[tuple[str, str]]:
    """Return [(canonical_cve_id, iso_date), ...] for vulns published in window.

    Identity = {id} ∪ aliases. Canonical id = lexicographically smallest
    member. Two vulns sharing any identifier collapse to one entry. The
    canonical date is the earliest `published` date among grouped vulns
    (an alias added later doesn't push the date forward).

    The list is sorted stably by (date, cve) so the CSV output is stable.
    """
    # union-find on identifier strings
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

    # Track which vulns are in window, with their identifier sets and date.
    in_window: list[tuple[set[str], str | None]] = []
    for v in vulns:
        year, iso_date = _parse_published(v)
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
        in_window.append((ids, iso_date))
        for i in ids:
            parent.setdefault(i, i)

    # Union identifiers within each vuln so they share a root
    for ids, _ in in_window:
        ids_list = list(ids)
        for i in ids_list[1:]:
            union(ids_list[0], i)

    # For each canonical root, keep the earliest non-empty date seen.
    by_root: dict[str, str | None] = {}
    for ids, iso_date in in_window:
        root = find(next(iter(ids)))
        prev = by_root.get(root, "__missing__")
        if prev == "__missing__":
            by_root[root] = iso_date
        elif iso_date and (prev is None or iso_date < prev):
            by_root[root] = iso_date

    out = [(cve, date or "") for cve, date in by_root.items()]
    # Stable sort: by (date, cve)
    out.sort(key=lambda t: (t[1], t[0]))
    return out


# ── CSV I/O ──────────────────────────────────────────────────────────────────


def _load_existing_cves() -> dict[str, list[dict[str, str]]]:
    """Read cves.csv grouped by repo. Empty dict if file missing.

    Files written in older formats (with `cve_count_5y, cve_ids_5y, …`)
    are silently dropped — we'll repopulate from a fresh fetch.
    """
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not OUTPUT_FILE.exists():
        return out
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # Old format detection: if the file lacks `cve` and `date` columns,
        # treat it as empty so we re-derive from scratch.
        if not reader.fieldnames or "cve" not in reader.fieldnames or "date" not in reader.fieldnames:
            log.info("cves.csv is in old format — ignoring; will rewrite as long format")
            return defaultdict(list)
        for row in reader:
            repo = (row.get("repo") or "").strip()
            cve = (row.get("cve") or "").strip()
            if not repo or not cve:
                continue
            out[repo].append({
                "repo": repo,
                "repo_id": (row.get("repo_id") or "").strip(),
                "date": (row.get("date") or "").strip(),
                "cve": cve,
            })
    return out


def _load_queried() -> dict[str, dict[str, str]]:
    """Read queried.csv keyed by repo. Empty dict if file missing."""
    out: dict[str, dict[str, str]] = {}
    if not QUERIED_FILE.exists():
        return out
    with open(QUERIED_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in QUERIED_FIELDS:
                row.setdefault(k, "")
            repo = row.get("repo", "").strip()
            if repo:
                out[repo] = {k: row.get(k, "") for k in QUERIED_FIELDS}
    return out


def _write_cves(rows_by_repo: dict[str, list[dict[str, str]]]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CVE_FIELDS, extrasaction="ignore")
        w.writeheader()
        # Stable order: (repo, date, cve)
        for repo in sorted(rows_by_repo):
            for row in sorted(
                rows_by_repo[repo],
                key=lambda r: (r.get("date", ""), r.get("cve", "")),
            ):
                w.writerow(row)
    tmp.replace(OUTPUT_FILE)


def _write_queried(rows: dict[str, dict[str, str]]) -> None:
    QUERIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUERIED_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=QUERIED_FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])
    tmp.replace(QUERIED_FILE)


def _is_fresh(queried_row: dict[str, str], ttl_days: int) -> bool:
    """True iff the queried.csv row's `fetched_at` is within ttl_days.

    Presence in queried.csv already means the previous fetch succeeded
    (failures don't write here), so any timestamp within the TTL counts.
    """
    ts = (queried_row.get("fetched_at") or "").strip()
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
) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    """Fetch vulns for every (eco, pkg) mapped to `repo`, aggregate & dedupe.

    All package fetches are issued sequentially under a single shared
    throttle so this worker stays at ≤1 req/sec.

    Returns `(cve_rows, queried_row)`:
    - `cve_rows`: list of `{repo, repo_id, date, cve}` dicts (one per CVE).
    - `queried_row`: sidecar row, or `None` if any package lookup failed
      (so a re-run retries this repo).
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

    if any_failed:
        return [], None

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    pkgs_str = ",".join(f"{eco}:{pkg}" for eco, pkg in packages)
    repo_id = repo_id_map.get(repo, "")

    cve_rows: list[dict[str, str]] = [
        {"repo": repo, "repo_id": repo_id, "date": date, "cve": cve}
        for cve, date in dedupe_in_window(all_vulns)
    ]
    queried_row = {
        "repo": repo,
        "repo_id": repo_id,
        "packages_queried": pkgs_str,
        "fetched_at": now,
    }
    return cve_rows, queried_row


async def _worker(
    name: str,
    queue: asyncio.Queue,
    session: aiohttp.ClientSession,
    new_cve_rows: dict[str, list[dict[str, str]]],
    new_queried: dict[str, dict[str, str]],
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
            # repos before queueing.
            progress.advance(task_id)
            queue.task_done()
            continue

        try:
            cve_rows, queried_row = await _process_repo(
                repo, packages, session, repo_id_map,
                last_request_ref, throttle_lock,
            )
        except Exception as exc:
            log.exception("worker %s crashed on %s: %s", name, repo, exc)
            cve_rows, queried_row = [], None

        if queried_row is not None:
            # Successful fetch — replace this repo's CVE rows wholesale,
            # and stamp the sidecar.
            new_cve_rows[repo] = cve_rows
            new_queried[repo] = queried_row
        # else: failed — leave existing rows alone, do NOT update sidecar
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
    only_repos: set[str] | None = None,
) -> tuple[
    dict[str, list[dict[str, str]]],  # final cve_rows by repo
    dict[str, dict[str, str]],        # final queried rows
    list[str],                         # list of repos processed this run
    int,                               # n unmapped skipped
]:
    """Fetch and write CVE rows + queried sidecar."""
    existing_cves = _load_existing_cves()
    existing_queried = _load_queried()
    repo_id_map = {r: rid for r, rid in repos_with_ids}

    if force:
        fresh: set[str] = set()
    else:
        fresh = {
            r for r, row in existing_queried.items() if _is_fresh(row, ttl_days)
        }

    all_repos = [r for r, _ in repos_with_ids]
    mapped_repos = [r for r in all_repos if r in repo_pkg_map]
    unmapped_skipped = len(all_repos) - len(mapped_repos)

    if only_repos is not None:
        # Targeted re-fetch: just the named repos (bypasses TTL/fresh).
        candidates = [r for r in mapped_repos if r in only_repos]
        fresh_skipped = 0
    else:
        candidates = [r for r in mapped_repos if r not in fresh]
        fresh_skipped = len(mapped_repos) - len(candidates)
    if limit and limit < len(candidates):
        to_fetch = random.sample(candidates, limit)
        limit_skipped = len(candidates) - limit
    else:
        to_fetch = candidates
        limit_skipped = 0

    console.print(
        f"[bold]osv-cves[/bold]: {len(all_repos)} risk repos · "
        f"{unmapped_skipped} no-package-mapping (skipped) · "
        f"{len(mapped_repos)} mapped · {len(to_fetch)} to fetch · "
        f"{fresh_skipped} fresh-skipped · {limit_skipped} limit-skipped"
    )
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return existing_cves, existing_queried, [], unmapped_skipped

    queue: asyncio.Queue[str] = asyncio.Queue()
    for repo in to_fetch:
        queue.put_nowait(repo)

    new_cve_rows: dict[str, list[dict[str, str]]] = {}
    new_queried: dict[str, dict[str, str]] = {}
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
                        f"w{i}", queue, session,
                        new_cve_rows, new_queried,
                        repo_id_map, repo_pkg_map, progress, task_id,
                    )
                )
                for i in range(concurrency)
            ]
            # Periodically flush partial results to disk so an aborted run
            # doesn't lose work.
            flush_task = asyncio.create_task(
                _periodic_flush(
                    existing_cves, existing_queried,
                    new_cve_rows, new_queried,
                    interval_s=15.0,
                )
            )
            await asyncio.gather(*workers)
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

    # Merge: new_cve_rows REPLACES existing per-repo rows; new_queried
    # overrides existing rows for those repos.
    merged_cves = {**existing_cves, **new_cve_rows}
    merged_queried = {**existing_queried, **new_queried}
    _write_cves(merged_cves)
    _write_queried(merged_queried)

    elapsed = time.monotonic() - t_start
    n_rows_written = sum(len(v) for v in merged_cves.values())
    console.print(
        f"[green]done[/green] in {elapsed:.1f}s · "
        f"{len(new_queried)} repos fetched · "
        f"{sum(len(v) for v in new_cve_rows.values())} new cve rows · "
        f"{n_rows_written} total rows → {OUTPUT_FILE}"
    )
    return merged_cves, merged_queried, list(new_queried.keys()), unmapped_skipped


async def _periodic_flush(
    existing_cves: dict[str, list[dict[str, str]]],
    existing_queried: dict[str, dict[str, str]],
    new_cve_rows: dict[str, list[dict[str, str]]],
    new_queried: dict[str, dict[str, str]],
    *,
    interval_s: float,
) -> None:
    """Merge & write every interval_s seconds while workers run."""
    try:
        while True:
            await asyncio.sleep(interval_s)
            _write_cves({**existing_cves, **new_cve_rows})
            _write_queried({**existing_queried, **new_queried})
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
    cve_rows_by_repo: dict[str, list[dict[str, str]]],
    queried: dict[str, dict[str, str]],
    *,
    processed: list[str],
    unmapped_skipped: int,
) -> None:
    """Print: top-10 by CVE count, distribution, totals."""
    # Per-repo CVE counts, restricted to repos we actually touched this run
    counts = [(r, len(cve_rows_by_repo.get(r, []))) for r in processed]
    counts.sort(key=lambda t: (-t[1], t[0]))
    with_cve = sum(1 for _, n in counts if n >= 1)

    total_rows_all = sum(len(v) for v in cve_rows_by_repo.values())
    unique_cves_all = len({
        row["cve"] for rows in cve_rows_by_repo.values() for row in rows
    })

    overview = Table(title="OSV CVE fetch — overview", show_header=False)
    overview.add_column("metric", style="dim")
    overview.add_column("value", justify="right", style="bold")
    overview.add_row("Repos processed (this run)", str(len(processed)))
    overview.add_row("Successful lookups (this run)", str(len(processed)))
    overview.add_row("Repos with ≥1 CVE (this run)", str(with_cve))
    overview.add_row("No package mapping (skipped)", str(unmapped_skipped))
    overview.add_row("Total CVE rows in file", str(total_rows_all))
    overview.add_row("Unique CVEs in file", str(unique_cves_all))
    overview.add_row("Total queried repos in sidecar", str(len(queried)))
    console.print(overview)

    dist = Counter(_bucket(n) for _, n in counts)
    dist_table = Table(title="CVE count distribution (this run, 2021–2025)")
    dist_table.add_column("bucket", style="cyan")
    dist_table.add_column("repos", justify="right")
    for bucket in ("0", "1-2", "3-10", "11-50", "50+"):
        dist_table.add_row(bucket, str(dist.get(bucket, 0)))
    console.print(dist_table)

    if counts:
        top = Table(title="Top 10 repos by CVE count (this run, 2021–2025)")
        top.add_column("repo", style="cyan")
        top.add_column("CVEs", justify="right", style="bold")
        for repo, n in counts[:10]:
            top.add_row(repo, str(n))
        console.print(top)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None,
                   help="Process only N random risk repos (testing)")
    p.add_argument("--ttl-days", type=int, default=TTL_DAYS_DEFAULT,
                   help=f"Skip repos fetched within N days (default: {TTL_DAYS_DEFAULT})")
    p.add_argument("--concurrency", type=int, default=10,
                   help="Max concurrent HTTP workers (default: 10)")
    p.add_argument("--force", action="store_true",
                   help="Ignore TTL — refetch every risk repo")
    p.add_argument("--repos", nargs="+", default=None, metavar="owner/repo",
                   help="Fetch only these repos (bypasses TTL) — e.g. a targeted "
                        "re-fetch after a mapping/override change")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG-level logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    risk = load_risk_repos()
    repos_with_ids: list[tuple[str, str]] = sorted(
        {(e.repo, e.repo_id) for e in risk if e.repo}
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
    banner.add_row("script", "src.sources.osv.fetch_cves")
    banner.add_row("risk repos", str(len(repos_with_ids)))
    banner.add_row("mapped to packages", f"{n_mapped} (of {len(repos_with_ids)} risk repos)")
    banner.add_row("total package queries", str(n_total_pkgs))
    banner.add_row("ecosystems", ", ".join(eco for _, eco in ECOSYSTEM_FILES))
    banner.add_row("output", str(OUTPUT_FILE))
    banner.add_row("queried sidecar", str(QUERIED_FILE))
    banner.add_row("concurrency", str(args.concurrency))
    banner.add_row("ttl-days", str(args.ttl_days))
    banner.add_row("limit", str(args.limit) if args.limit else "(all)")
    banner.add_row("force", "yes" if args.force else "no")
    banner.add_row("year window", f"{YEAR_MIN}–{YEAR_MAX}")
    banner.add_row("started", started)
    console.print(banner)

    only_repos = {r.strip().lower() for r in args.repos} if args.repos else None
    final_cves, final_queried, processed, unmapped_skipped = asyncio.run(batch_fetch(
        repos_with_ids,
        repo_pkg_map,
        force=args.force,
        limit=args.limit,
        ttl_days=args.ttl_days,
        concurrency=args.concurrency,
        only_repos=only_repos,
    ))

    print_summary(
        final_cves,
        final_queried,
        processed=processed,
        unmapped_skipped=unmapped_skipped,
    )


if __name__ == "__main__":
    main()
