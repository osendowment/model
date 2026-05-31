"""
Fetch PyPI dependency data iteratively, starting from top packages.

Each round:
  1. Find packages in the dep tree whose deps haven't been fetched → fetch from PyPI JSON API
  2. Discover new packages from those deps → add to tree
  Repeats until no gaps remain.

State is persisted every SAVE_INTERVAL packages — safe to interrupt and resume.

Output:
  data/sources/pypi/raw/package-dependencies.csv  — package, dependency, type, fetched_at

Run:
    uv run src/pypi/fetch_pypi_data.py
    uv run src/pypi/fetch_pypi_data.py --max-rounds 5
    uv run src/pypi/fetch_pypi_data.py --concurrency 30
    uv run src/pypi/fetch_pypi_data.py --limit 50   # test mode
"""

import argparse
import asyncio
import csv
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

PYPI_JSON     = "https://pypi.org/pypi"
TOP_PACKAGES  = "data/sources/pypi/top-packages.csv"
RAW_DEPS      = "data/sources/pypi/raw/package-dependencies.csv"
CONCURRENCY   = 20
MAX_RETRIES   = 4
RETRY_BACKOFF = [2, 5, 15, 30]
RATE_PER_SEC  = 50.0
SAVE_INTERVAL = 200
USER_AGENT    = "osendowment-model/1.0 (research; +https://endowment.dev)"

# PEP 508: extract package name before any version specifier, extras, or markers
# Handles: "numpy", "numpy>=1.20", "numpy[extra]>=1.20", "numpy (>=1.20)", etc.
RE_PKG_NAME = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)")

console = Console()


# ── PEP 508 parsing ──────────────────────────────────────────────────────────

def parse_requires_dist(requires_dist: list[str] | None) -> list[str]:
    """Extract unique runtime dependency names from requires_dist (skip extras/markers)."""
    if not requires_dist:
        return []
    seen: set[str] = set()
    deps: list[str] = []
    for spec in requires_dist:
        # Skip optional/extra dependencies: "; extra ==" or "; extra=="
        if "; extra ==" in spec or "; extra==" in spec or ";extra ==" in spec or ";extra==" in spec:
            continue
        m = RE_PKG_NAME.match(spec.strip())
        if m:
            name = m.group(1).lower()
            # Normalize PEP 503: underscores/dots → hyphens
            name = re.sub(r"[-_.]+", "-", name)
            if name not in seen:
                seen.add(name)
                deps.append(name)
    return deps


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_top_packages() -> list[str]:
    """Load top package names from top-packages.csv."""
    with open(TOP_PACKAGES, newline="", encoding="utf-8") as f:
        return [row["package"].strip().lower() for row in csv.DictReader(f)]


def load_raw_deps() -> dict[str, list[tuple[str, str, str]]]:
    """Return {package: [(dep, type, fetched_at)]} from existing output."""
    if not os.path.exists(RAW_DEPS):
        return {}
    data: dict[str, list] = defaultdict(list)
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data[row["package"]].append((row["dependency"], row["type"], row.get("fetched_at", "")))
    return data


def fetched_packages(deps: dict[str, list]) -> set[str]:
    """Packages whose deps have been fetched (appear as keys in deps dict)."""
    return set(deps.keys())


def all_dep_names(deps: dict[str, list]) -> set[str]:
    """All dependency names seen (excludes __none__ placeholders)."""
    return {dep for dep_list in deps.values() for dep, _, _ in dep_list
            if dep and dep != "__none__"}


def write_raw_deps(deps: dict[str, list[tuple[str, str, str]]]) -> None:
    """Write deps atomically."""
    tmp = RAW_DEPS + ".tmp"
    os.makedirs(os.path.dirname(RAW_DEPS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["package", "dependency", "type", "fetched_at"])
        for pkg in sorted(deps):
            for dep, dep_type, fetched_at in deps[pkg]:
                w.writerow([pkg, dep, dep_type, fetched_at])
    os.replace(tmp, RAW_DEPS)


# ── Rate limiter ─────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate: float):
        self._rate = rate
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last + 1.0 / self._rate - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ── PyPI JSON API ────────────────────────────────────────────────────────────

async def fetch_deps_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    package: str,
    bar: tqdm | None = None,
) -> tuple[str, list[str] | None]:
    """Fetch runtime deps for one package. Returns (package, [dep_names]) or (package, None) on failure."""
    url = f"{PYPI_JSON}/{package}/json"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 404:
                        return package, []
                    if r.status == 429:
                        wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                        if bar:
                            bar.set_description(f"  deps [rate limit — {wait}s]")
                        await asyncio.sleep(wait)
                        if bar:
                            bar.set_description("  deps")
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    requires = data.get("info", {}).get("requires_dist") or []
                    return package, parse_requires_dist(requires)
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    if bar:
                        bar.set_description(f"  deps [retry {attempt + 1}/{MAX_RETRIES} in {wait}s]")
                    await asyncio.sleep(wait)
                    if bar:
                        bar.set_description("  deps")
    return package, None  # all retries exhausted


async def fetch_all_deps(
    packages: list[str],
    concurrency: int,
    bar: tqdm,
) -> dict[str, list[str] | None]:
    """Fetch deps for all packages with concurrency control."""
    sem = asyncio.Semaphore(concurrency)
    rl = RateLimiter(RATE_PER_SEC)
    results: dict[str, list[str] | None] = {}

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [
            asyncio.ensure_future(fetch_deps_one(session, sem, rl, pkg, bar))
            for pkg in packages
        ]
        for fut in asyncio.as_completed(futs):
            pkg, deps = await fut
            results[pkg] = deps
            bar.update(1)

    return results


# ── Fetch + save in chunks ───────────────────────────────────────────────────

def fetch_and_save_deps(
    packages: list[str],
    concurrency: int,
    raw_deps: dict[str, list[tuple[str, str, str]]],
) -> tuple[int, int]:
    """Fetch deps, upsert into raw_deps, save periodically. Returns (new_packages, new_edges)."""
    new_packages = 0
    new_edges = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Process in chunks for periodic saving
    chunk_size = min(SAVE_INTERVAL, len(packages))
    for i in range(0, len(packages), chunk_size):
        chunk = packages[i:i + chunk_size]
        with tqdm(total=len(chunk), desc="  deps", unit="pkg",
                  bar_format="  {desc}: {n_fmt}/{total_fmt} [{rate_fmt}] {bar:30}") as bar:
            results = asyncio.run(fetch_all_deps(chunk, concurrency, bar))

        for pkg, deps in results.items():
            if deps is None:
                continue  # failed after retries, skip
            new_packages += 1
            if deps:
                raw_deps[pkg] = [(dep, "declared", now) for dep in deps]
                new_edges += len(deps)
            else:
                raw_deps[pkg] = [("__none__", "", now)]

        write_raw_deps(raw_deps)
        done = min(i + chunk_size, len(packages))
        console.print(f"    Saved ({done:,}/{len(packages):,} packages)")

    return new_packages, new_edges


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--limit", type=int, default=None, help="Max packages per round (test mode)")
    args = parser.parse_args()

    console.rule("[bold]PyPI — fetch_pypi_data")
    console.print(f"  Max rounds  : {args.max_rounds}")
    console.print(f"  Concurrency : {args.concurrency}")
    console.print(f"  Rate limit  : {RATE_PER_SEC:.0f} req/s")
    if args.limit:
        console.print(f"  Limit       : [yellow]{args.limit}[/yellow] (test mode)")
    console.print()

    t0 = time.perf_counter()

    # Load seed set: top packages
    top_pkgs = set(load_top_packages())
    console.print(f"  Top packages: {len(top_pkgs):,}")

    for round_num in range(1, args.max_rounds + 1):
        console.rule(f"[bold]Round {round_num}")

        raw_deps = load_raw_deps()
        already_fetched = fetched_packages(raw_deps)
        known_deps = all_dep_names(raw_deps)

        # Universe = top packages + everything reachable via deps
        universe = top_pkgs | known_deps
        need_fetch = sorted(universe - already_fetched)

        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row("Universe (top + deps)", f"{len(universe):,}")
        tbl.add_row("Already fetched", f"[green]{len(already_fetched):,}[/green]")
        tbl.add_row("Need fetch",
                     f"[yellow]{len(need_fetch):,}[/yellow]" if need_fetch else "[green]0[/green]")
        console.print(tbl)

        if not need_fetch:
            console.print("[bold green]Dependency tree is complete.[/bold green]")
            break

        if args.limit:
            need_fetch = need_fetch[:args.limit]

        console.print(f"\n  Fetching deps for {len(need_fetch):,} packages …")
        t = time.perf_counter()
        new_pkgs, new_edges = fetch_and_save_deps(need_fetch, args.concurrency, raw_deps)
        elapsed = time.perf_counter() - t

        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row("Fetched", f"{new_pkgs:,} packages")
        tbl.add_row("New edges", f"{new_edges:,}")
        tbl.add_row("Speed", f"{new_pkgs / elapsed:.0f} pkg/s" if elapsed > 0 else "—")
        tbl.add_row("Elapsed", f"{elapsed:.1f}s")
        console.print(tbl)

    # Final stats
    raw_deps = load_raw_deps()
    total_pkgs = len(raw_deps)
    total_edges = sum(1 for deps in raw_deps.values() for d, _, _ in deps if d != "__none__")

    console.rule()
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Total packages fetched", f"[bold]{total_pkgs:,}[/bold]")
    tbl.add_row("Total dependency edges", f"[bold]{total_edges:,}[/bold]")
    tbl.add_row("Output", RAW_DEPS)
    tbl.add_row("Total elapsed", f"{time.perf_counter() - t0:.1f}s")
    console.print(tbl)


if __name__ == "__main__":
    main()
