"""
Fetch all-the-package-repos from nice-registry and save as CSV.

Downloads packages.json (212 MB): {package_name: repo_url | null}
Saves non-null entries to data/sources/npm/nice-registry/packages.csv.

Cached for TTL_DAYS (365) — see the constant for why it is that long.

Run:
    uv run python -m src.sources.npm.fetch_nice_registry
    uv run python -m src.sources.npm.fetch_nice_registry --refresh   # ignore TTL
"""

import argparse
import asyncio
import csv
import json
import os
import time

import aiohttp
from rich.console import Console
from rich.table import Table

from src.common.lfs import has_real_data
from src.common.params import fetch_ttl_days

PACKAGES_URL = "https://media.githubusercontent.com/media/nice-registry/all-the-package-repos/master/data/packages.json"
OUT_DIR      = "data/sources/npm/nice-registry"
OUT_CSV      = f"{OUT_DIR}/packages.csv"

# A 212 MB download over a slow-moving name→repo index. A refresh also
# introduces newly published packages, which resolve to repos that have never
# been validated — and validation needs the network, so a refresh makes the
# next value run network-bound. Pass --refresh to override.
TTL_DAYS = fetch_ttl_days("sources/npm/fetch_nice_registry")

console = Console()


async def download() -> bytes:
    console.print("  Downloading packages.json (212 MB) …")
    t = time.perf_counter()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.get(PACKAGES_URL) as r:
            r.raise_for_status()
            data = await r.read()
    mb = len(data) / 1_048_576
    console.print(f"  [green]✓[/green] {mb:.1f} MB in {time.perf_counter() - t:.1f}s")
    return data


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--refresh", action="store_true",
                   help=f"Force refetch, ignoring the {TTL_DAYS}-day output TTL")
    args = p.parse_args()

    console.rule("[bold]npm — fetch_nice_registry")
    t0 = time.perf_counter()

    # A Git LFS pointer counts as missing — fall through and re-fetch real data.
    cached = has_real_data(OUT_CSV)

    if cached and not args.refresh:
        age = time.time() - os.path.getmtime(OUT_CSV)
        if age < TTL_DAYS * 86400:
            console.print(f"[dim]{OUT_CSV} is {age/86400:.0f}d old — skipping fetch "
                          f"(TTL {TTL_DAYS}d)[/dim]")
            return
        console.print(f"[yellow]{OUT_CSV} is {age/86400:.0f}d old — re-fetching "
                      f"(TTL {TTL_DAYS}d)[/yellow]")

    raw = asyncio.run(download())

    console.print("\n  Parsing …")
    t = time.perf_counter()
    packages: dict[str, str | None] = json.loads(raw)
    console.print(f"  {len(packages):,} entries parsed in {time.perf_counter() - t:.1f}s")

    console.print("\n  Writing CSV …")
    t = time.perf_counter()
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT_CSV + ".tmp"
    rows_written = 0
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["package", "repo_url"])
        for pkg, repo in packages.items():
            if repo:
                w.writerow([pkg, repo])
                rows_written += 1
    os.replace(tmp, OUT_CSV)
    console.print(f"  Written in {time.perf_counter() - t:.1f}s")

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Total packages in source",  f"{len(packages):,}")
    tbl.add_row("With repo URL (written)",   f"[green]{rows_written:,}[/green]")
    tbl.add_row("Without repo (skipped)",    f"[dim]{len(packages) - rows_written:,}[/dim]")
    tbl.add_row("Output",                    OUT_CSV)
    tbl.add_row("Total elapsed",             f"{time.perf_counter() - t0:.1f}s")
    console.print()
    console.print(tbl)


if __name__ == "__main__":
    main()
