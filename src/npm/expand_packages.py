"""
Expand npm package coverage by following dependency edges iteratively.

Starting from top-package-downloads.csv, each round:
  1. Find all dependency packages without download data
  2. Fetch their annual downloads (2021–2025) from npm downloads API
  3. Fetch their runtime dependencies from npm registry
  4. Repeat until no new packages remain

State is persisted after every round — safe to interrupt and resume.

Outputs:
  data/npm/packages-summary.csv     — all packages with downloads (grows each round)
  data/npm/package-dependencies.csv — all known dep edges (grows each round)

Run:
    uv run src/npm/expand_packages.py
    uv run src/npm/expand_packages.py --max-rounds 3
    uv run src/npm/expand_packages.py --concurrency 20
"""

import argparse
import asyncio
import csv
import os
import shutil
import time
from datetime import datetime, timezone

import aiohttp
from rich.console import Console
from rich.table import Table

TOP_PACKAGES_CSV = "data/npm/top-package-downloads.csv"
DEPS_CSV         = "data/npm/package-dependencies.csv"
SUMMARY_CSV      = "data/npm/packages-summary.csv"
NPM_DOWNLOADS    = "https://api.npmjs.org/downloads/point"
NPM_REGISTRY     = "https://registry.npmjs.org"
YEARS            = [2021, 2022, 2023, 2024, 2025]
BATCH_SIZE       = 128   # npm bulk API limit for unscoped packages
CONCURRENCY      = 10
MAX_RETRIES      = 3
RETRY_BACKOFF    = [2, 5, 15]
USER_AGENT       = "osendowment-model/1.0 (research; +https://endowment.dev)"

console = Console()


# ── I/O helpers ───────────────────────────────────────────────────────────────

def load_summary(path: str) -> dict[str, dict]:
    """Return {package: row_dict} from packages-summary.csv."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["package"]: row for row in csv.DictReader(f)}


def load_dep_packages(path: str) -> set[str]:
    """Return set of packages we've already fetched deps for."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f) if row.get("package")}


def load_all_deps(path: str) -> set[str]:
    """Return set of all dep_name values seen (excludes __none__ placeholders)."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {
            row["dep_name"] for row in csv.DictReader(f)
            if row.get("dep_name") and row["dep_name"] != "__none__"
        }


def write_summary(path: str, rows: dict[str, dict]) -> None:
    fields = ["package", "avg_downloads"] + [str(y) for y in YEARS]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore",
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: -int(r.get("avg_downloads") or 0)))
    os.replace(tmp, path)


def append_deps(path: str, rows: list[tuple]) -> None:
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["package", "dep_name", "dep_version", "fetched_at"])
        w.writerows(rows)


# ── npm downloads API ─────────────────────────────────────────────────────────

def batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def fetch_downloads_batch(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    year: int,
    pkgs: list[str],
) -> dict[str, int]:
    url = f"{NPM_DOWNLOADS}/{year}-01-01:{year}-12-31/{','.join(pkgs)}"
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429:
                        await asyncio.sleep(RETRY_BACKOFF[min(attempt, 2)])
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    if "downloads" in data:
                        return {pkgs[0]: data.get("downloads") or 0}
                    return {p: (info or {}).get("downloads") or 0
                            for p, info in data.items()}
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, 2)])
    return {p: 0 for p in pkgs}


async def fetch_all_downloads(
    packages: list[str],
    concurrency: int,
) -> dict[str, dict[int, int]]:
    """Return {package: {year: downloads}}."""
    unscoped = [p for p in packages if not p.startswith("@")]
    scoped   = [p for p in packages if p.startswith("@")]
    result: dict[str, dict[int, int]] = {p: {} for p in packages}

    all_tasks = (
        [(y, b) for y in YEARS for b in batches(unscoped, BATCH_SIZE)]
        + [(y, [p]) for y in YEARS for p in scoped]
    )

    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        coros = [fetch_downloads_batch(session, sem, y, b) for y, b in all_tasks]
        for (year, _), res in zip(all_tasks, await asyncio.gather(*coros)):
            for pkg, count in res.items():
                result[pkg][year] = count

    return result


# ── npm registry deps ─────────────────────────────────────────────────────────

async def fetch_deps_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    package: str,
) -> tuple[str, list[tuple[str, str]]]:
    url = f"{NPM_REGISTRY}/{package}/latest"
    async with sem:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 404:
                        return package, []
                    if r.status == 429:
                        await asyncio.sleep(RETRY_BACKOFF[min(attempt, 2)])
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)
                    deps = list((data.get("dependencies") or {}).items())
                    return package, deps
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, 2)])
    return package, []


async def fetch_all_deps(
    packages: list[str],
    concurrency: int,
) -> dict[str, list[tuple[str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        results = await asyncio.gather(
            *[fetch_deps_one(session, sem, p) for p in packages]
        )
    return dict(results)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds",   type=int, default=20)
    parser.add_argument("--concurrency",  type=int, default=CONCURRENCY)
    args = parser.parse_args()

    console.rule("[bold]npm — expand packages")
    console.print(f"Max rounds  : {args.max_rounds}")
    console.print(f"Concurrency : {args.concurrency}")
    console.print()

    # Initialise packages-summary.csv from top list if not present
    if not os.path.exists(SUMMARY_CSV):
        shutil.copy(TOP_PACKAGES_CSV, SUMMARY_CSV)
        console.print(f"[dim]Initialised {SUMMARY_CSV} from {TOP_PACKAGES_CSV}[/dim]\n")

    t0 = time.perf_counter()

    for round_num in range(1, args.max_rounds + 1):
        console.rule(f"[bold]Round {round_num}")

        summary      = load_summary(SUMMARY_CSV)
        fetched_deps = load_dep_packages(DEPS_CSV)
        all_dep_names = load_all_deps(DEPS_CSV)

        # Packages that appear as deps but have no download data
        missing_downloads = all_dep_names - set(summary.keys())

        # Packages in summary whose deps we haven't fetched yet
        need_deps = set(summary.keys()) - fetched_deps

        console.print(f"  Known packages     : {len(summary):,}")
        console.print(f"  Deps without data  : {len(missing_downloads):,}")
        console.print(f"  Need dep fetch     : {len(need_deps):,}")

        if not missing_downloads and not need_deps:
            console.print("\n[bold green]Nothing left to do — graph is complete.[/bold green]")
            break

        # ── 1. Fetch downloads for missing packages ────────────────────────
        if missing_downloads:
            pkgs = sorted(missing_downloads)
            console.print(f"\n  Fetching downloads for {len(pkgs):,} new packages …")
            t = time.perf_counter()
            dl_data = asyncio.run(fetch_all_downloads(pkgs, args.concurrency))

            new_rows = 0
            for pkg in pkgs:
                yearly = [dl_data[pkg].get(y, 0) for y in YEARS]
                avg = sum(yearly) // len(YEARS)
                summary[pkg] = {
                    "package": pkg,
                    "avg_downloads": avg,
                    **{str(y): yearly[i] for i, y in enumerate(YEARS)},
                }
                new_rows += 1

            write_summary(SUMMARY_CSV, summary)
            console.print(f"  Added {new_rows:,} packages  ({time.perf_counter()-t:.1f}s)")

        # ── 2. Fetch deps for packages we haven't checked yet ─────────────
        if need_deps:
            pkgs = sorted(need_deps)
            console.print(f"\n  Fetching deps for {len(pkgs):,} packages …")
            t = time.perf_counter()
            deps_data = asyncio.run(fetch_all_deps(pkgs, args.concurrency))

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
            new_edges = 0
            rows_to_append: list[tuple] = []
            for pkg, deps in deps_data.items():
                for dep_name, dep_ver in deps:
                    if dep_name:
                        rows_to_append.append((pkg, dep_name, dep_ver, now))
                        new_edges += 1
                # Packages with no deps: write a placeholder so load_dep_packages
                # sees them as "already fetched" on the next round
                if not deps:
                    rows_to_append.append((pkg, "__none__", "", now))

            append_deps(DEPS_CSV, rows_to_append)
            console.print(f"  Added {new_edges:,} dep edges  ({time.perf_counter()-t:.1f}s)")

        # ── Round summary ──────────────────────────────────────────────────
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim")
        table.add_column(justify="right")
        table.add_row("packages-summary.csv", f"{len(summary):,} packages")
        dep_count = sum(1 for _ in open(DEPS_CSV)) - 1
        table.add_row("package-dependencies.csv", f"{dep_count:,} rows")
        console.print()
        console.print(table)

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
