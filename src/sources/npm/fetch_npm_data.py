"""
Fetch npm download + dependency data, iterating until the graph is complete.

Each round:
  1. Find packages that appear as deps but have no download data → fetch downloads
  2. Find packages with downloads whose deps haven't been fetched → fetch deps
  Repeats until no gaps remain.

State is persisted every 20 packages — safe to interrupt and resume.

Run:
    uv run src/sources/npm/fetch_npm_data.py
    uv run src/sources/npm/fetch_npm_data.py --max-rounds 3
    uv run src/sources/npm/fetch_npm_data.py --concurrency 20
    uv run src/sources/npm/fetch_npm_data.py --limit 50   # test mode
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

RAW_DOWNLOADS = "data/sources/npm/raw/downloads.csv"
RAW_DEPS      = "data/sources/npm/raw/dependencies.csv"
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
    """Return {(package, year): row} from raw/downloads.csv."""
    if not os.path.exists(RAW_DOWNLOADS):
        return {}
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        return {
            (row["package"], int(row["year"])): {
                "package": row["package"], "year": row["year"], "downloads": row["downloads"],
            }
            for row in csv.DictReader(f)
        }


def packages_with_all_years(raw_dl: dict) -> set[str]:
    """Packages that have data for all 5 years."""
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
    """Packages whose deps have already been fetched (appear as `package` in raw/deps)."""
    if not os.path.exists(RAW_DEPS):
        return set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f) if row.get("package")}


def load_all_dep_names() -> set[str]:
    """All dep_name values seen in raw/deps (excludes __none__ placeholders)."""
    deps = load_raw_deps()
    return {dep_name for deps_list in deps.values() for dep_name, _, _ in deps_list
            if dep_name and dep_name != "__none__"}


def load_raw_deps() -> dict[str, list[tuple[str, str, str]]]:
    """Return {package: [(dep_name, dep_version, fetched_at)]} from raw/deps."""
    if not os.path.exists(RAW_DEPS):
        return {}
    data: dict[str, list] = defaultdict(list)
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            data[row["package"]].append((row["dep_name"], row["dep_version"], row.get("fetched_at", "")))
    return data


def write_raw_deps(deps: dict[str, list[tuple[str, str, str]]]) -> None:
    """Write {package: [(dep_name, dep_version, fetched_at)]} atomically."""
    tmp = RAW_DEPS + ".tmp"
    os.makedirs(os.path.dirname(RAW_DEPS), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(["package", "dep_name", "dep_version", "fetched_at"])
        for pkg in sorted(deps):
            for dep_name, dep_ver, fetched_at in deps[pkg]:
                w.writerow([pkg, dep_name, dep_ver, fetched_at])
    os.replace(tmp, RAW_DEPS)


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
    bar: tqdm | None = None,
) -> tuple[int, dict[str, int | None]]:
    """Return {pkg: count} where None means the package didn't exist that year."""
    url = f"{NPM_DOWNLOADS}/{year}-01-01:{year}-12-31/{','.join(pkgs)}"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 404:
                        return year, {p: None for p in pkgs}
                    if r.status == 429:
                        wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                        if bar: bar.set_description(f"  downloads [rate limit — waiting {wait}s]")
                        await asyncio.sleep(wait)
                        if bar: bar.set_description("  downloads")
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    if "downloads" in data:
                        # single-package response — None means not found
                        dl = data.get("downloads")
                        return year, {pkgs[0]: None if dl is None else dl}
                    # bulk response — info=None means package didn't exist
                    return year, {
                        p: None if info is None else (info.get("downloads") or 0)
                        for p, info in data.items()
                    }
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    if bar: bar.set_description(f"  downloads [retry {attempt + 1}/{MAX_RETRIES} in {wait}s]")
                    await asyncio.sleep(wait)
                    if bar: bar.set_description("  downloads")
    return year, {p: 0 for p in pkgs}  # failed after retries — treat as unknown


SAVE_INTERVAL = 200  # save raw_dl to disk every N completed packages


def _apply_short_circuit(
    pkg: str,
    yr_data: dict[int, int | None],
    raw_dl: dict,
) -> None:
    """Write all years for one package, filling zeros for years before first None."""
    for year in sorted(YEARS, reverse=True):
        count = yr_data.get(year)
        raw_dl[(pkg, year)] = {
            "package": pkg, "year": year,
            "downloads": 0 if count is None else count,
        }
        if count is None:
            for earlier in YEARS:
                if earlier < year:
                    raw_dl[(pkg, earlier)] = {"package": pkg, "year": earlier, "downloads": 0}
            return


async def _fetch_downloads_async(
    packages: list[str],
    concurrency: int,
    raw_dl: dict,
    bar: tqdm,
) -> None:
    """One session, all (year × batch) tasks at once. Saves raw_dl every SAVE_INTERVAL packages."""
    unscoped = [p for p in packages if not p.startswith("@")]
    scoped   = [p for p in packages if p.startswith("@")]

    all_tasks = (
        [(y, b) for y in YEARS for b in batches(unscoped, BATCH_SIZE)]
        + [(y, [p]) for y in YEARS for p in scoped]
    )

    sem     = asyncio.Semaphore(concurrency)
    rl      = RateLimiter(RATE_PER_SEC)
    pkg_yrs: dict[str, dict[int, int | None]] = {p: {} for p in packages}
    waiting: dict[str, int] = {p: len(YEARS) for p in packages}  # year results still outstanding
    n_done  = 0

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [
            asyncio.ensure_future(fetch_downloads_batch(session, sem, rl, y, b, bar))
            for y, b in all_tasks
        ]
        for fut in asyncio.as_completed(futs):
            year, res = await fut
            newly_complete: list[str] = []

            for pkg, count in res.items():
                if year in pkg_yrs[pkg]:
                    continue  # already filled by short-circuit
                pkg_yrs[pkg][year] = count
                waiting[pkg] -= 1
                if count is None:
                    # package didn't exist this year — pre-fill all earlier years
                    for earlier in YEARS:
                        if earlier < year and earlier not in pkg_yrs[pkg]:
                            pkg_yrs[pkg][earlier] = 0
                            waiting[pkg] -= 1
                if waiting[pkg] == 0:
                    newly_complete.append(pkg)

            for pkg in newly_complete:
                _apply_short_circuit(pkg, pkg_yrs[pkg], raw_dl)
                bar.update(1)
                n_done += 1

            if newly_complete and n_done % SAVE_INTERVAL < len(newly_complete):
                write_raw_downloads(raw_dl)

    write_raw_downloads(raw_dl)  # final flush


# ── npm registry deps ─────────────────────────────────────────────────────────

async def fetch_deps_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    package: str,
    bar: tqdm | None = None,
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
                        wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                        if bar: bar.set_description(f"  deps [rate limit — waiting {wait}s]")
                        await asyncio.sleep(wait)
                        if bar: bar.set_description("  deps")
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    return package, list((data.get("dependencies") or {}).items())
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    if bar: bar.set_description(f"  deps [retry {attempt + 1}/{MAX_RETRIES} in {wait}s]")
                    await asyncio.sleep(wait)
                    if bar: bar.set_description("  deps")
    return package, []


async def fetch_all_deps(
    packages: list[str], concurrency: int, bar: tqdm | None = None
) -> dict[str, list[tuple[str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    rl  = RateLimiter(RATE_PER_SEC)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [asyncio.ensure_future(fetch_deps_one(session, sem, rl, p, bar)) for p in packages]
        results = []
        for fut in asyncio.as_completed(futs):
            results.append(await fut)
            if bar:
                bar.update(1)
    return dict(results)


# ── fetch + save helpers (used by process_data.py) ────────────────────────────

def fetch_and_save_deps(packages: list[str], concurrency: int = CONCURRENCY) -> int:
    """Fetch runtime deps for packages, upsert into raw/deps every 20. Returns new edge count."""
    raw_deps   = load_raw_deps()
    edge_count = 0
    with tqdm(total=len(packages), desc="  deps", unit="pkg") as bar:
        for i in range(0, len(packages), 20):
            chunk     = packages[i:i + 20]
            deps_data = asyncio.run(fetch_all_deps(chunk, concurrency, bar=bar))
            now       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
            for pkg, deps in deps_data.items():
                if deps:
                    raw_deps[pkg] = [(dep_name, dep_ver, now) for dep_name, dep_ver in deps if dep_name]
                    edge_count += len(raw_deps[pkg])
                else:
                    raw_deps[pkg] = [("__none__", "", now)]
            write_raw_deps(raw_deps)
    return edge_count


def fetch_and_save_downloads(packages: list[str], concurrency: int = CONCURRENCY) -> None:
    """One asyncio.run(), one session, all years dispatched at once."""
    raw_dl = load_raw_downloads()
    with tqdm(total=len(packages), desc="  downloads", unit="pkg") as bar:
        asyncio.run(_fetch_downloads_async(packages, concurrency, raw_dl, bar))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds",  type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--limit",       type=int, default=None, help="Max packages per step (test mode)")
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

        raw_dl        = load_raw_downloads()
        known         = packages_with_all_years(raw_dl)
        fetched_deps  = load_fetched_dep_packages()
        all_dep_names = load_all_dep_names()
        missing_dl    = all_dep_names - known
        need_deps     = known - fetched_deps

        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row("Known packages",    f"{len(known):,}")
        tbl.add_row("Missing downloads", f"[yellow]{len(missing_dl):,}[/yellow]" if missing_dl else "[green]0[/green]")
        tbl.add_row("Need dep fetch",    f"[yellow]{len(need_deps):,}[/yellow]"  if need_deps  else "[green]0[/green]")
        console.print(tbl)

        if not missing_dl and not need_deps:
            console.print("[bold green]Graph is complete.[/bold green]")
            break

        if missing_dl:
            pkgs = sorted(missing_dl)[:args.limit] if args.limit else sorted(missing_dl)
            console.print(f"\n  Fetching downloads for {len(pkgs):,} packages …")
            t = time.perf_counter()
            fetch_and_save_downloads(pkgs, args.concurrency)
            console.print(f"  Done ({time.perf_counter()-t:.1f}s)")

        if need_deps:
            pkgs = sorted(need_deps)[:args.limit] if args.limit else sorted(need_deps)
            console.print(f"\n  Fetching deps for {len(pkgs):,} packages …")
            t = time.perf_counter()
            new_edges = fetch_and_save_deps(pkgs, args.concurrency)
            console.print(f"  Added {new_edges:,} dep edges ({time.perf_counter()-t:.1f}s)")

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
