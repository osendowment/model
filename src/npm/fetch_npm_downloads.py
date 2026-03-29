"""Fetch annual npm download counts per package from the npm downloads API.

Reads packages from data/npm/package-github-mapping.csv and writes yearly
download totals to data/npm/pkg-downloads-sample.csv (same format as
top-package-downloads.csv: package, avg_downloads, 2021, 2022, ...).

Unscoped packages use the bulk endpoint (128 per request).
Scoped packages (@org/pkg) require individual requests (API limitation).

Usage:
    uv run python src/npm/fetch_npm_downloads.py [--limit N]
"""

import argparse
import asyncio
import csv
import logging

import aiohttp
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TimeElapsedColumn, TextColumn

log = logging.getLogger(__name__)
console = Console()

INPUT_CSV = "data/npm/package-github-mapping.csv"
OUTPUT_CSV = "data/npm/pkg-downloads-sample.csv"
API_BASE = "https://api.npmjs.org/downloads/point"
YEARS = [2021, 2022, 2023, 2024, 2025]
BATCH_SIZE = 128   # npm bulk API limit (unscoped packages only)
CONCURRENCY = 5    # parallel requests


def read_packages(filepath: str) -> list[str]:
    with open(filepath, encoding="utf-8") as f:
        return [row["package"] for row in csv.DictReader(f)]


def batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def fetch_year_batch(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    year: int,
    pkg_batch: list[str],
) -> tuple[int, dict[str, int]]:
    """Fetch download counts for a batch of packages for one year.
    Returns (year, {package: downloads}).
    """
    date_range = f"{year}-01-01:{year}-12-31"
    url = f"{API_BASE}/{date_range}/{','.join(pkg_batch)}"
    async with sem:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    # Single-package response is unwrapped; bulk response is keyed by package name
    if "downloads" in data:
        return (year, {pkg_batch[0]: data.get("downloads") or 0})
    return (year, {pkg: (info or {}).get("downloads") or 0 for pkg, info in data.items()})


async def run(packages: list[str], output: str) -> None:
    unscoped = [p for p in packages if not p.startswith("@")]
    scoped = [p for p in packages if p.startswith("@")]

    if scoped:
        console.print(f"  [dim]{len(unscoped)} unscoped (bulk) + {len(scoped)} scoped (individual)[/dim]")

    # downloads[pkg][year] = count
    downloads: dict[str, dict[int, int]] = {pkg: {} for pkg in packages}

    sem = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        coros = [fetch_year_batch(session, sem, year, batch)
                 for year in YEARS for batch in batches(unscoped, BATCH_SIZE)]
        scoped_coros = [fetch_year_batch(session, sem, year, [pkg])
                        for year in YEARS for pkg in scoped]
        all_coros = coros + scoped_coros
        total = len(all_coros)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("fetching...", total=total)

            for coro in asyncio.as_completed(all_coros):
                year, result = await coro
                for pkg, count in result.items():
                    downloads[pkg][year] = count
                progress.advance(task)

    # Write CSV
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["package", "avg_downloads"] + [str(y) for y in YEARS])
        for pkg in packages:
            yearly = [downloads[pkg].get(y, 0) for y in YEARS]
            avg = round(sum(yearly) / len(yearly))
            writer.writerow([pkg, avg] + yearly)

    console.print(f"Wrote {len(packages)} packages → [cyan]{output}[/cyan]")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max packages (for testing)")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    packages = read_packages(INPUT_CSV)
    if args.limit:
        packages = packages[: args.limit]

    console.print(f"Fetching downloads for [bold]{len(packages)}[/bold] packages × {len(YEARS)} years")
    asyncio.run(run(packages, OUTPUT_CSV))


if __name__ == "__main__":
    main()
