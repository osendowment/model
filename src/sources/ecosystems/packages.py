#!/usr/bin/env python3
"""ecosyste.ms package connector — backfill missing git/github URLs.

Connector for the ecosyste.ms /registries/{registry}/packages/{name} endpoint,
used by the value pipeline to fill URLs the native registry crawl didn't
surface, so we prefer registry-native URLs and fall back to ecosyste.ms's
cross-registry consensus.

For each ecosystem (npm, pypi, crates, cpp), reads `data/sources/{eco}/results.csv`
and finds packages with empty `github_repo` AND empty `git`. Queries
ecosyste.ms for those packages, caches the full raw JSON response under
`data/sources/ecosystems/{eco}/raw/{package}.json` (90-day TTL), and writes a
downstream-consumable index at `data/sources/ecosystems/{eco}/packages.csv`:

    package, registry_hit, repository_url, homepage, fetched_at

Registry choices per ecosystem:
    npm    → npmjs.org
    pypi   → pypi.org
    crates → crates.io
    cpp    → debian-13, debian-12, spack.io, vcpkg.io, conan.io (first hit wins)

Usage:
    uv run python -m src.sources.ecosystems.packages
    uv run python -m src.sources.ecosystems.packages --eco cpp --limit 20
    uv run python -m src.sources.ecosystems.packages --ttl 0           # force refresh
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
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

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"

# Ecosystem → ordered list of ecosyste.ms registry names to try.
REGISTRY_MAP: dict[str, list[str]] = {
    "npm":    ["npmjs.org"],
    "pypi":   ["pypi.org"],
    "crates": ["crates.io"],
    "cpp":    ["debian-13", "debian-12", "spack.io", "vcpkg.io", "conan.io"],
}

ECOSYSTE_API = "https://packages.ecosyste.ms/api/v1"
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"

DEFAULT_TTL_DAYS = 90
DEFAULT_CONCURRENCY = 8
INDEX_FIELDS = ["package", "registry_hit", "repository_url", "homepage", "fetched_at"]


@dataclass
class FetchResult:
    package: str
    registry_hit: str = ""
    repository_url: str = ""
    homepage: str = ""
    fetched_at: str = ""
    error: str = ""


# ── package selection ───────────────────────────────────────────────────────


def _missing_url_packages(eco: str) -> list[str]:
    """Return packages from results.csv with empty github_repo AND empty git."""
    path = DATA_DIR / "sources" / eco / "results.csv"
    if not path.exists():
        log.warning("%s/results.csv missing", eco)
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("github_repo") or "").strip():
                continue
            if (row.get("git") or "").strip():
                continue
            pkg = (row.get("package") or "").strip()
            if pkg:
                out.append(pkg)
    return out


# ── cache I/O ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_fresh(fetched_at: str, ttl_days: int) -> bool:
    if not fetched_at or ttl_days <= 0:
        return False
    try:
        when = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return when >= dt.datetime.now(dt.UTC) - dt.timedelta(days=ttl_days)
    except ValueError:
        return False


def _cache_path(eco: str, pkg: str) -> Path:
    """Where the raw JSON for one (eco, pkg) is stored."""
    # Some package names contain '/' (npm scopes like @babel/core). Replace with
    # '__' to keep one file per package without nested directories.
    safe = pkg.replace("/", "__")
    return DATA_DIR / "sources" / "ecosystems" / eco / "raw" / f"{safe}.json"


def _read_cached(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cached(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_index(eco: str) -> dict[str, dict[str, str]]:
    path = DATA_DIR / "sources" / "ecosystems" / eco / "packages.csv"
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["package"]] = row
    return out


def _write_index(eco: str, rows: dict[str, dict[str, str]]) -> None:
    path = DATA_DIR / "sources" / "ecosystems" / eco / "packages.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS,
                                extrasaction="ignore",
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for pkg in sorted(rows):
            writer.writerow(rows[pkg])


# ── ecosyste.ms fetch ────────────────────────────────────────────────────────


async def _fetch_one(
    session: aiohttp.ClientSession,
    eco: str,
    pkg: str,
    registries: list[str],
    sem: asyncio.Semaphore,
) -> FetchResult:
    """Try each registry in order; first 200 wins. Caches the raw JSON."""
    cache = _cache_path(eco, pkg)
    fetched_at = _now_iso()
    payload: dict | None = None
    registry_hit = ""

    for reg in registries:
        q_reg = urllib.parse.quote(reg, safe="")
        q_pkg = urllib.parse.quote(pkg, safe="")
        url = f"{ECOSYSTE_API}/registries/{q_reg}/packages/{q_pkg}"
        async with sem:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = None
                        if isinstance(payload, dict) and payload.get("name"):
                            registry_hit = reg
                            break
                        payload = None
                        continue
                    if resp.status == 404:
                        continue
                    if resp.status in (429, 502, 503, 504):
                        # Brief backoff then try the next registry; we stay polite.
                        await asyncio.sleep(2)
                        continue
                    log.debug("%s/%s @ %s: HTTP %s", eco, pkg, reg, resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.debug("%s/%s @ %s: %s", eco, pkg, reg, e)
                continue

    if payload is None:
        return FetchResult(package=pkg, fetched_at=fetched_at,
                           error="not-found-in-any-registry")

    # Persist raw JSON (wrap with our metadata so we know which registry
    # served it without re-querying ecosyste.ms).
    wrapper = {
        "ecosystem": eco,
        "registry_hit": registry_hit,
        "fetched_at": fetched_at,
        "data": payload,
    }
    _write_cached(cache, wrapper)

    return FetchResult(
        package=pkg,
        registry_hit=registry_hit,
        repository_url=(payload.get("repository_url") or "").strip(),
        homepage=(payload.get("homepage") or "").strip(),
        fetched_at=fetched_at,
    )


def _result_from_cache(pkg: str, cached: dict) -> FetchResult:
    """Reconstruct a FetchResult from a cached wrapper file."""
    data = cached.get("data") or {}
    return FetchResult(
        package=pkg,
        registry_hit=cached.get("registry_hit", ""),
        repository_url=(data.get("repository_url") or "").strip(),
        homepage=(data.get("homepage") or "").strip(),
        fetched_at=cached.get("fetched_at", ""),
    )


async def fetch_ecosystem(
    eco: str,
    ttl_days: int,
    concurrency: int,
    limit: int | None = None,
) -> tuple[int, int, int]:
    """Fetch missing-URL packages for one ecosystem. Returns (cached, fetched, errors)."""
    registries = REGISTRY_MAP.get(eco)
    if not registries:
        log.warning("No registry mapping for ecosystem %s", eco)
        return (0, 0, 0)

    pkgs = _missing_url_packages(eco)
    if limit:
        pkgs = pkgs[:limit]

    if not pkgs:
        console.print(f"[dim]{eco}: nothing missing[/dim]")
        return (0, 0, 0)

    # Decide cache vs fetch per package
    cached_index = _read_index(eco)
    to_fetch: list[str] = []
    cached_results: dict[str, FetchResult] = {}
    for pkg in pkgs:
        cache = _read_cached(_cache_path(eco, pkg))
        if cache and _is_fresh(cache.get("fetched_at", ""), ttl_days):
            cached_results[pkg] = _result_from_cache(pkg, cache)
        else:
            to_fetch.append(pkg)

    console.print(f"[bold]{eco}[/bold]  [dim]{len(pkgs):,} missing | "
                  f"{len(cached_results):,} cached | {len(to_fetch):,} to fetch[/dim]")

    fetched: dict[str, FetchResult] = {}
    if to_fetch:
        sem = asyncio.Semaphore(concurrency)
        progress = Progress(
            SpinnerColumn(),
            BarColumn(bar_width=16),
            TaskProgressColumn(),
            TextColumn("[dim]{task.completed}/{task.total}[/]"),
            TimeElapsedColumn(),
            TextColumn("[dim]{task.description}[/]"),
            console=console,
        )
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT,
                                                  "Accept": "application/json"}) as session:
            with progress:
                task = progress.add_task(f"{eco}", total=len(to_fetch))

                async def _run(p: str) -> None:
                    r = await _fetch_one(session, eco, p, registries, sem)
                    fetched[p] = r
                    progress.update(task, advance=1, description=p[:24])

                await asyncio.gather(*[_run(p) for p in to_fetch])

    # Update the index file: existing rows preserved, new ones overwritten.
    all_results = {**cached_results, **fetched}
    for pkg, r in all_results.items():
        cached_index[pkg] = {
            "package": pkg,
            "registry_hit": r.registry_hit,
            "repository_url": r.repository_url,
            "homepage": r.homepage,
            "fetched_at": r.fetched_at,
        }
    _write_index(eco, cached_index)

    errors = sum(1 for r in fetched.values() if r.error)
    return (len(cached_results), len(fetched), errors)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eco", choices=list(REGISTRY_MAP) + ["all"],
                        default="all", help="Ecosystem (default: all)")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL_DAYS,
                        help=f"Skip refetch if cached within N days (default: {DEFAULT_TTL_DAYS}, 0 to force)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Concurrent in-flight requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--limit", type=int, help="Process only first N packages per ecosystem (testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    ecosystems = list(REGISTRY_MAP) if args.eco == "all" else [args.eco]

    summary = Table(title="[bold]ecosyste.ms backfill[/bold]",
                    show_header=True, header_style="bold dim", padding=(0, 1))
    summary.add_column("Ecosystem", style="bold")
    summary.add_column("Cached", justify="right")
    summary.add_column("Fetched", justify="right")
    summary.add_column("Hit", justify="right", style="green")
    summary.add_column("Miss", justify="right", style="dim")

    t_start = time.monotonic()
    grand = {"cached": 0, "fetched": 0, "hit": 0, "miss": 0}
    for eco in ecosystems:
        cached, fetched, errors = asyncio.run(fetch_ecosystem(
            eco, ttl_days=args.ttl, concurrency=args.concurrency, limit=args.limit,
        ))
        # "hit" = packages where ecosyste.ms returned ANY URL we can use
        index = _read_index(eco)
        hit = sum(1 for r in index.values()
                  if (r.get("repository_url") or r.get("homepage")))
        miss = len(index) - hit
        for k, v in (("cached", cached), ("fetched", fetched), ("hit", hit), ("miss", miss)):
            grand[k] += v
        summary.add_row(eco, f"{cached:,}", f"{fetched:,}", f"{hit:,}", f"{miss:,}")

    summary.add_section()
    summary.add_row("[bold]Total[/bold]",
                    f"[bold]{grand['cached']:,}[/bold]",
                    f"[bold]{grand['fetched']:,}[/bold]",
                    f"[bold]{grand['hit']:,}[/bold]",
                    f"[bold]{grand['miss']:,}[/bold]")

    console.print()
    console.print(summary)
    console.print(f"[dim]Elapsed: {time.monotonic() - t_start:.1f}s[/dim]")


if __name__ == "__main__":
    main()
