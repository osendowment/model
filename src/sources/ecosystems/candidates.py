#!/usr/bin/env python3
"""ecosyste.ms class-A candidate fetch — full results for every A-class package.

Unlike `packages.py` (which only backfills packages *missing* both a github_repo
and a git URL), this connector fetches ecosyste.ms data for **every class-A
candidate** — the `value_class == "A"` rows in each ecosystem's `results.csv`
(the packages inside the ≤75% cumulative-PageRank head). The point is not to
fill gaps but to obtain an *independent* repository identity for packages we
already have URLs for, so the audit step can compare ecosyste.ms's canonical
repo against ours (`audit_ecosystems.py`).

It shares the raw-JSON cache with `packages.py`
(`data/sources/ecosystems/<eco>/raw/<pkg>.json`, fetched via the same
`_fetch_one`), so a package fetched by either connector is reused by the other.
The consolidated, cross-ecosystem index is written to a single file:

    data/sources/ecosystems/packages.csv

with one row per package:

    ecosystem, package, purl, registry_hit, repository_url, homepage,
    repo_host, repo_full_name, repo_archived, repo_fork, repo_stars,
    last_synced_at, fetched_at

`repo_host` / `repo_full_name` come from ecosyste.ms's `repo_metadata` — its own
rename-resolved canonical repo identity on the hosting platform (github/gitlab/…).

Fetch is **incremental**: a package already present in packages.csv with a
`fetched_at` within the TTL is kept untouched; only missing or stale rows hit
the network (and even then, a still-fresh raw-JSON cache file is reused without
a request). The TTL is 365 days and comes from settings.json
(`fetch_ttl_days`).

Usage:
    uv run python -m src.sources.ecosystems.candidates
    uv run python -m src.sources.ecosystems.candidates --eco npm --limit 20
    uv run python -m src.sources.ecosystems.candidates --concurrency 16
    uv run python -m src.sources.ecosystems.candidates --ttl 0     # force refresh
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import time

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

from src.common.params import fetch_ttl_days

# Reuse the fetch/cache core from the backfill connector — one engine, two views.
from src.sources.ecosystems.packages import (
    DATA_DIR,
    REGISTRY_MAP,
    USER_AGENT,
    _cache_path,
    _fetch_one,
    _is_fresh,
    _now_iso,
    _read_cached,
)

log = logging.getLogger(__name__)
console = Console()

# Default cache TTL — 365 days, from settings.json (`fetch_ttl_days`).
DEFAULT_TTL_DAYS = fetch_ttl_days("sources/ecosystems/candidates")
DEFAULT_CONCURRENCY = 10
# Flush the consolidated packages.csv every this many newly-fetched rows, so an
# interrupted run keeps its progress (and packages.csv reflects it live).
BATCH_FLUSH = 100

OUTPUT_CSV = DATA_DIR / "sources" / "ecosystems" / "packages.csv"
FIELDS = [
    "ecosystem", "package", "purl", "registry_hit", "repository_url", "homepage",
    "repo_host", "repo_full_name", "repo_archived", "repo_fork", "repo_stars",
    "last_synced_at", "fetched_at",
]


# ── candidate selection ───────────────────────────────────────────────────────


def _candidate_packages(eco: str, scope: str) -> list[str]:
    """Candidate packages from this ecosystem's results.csv.

    scope == "a-class": only `value_class == "A"` (the ≤75% cum-PR head).
    scope == "top":     every package in results.csv (the whole top-download
                        set across all A/B/C classes).
    """
    path = DATA_DIR / "sources" / eco / "results.csv"
    if not path.exists():
        log.warning("%s/results.csv missing", eco)
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if scope == "a-class" and (row.get("value_class") or "").strip() != "A":
                continue
            pkg = (row.get("package") or "").strip()
            if pkg:
                out.append(pkg)
    return out


# ── consolidated index I/O ─────────────────────────────────────────────────────


def _read_output() -> dict[tuple[str, str], dict[str, str]]:
    """Existing packages.csv keyed by (ecosystem, package)."""
    if not OUTPUT_CSV.exists():
        return {}
    out: dict[tuple[str, str], dict[str, str]] = {}
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[(row["ecosystem"], row["package"])] = row
    return out


def _write_output(rows: dict[tuple[str, str], dict[str, str]]) -> None:
    """Atomically rewrite packages.csv (tmp + replace) so a crash mid-flush
    can never leave a truncated file — readers see the old or new file, never half."""
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for key in sorted(rows):
            w.writerow(rows[key])
    tmp.replace(OUTPUT_CSV)


def _row_from_cache(eco: str, pkg: str, wrapper: dict | None) -> dict[str, str]:
    """Flatten a cached ecosyste.ms wrapper into one packages.csv row."""
    data = (wrapper or {}).get("data") or {}
    rm = data.get("repo_metadata") or {}
    host = rm.get("host")
    host_kind = host.get("kind", "") if isinstance(host, dict) else ""
    return {
        "ecosystem": eco,
        "package": pkg,
        "purl": (data.get("purl") or "").strip(),
        "registry_hit": (wrapper or {}).get("registry_hit", ""),
        "repository_url": (data.get("repository_url") or "").strip(),
        "homepage": (data.get("homepage") or "").strip(),
        "repo_host": host_kind,
        "repo_full_name": (rm.get("full_name") or "").strip(),
        "repo_archived": rm.get("archived", ""),
        "repo_fork": rm.get("fork", ""),
        "repo_stars": rm.get("stargazers_count", ""),
        "last_synced_at": (data.get("last_synced_at") or "").strip(),
        "fetched_at": (wrapper or {}).get("fetched_at", "") or _now_iso(),
    }


# ── fetch ──────────────────────────────────────────────────────────────────────


async def _ensure_row(
    session: aiohttp.ClientSession,
    eco: str,
    pkg: str,
    ttl_days: int,
    sem: asyncio.Semaphore,
    counters: dict[str, int],
    offline: bool = False,
) -> dict[str, str]:
    """Return a packages.csv row for one package, fetching only if the shared
    raw-JSON cache is missing or stale. Mutates `counters` for perf reporting.

    Pass `offline=True` to hard-forbid network access: cache hits are returned
    as usual, but a cache miss/stale entry is returned as-is without fetching.
    """
    cache_p = _cache_path(eco, pkg)
    cached = _read_cached(cache_p)
    if cached and _is_fresh(cached.get("fetched_at", ""), ttl_days):
        counters["cache"] += 1
        return _row_from_cache(eco, pkg, cached)

    if offline:
        # Hard-forbid network: return whatever cache has (may be stale/empty).
        counters["cache"] += 1
        if cached:
            return _row_from_cache(eco, pkg, cached)
        return {"ecosystem": eco, "package": pkg, "fetched_at": ""}

    # Cache miss / stale → hit the network (writes the raw JSON on a 200).
    await _fetch_one(session, eco, pkg, REGISTRY_MAP[eco], sem)
    counters["fetch"] += 1
    wrapper = _read_cached(cache_p)  # populated iff some registry returned 200
    if wrapper is None:
        counters["miss"] += 1
        return {"ecosystem": eco, "package": pkg, "fetched_at": _now_iso()}
    return _row_from_cache(eco, pkg, wrapper)


async def fetch_eco(
    eco: str,
    ttl_days: int,
    concurrency: int,
    all_rows: dict[tuple[str, str], dict[str, str]],
    flush,
    limit: int | None,
    scope: str = "a-class",
    offline: bool = False,
) -> dict[str, int]:
    """Fetch candidates for one ecosystem INTO the shared `all_rows` dict.

    Rows are written into `all_rows` (the full cross-ecosystem index) as they
    resolve, and `flush()` rewrites packages.csv every BATCH_FLUSH new rows plus
    once at the end of the ecosystem — so an interrupted run keeps its progress.
    Returns counters.

    Pass `offline=True` to hard-forbid network: only cached/stale data is used.
    """
    pkgs = _candidate_packages(eco, scope)
    if limit:
        pkgs = pkgs[:limit]
    counters = {"candidates": len(pkgs), "kept": 0, "cache": 0, "fetch": 0, "miss": 0}
    if not pkgs:
        return counters

    # Incremental: rows already indexed within the TTL stay put in `all_rows`;
    # only missing/stale packages move on.
    todo: list[str] = []
    for pkg in pkgs:
        prev = all_rows.get((eco, pkg))
        if prev and _is_fresh(prev.get("fetched_at", ""), ttl_days):
            counters["kept"] += 1
        else:
            todo.append(pkg)

    if todo:
        sem = asyncio.Semaphore(concurrency)
        progress = Progress(
            SpinnerColumn(), BarColumn(bar_width=16), TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
            TimeElapsedColumn(), TextColumn("[dim]{task.description}[/]"),
            console=console,
        )
        since_flush = 0
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        ) as session:
            with progress:
                task = progress.add_task(eco, total=len(todo))

                async def _run(p: str) -> None:
                    nonlocal since_flush
                    all_rows[(eco, p)] = await _ensure_row(
                        session, eco, p, ttl_days, sem, counters, offline=offline)
                    progress.update(task, advance=1, description=p[:24])
                    since_flush += 1
                    if since_flush >= BATCH_FLUSH:
                        since_flush = 0
                        flush()  # incremental checkpoint — survives interruption

                await asyncio.gather(*[_run(p) for p in todo])

    flush()  # end-of-ecosystem checkpoint
    return counters


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eco", choices=list(REGISTRY_MAP) + ["all"], default="all",
                        help="Ecosystem (default: all)")
    parser.add_argument("--scope", choices=["a-class", "top"], default="top",
                        help="top = every results.csv package (default, full authoritative coverage); "
                             "a-class = value_class==A only")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip refetch if cached within N days (default: {DEFAULT_TTL_DAYS}, 0 to force)")
    parser.add_argument("--offline", action="store_true",
                        help="Hard-forbid network; use only existing caches (stale data is OK).")
    parser.add_argument("--refresh", action="store_true",
                        help="Force refetch for all packages, ignoring TTL (equivalent to --ttl 0).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Concurrent in-flight requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--limit", type=int, help="Process only first N candidates per eco (testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # --refresh maps to ttl=0 (force all); --offline overrides to skip network.
    ttl = 0 if args.refresh else args.ttl

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    ecosystems = list(REGISTRY_MAP) if args.eco == "all" else [args.eco]

    console.print()
    console.print(f"[bold]ecosyste.ms candidate fetch[/bold]  [dim]scope={args.scope} | "
                  f"{_now_iso()} | ttl={ttl}d | conc={args.concurrency}"
                  f"{' | OFFLINE' if args.offline else ''}[/dim]")

    all_rows: dict[tuple[str, str], dict[str, str]] = _read_output()

    def flush() -> None:
        _write_output(all_rows)

    grand = {"candidates": 0, "kept": 0, "cache": 0, "fetch": 0, "miss": 0}
    network_pkgs = 0

    summary = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    summary.add_column("Ecosystem", style="bold")
    for col in ("Cand.", "Kept", "Cache", "Fetched", "Miss", "With repo"):
        summary.add_column(col, justify="right",
                           style="green" if col == "With repo" else None)

    t0 = time.monotonic()
    for eco in ecosystems:
        c = fetch_eco_sync(eco, ttl, args.concurrency, all_rows, flush, args.limit,
                           args.scope, offline=args.offline)
        network_pkgs += c["fetch"]
        with_repo = sum(1 for k, v in all_rows.items() if k[0] == eco and v.get("repo_full_name"))
        for k in grand:
            grand[k] += c[k]
        summary.add_row(eco, f"{c['candidates']:,}", f"{c['kept']:,}", f"{c['cache']:,}",
                        f"{c['fetch']:,}", f"{c['miss']:,}", f"{with_repo:,}")
    elapsed = time.monotonic() - t0

    flush()  # final checkpoint

    total_repo = sum(1 for k in all_rows if all_rows[k].get("repo_full_name"))
    summary.add_section()
    summary.add_row("[bold]Total[/bold]", f"[bold]{grand['candidates']:,}[/bold]",
                    f"[bold]{grand['kept']:,}[/bold]", f"[bold]{grand['cache']:,}[/bold]",
                    f"[bold]{grand['fetch']:,}[/bold]", f"[bold]{grand['miss']:,}[/bold]",
                    f"[bold]{total_repo:,}[/bold]")

    console.print()
    console.print(summary)

    # Perf stats — the network fetch is what costs; cache/kept are ~free.
    rate = network_pkgs / elapsed if elapsed > 0 else 0.0
    per_req = (elapsed / network_pkgs * 1000) if network_pkgs else 0.0
    console.print()
    console.print(
        f"[bold]Perf[/bold]  [dim]{network_pkgs:,} network fetches in {elapsed:.1f}s[/dim]  "
        f"→ [green]{rate:.1f} pkg/s[/green]  [dim]({per_req:.0f} ms/req @ conc {args.concurrency})[/dim]"
    )
    console.print(f"[dim]rows total {len(all_rows):,} | with repo {total_repo:,} "
                  f"| wrote {OUTPUT_CSV}[/dim]")


def fetch_eco_sync(eco, ttl, conc, all_rows, flush, limit, scope="a-class",
                   offline: bool = False):
    """Sync wrapper so the per-eco coroutine runs under one event loop per eco."""
    return asyncio.run(fetch_eco(eco, ttl, conc, all_rows, flush, limit, scope,
                                 offline=offline))


if __name__ == "__main__":
    main()
