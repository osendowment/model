"""
Fetch npm download + dependency data, iterating until the graph is complete.

Reads raw/dependencies.csv to find packages without download data, fetches
their annual downloads and runtime deps, repeats until no gaps remain.

State is persisted every 20 packages — safe to interrupt and resume.

Run:
    uv run src/npm/fetch_npm_data.py
    uv run src/npm/fetch_npm_data.py --max-rounds 3
    uv run src/npm/fetch_npm_data.py --concurrency 20
    uv run src/npm/fetch_npm_data.py --limit 50   # test mode
"""

import argparse
import asyncio
import csv
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

RAW_DOWNLOADS = "data/npm/raw/downloads.csv"
RAW_DEPS      = "data/npm/raw/dependencies.csv"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads/point"
NPM_REGISTRY  = "https://registry.npmjs.org"
YEARS         = [2021, 2022, 2023, 2024, 2025]
BATCH_SIZE    = 128
CONCURRENCY   = 5
MAX_RETRIES   = 4
RETRY_BACKOFF = [5, 15, 30, 60]
RATE_PER_SEC  = 5.0
USER_AGENT    = "osendowment-model/1.0 (research; +https://endowment.dev)"

console = Console()


# ── I/O helpers ───────────────────────────────────────────────────────────────

def load_raw_downloads() -> dict[tuple[str, int], dict]:
    if not os.path.exists(RAW_DOWNLOADS):
        return {}
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        return {
            (row["package"], int(row["year"])): {
                "package": row["package"],
                "year": row["year"],
                "downloads": row["downloads"],
            }
            for row in csv.DictReader(f)
        }


def packages_with_all_years(raw_dl: dict) -> set[str]:
    pkg_years: dict[str, set] = defaultdict(set)
    for (pkg, year) in raw_dl:
        pkg_years[pkg].add(year)
    return {pkg for pkg, years in pkg_years.items() if set(YEARS) <= years}


def write_raw_downloads(raw_dl: dict[tuple[str, int], dict]) -> None:
    tmp = RAW_DOWNLOADS + ".tmp"
    os.makedirs(os.path.dirname(RAW_DOWNLOADS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["package", "year", "downloads"],
                           extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(raw_dl.values(), key=lambda r: (r["package"], int(r["year"]))))
    os.replace(tmp, RAW_DOWNLOADS)


def load_fetched_dep_packages() -> set[str]:
    """Packages we've already fetched deps for (appear as package col in deps CSV)."""
    if not os.path.exists(RAW_DEPS):
        return set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f) if row.get("package")}


def load_all_dep_names() -> set[str]:
    """All dep_name values seen (excludes __none__ placeholders)."""
    if not os.path.exists(RAW_DEPS):
        return set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        return {
            row["dep_name"] for row in csv.DictReader(f)
            if row.get("dep_name") and row["dep_name"] != "__none__"
        }


def append_deps(rows: list[tuple]) -> None:
    is_new = not os.path.exists(RAW_DEPS)
    os.makedirs(os.path.dirname(RAW_DEPS), exist_ok=True)
    with open(RAW_DEPS, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        if is_new:
            w.writerow(["package", "dep_name", "dep_version", "fetched_at"])
        w.writerows(rows)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, rate: float):
        self._rate = rate
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now  = asyncio.get_event_loop().time()
            wait = self._last + 1.0 / self._rate - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ── npm downloads API ─────────────────────────────────────────────────────────

def batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def fetch_downloads_batch(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    year: int,
    pkgs: list[str],
) -> tuple[int, dict[str, int]]:
    url = f"{NPM_DOWNLOADS}/{year}-01-01:{year}-12-31/{','.join(pkgs)}"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 429:
                        await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    if "downloads" in data:
                        return year, {pkgs[0]: data.get("downloads") or 0}
                    return year, {p: (info or {}).get("downloads") or 0
                                  for p, info in data.items()}
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    return year, {p: 0 for p in pkgs}


async def fetch_all_downloads(packages: list[str], concurrency: int) -> dict[str, dict[int, int]]:
    unscoped = [p for p in packages if not p.startswith("@")]
    scoped   = [p for p in packages if p.startswith("@")]
    result: dict[str, dict[int, int]] = {p: {} for p in packages}

    all_tasks = (
        [(y, b) for y in YEARS for b in batches(unscoped, BATCH_SIZE)]
        + [(y, [p]) for y in YEARS for p in scoped]
    )

    sem = asyncio.Semaphore(concurrency)
    rl  = RateLimiter(RATE_PER_SEC)

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [
            asyncio.ensure_future(fetch_downloads_batch(session, sem, rl, y, b))
            for y, b in all_tasks
        ]
        with tqdm(total=len(futs), desc="  downloads", unit="req") as bar:
            for fut in asyncio.as_completed(futs):
                year, res = await fut
                for pkg, count in res.items():
                    result[pkg][year] = count
                bar.update(1)

    return result


# ── npm registry deps ─────────────────────────────────────────────────────────

async def fetch_deps_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    package: str,
) -> tuple[str, list[tuple[str, str]]]:
    url = f"{NPM_REGISTRY}/{package}/latest"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 404:
                        return package, []
                    if r.status == 429:
                        await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    return package, list((data.get("dependencies") or {}).items())
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    return package, []


async def fetch_all_deps(packages: list[str], concurrency: int) -> dict[str, list[tuple[str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    rl  = RateLimiter(RATE_PER_SEC)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [asyncio.ensure_future(fetch_deps_one(session, sem, rl, p)) for p in packages]
        results = []
        with tqdm(total=len(futs), desc="  deps", unit="pkg") as bar:
            for fut in asyncio.as_completed(futs):
                results.append(await fut)
                bar.update(1)
    return dict(results)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds",  type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--limit",       type=int, default=None, help="Max packages per step (for testing)")
    args = parser.parse_args()

    console.rule("[bold]npm — fetch_npm_data")
    console.print(f"  Max rounds  : {args.max_rounds}")
    console.print(f"  Concurrency : {args.concurrency}")
    if args.limit:
        console.print(f"  Limit       : [yellow]{args.limit}[/yellow] (test mode)")
    console.print()

    t0 = time.perf_counter()

    for round_num in range(1, args.max_rounds + 1):
        console.rule(f"[bold]Round {round_num}")

        raw_dl         = load_raw_downloads()
        known          = packages_with_all_years(raw_dl)
        fetched_deps   = load_fetched_dep_packages()
        all_dep_names  = load_all_dep_names()
        missing_dl     = all_dep_names - known
        need_deps      = known - fetched_deps

        console.print(f"  Known packages     : {len(known):,}")
        console.print(f"  Deps without data  : {len(missing_dl):,}")
        console.print(f"  Need dep fetch     : {len(need_deps):,}")

        if not missing_dl and not need_deps:
            console.print("\n[bold green]Nothing left to do — graph is complete.[/bold green]")
            break

        # ── 1. Fetch downloads for missing packages ────────────────────────
        if missing_dl:
            pkgs = sorted(missing_dl)
            if args.limit:
                pkgs = pkgs[:args.limit]
            console.print(f"\n  Fetching downloads for {len(pkgs):,} packages …")
            t = time.perf_counter()

            for i in range(0, len(pkgs), 20):
                chunk = pkgs[i:i + 20]
                dl_data = asyncio.run(fetch_all_downloads(chunk, args.concurrency))
                for pkg in chunk:
                    for y in YEARS:
                        raw_dl[(pkg, y)] = {"package": pkg, "year": y,
                                            "downloads": dl_data[pkg].get(y, 0)}
                write_raw_downloads(raw_dl)
                console.print(f"  [{i + len(chunk):,}/{len(pkgs):,}] saved …", end="\r")

            console.print(f"  Added {len(pkgs):,} packages  ({time.perf_counter()-t:.1f}s)")

        # ── 2. Fetch deps for packages we haven't checked yet ─────────────
        if need_deps:
            pkgs = sorted(need_deps)
            if args.limit:
                pkgs = pkgs[:args.limit]
            console.print(f"\n  Fetching deps for {len(pkgs):,} packages …")
            t = time.perf_counter()
            deps_data = asyncio.run(fetch_all_deps(pkgs, args.concurrency))

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
            rows_to_append: list[tuple] = []
            new_edges = 0
            for pkg, deps in deps_data.items():
                for dep_name, dep_ver in deps:
                    if dep_name:
                        rows_to_append.append((pkg, dep_name, dep_ver, now))
                        new_edges += 1
                if not deps:
                    rows_to_append.append((pkg, "__none__", "", now))
            append_deps(rows_to_append)
            console.print(f"  Added {new_edges:,} dep edges  ({time.perf_counter()-t:.1f}s)")

        # ── Round summary ──────────────────────────────────────────────────
        known = packages_with_all_years(raw_dl)
        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row("downloads.csv", f"{len(known):,} packages")
        dep_rows = sum(1 for _ in open(RAW_DEPS)) - 1 if os.path.exists(RAW_DEPS) else 0
        tbl.add_row("dependencies.csv", f"{dep_rows:,} rows")
        console.print()
        console.print(tbl)

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
