#!/usr/bin/env python3
"""
Fetch Rust crate download data from crates.io and produce
data/crates/top-package-downloads.csv with yearly download counts (2021-2025).

Data sources:
  DB dump : https://static.crates.io/db-dump.tar.gz
            → crates.csv  (crate id → name)
            → versions.csv (version id → crate id)
  Archive : https://static.crates.io/archive/version-downloads/{date}.csv
            → daily per-version download counts

Usage:
    # Full run (downloads everything from scratch, ~7.6 GB):
    uv run scripts/fetch_crates_downloads.py

    # Reuse pre-aggregated yearly files from another directory:
    uv run scripts/fetch_crates_downloads.py \\
        --raw-dir /path/to/exp-oss-index/data/raw/crates

    # Lower threshold / custom output:
    uv run scripts/fetch_crates_downloads.py --min-avg 500000
"""

import argparse
import asyncio
import csv
import io
import logging
import os
import tarfile
from collections import defaultdict
from datetime import date, datetime

import httpx
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# ── Config ────────────────────────────────────────────────────────────────────
YEARS = list(range(2021, 2026))
MIN_AVG_DOWNLOADS = 1_000_000
CONCURRENCY = 40
DEFAULT_OUTPUT = "data/crates/top-package-downloads.csv"
CACHE_DIR = ".cache/crates"

ARCHIVE_INDEX = "https://static.crates.io/archive/version-downloads/index.json"
ARCHIVE_BASE = "https://static.crates.io/archive/version-downloads"
DB_DUMP_URL = "https://static.crates.io/db-dump.tar.gz"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)],
)
log = logging.getLogger(__name__)
# suppress noisy httpx/httpcore/hpack debug output
for _noisy in ("httpx", "httpcore", "hpack"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
console = Console()


# ── Crate name mapping ────────────────────────────────────────────────────────

def _load_mapping_from_dir(raw_dir: str) -> tuple[dict[int, str], dict[int, int]]:
    """Load crate_names and version→crate maps from pre-existing CSV files."""
    csv.field_size_limit(10 * 1024 * 1024)  # crates.csv has large readme fields
    crate_names: dict[int, str] = {}
    with open(os.path.join(raw_dir, "crates.csv"), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            crate_names[int(row["id"])] = row["name"]
    log.debug("Loaded %d crate names from %s/crates.csv", len(crate_names), raw_dir)

    version_to_crate: dict[int, int] = {}
    with open(os.path.join(raw_dir, "versions.csv"), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            version_to_crate[int(row["id"])] = int(row["crate_id"])
    log.debug("Loaded %d version mappings from %s/versions.csv", len(version_to_crate), raw_dir)

    return crate_names, version_to_crate


def _load_mapping_from_dump(cache_dir: str) -> tuple[dict[int, str], dict[int, int]]:
    """Download the crates.io DB dump and stream-extract crates.csv + versions.csv."""
    dump_path = os.path.join(cache_dir, "db-dump.tar.gz")

    if not os.path.exists(dump_path):
        log.info("Downloading DB dump from %s …", DB_DUMP_URL)
        with httpx.stream("GET", DB_DUMP_URL, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(dump_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded % (50 * 1024**2) == 0:
                        log.debug("  %.1f / %.1f GB", downloaded / 1024**3, total / 1024**3)
        log.info("DB dump saved to %s", dump_path)
    else:
        log.info("Using cached DB dump: %s", dump_path)

    crate_names: dict[int, str] = {}
    version_to_crate: dict[int, int] = {}

    with tarfile.open(dump_path, "r:gz") as tar:
        for member in tar.getmembers():
            if "/data/" not in member.name:
                continue
            fname = member.name.split("/data/")[-1]
            if fname == "crates.csv":
                f = tar.extractfile(member)
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                    crate_names[int(row["id"])] = row["name"]
                log.debug("Loaded %d crate names from dump", len(crate_names))
            elif fname == "versions.csv":
                f = tar.extractfile(member)
                for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                    version_to_crate[int(row["id"])] = int(row["crate_id"])
                log.debug("Loaded %d version mappings from dump", len(version_to_crate))

    return crate_names, version_to_crate


# ── Per-year downloads ────────────────────────────────────────────────────────

def _load_yearly_from_file(path: str) -> dict[int, int]:
    """Load pre-aggregated {version_id: downloads} from a CSV file."""
    totals: dict[int, int] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            totals[int(row["version_id"])] = int(row["downloads"])
    return totals


def _save_yearly_file(path: str, totals: dict[int, int]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["version_id", "downloads"])
        for vid, dl in sorted(totals.items()):
            writer.writerow([vid, dl])


async def _fetch_daily(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    retries: int = 3,
) -> dict[int, int]:
    async with sem:
        for attempt in range(retries):
            try:
                r = await client.get(url, timeout=60)
                if r.status_code in (500, 502, 503, 504):
                    if attempt < retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    log.warning("Skipping %s (HTTP %d after %d attempts)", url.split("/")[-1], r.status_code, retries)
                    return {}
                r.raise_for_status()
                result: dict[int, int] = {}
                for line in r.text.splitlines()[1:]:  # skip header
                    if "," in line:
                        vid, dl = line.split(",", 1)
                        result[int(vid)] = int(dl)
                return result
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    log.warning("Skipping %s (%s)", url.split("/")[-1], e)
                    return {}
    return {}


async def _download_year(year: int, cache_dir: str) -> dict[int, int]:
    """Download all daily archive files for a year and return {version_id: total}."""
    cache_path = os.path.join(cache_dir, f"downloads_{year}.csv")
    if os.path.exists(cache_path):
        log.info("Year %d: using cached %s", year, cache_path)
        return _load_yearly_from_file(cache_path)

    # Fetch archive index
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(ARCHIVE_INDEX)
        r.raise_for_status()
        index = r.json()

    year_entries = [
        e for e in index
        if e["name"].startswith(str(year))
    ]
    total_mb = sum(e["size"] for e in year_entries) / 1024**2
    log.info("Year %d: %d files, %.0f MB", year, len(year_entries), total_mb)

    urls = [f"{ARCHIVE_BASE}/{e['name']}" for e in year_entries]
    totals: dict[int, int] = defaultdict(int)
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(timeout=60, http2=True) as client:
        tasks = [_fetch_daily(client, sem, url) for url in urls]
        done = 0
        with console.status(f"[bold]Downloading {year} ({len(tasks)} files, {total_mb:.0f} MB)…") as status:
            for coro in asyncio.as_completed(tasks):
                daily = await coro
                for vid, dl in daily.items():
                    totals[vid] += dl
                done += 1
                if done % 50 == 0:
                    status.update(f"[bold]{year}: {done}/{len(tasks)} files…")

    result = dict(totals)
    _save_yearly_file(cache_path, result)
    log.info("Year %d: aggregated %d versions → cached to %s", year, len(result), cache_path)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(raw_dir: str | None, cache_dir: str, min_avg: int, output: str) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    started = datetime.now()
    console.rule("[bold]crates.io download collector")
    console.print(f"Years: {YEARS[0]}–{YEARS[-1]}  |  Min avg: {min_avg:,}  |  Output: {output}")
    if raw_dir:
        console.print(f"Raw data dir: {raw_dir}")
    console.print()

    # Step 1: name mapping
    with console.status("Loading crate name mapping…"):
        if raw_dir and os.path.exists(os.path.join(raw_dir, "crates.csv")):
            crate_names, version_to_crate = _load_mapping_from_dir(raw_dir)
        else:
            crate_names, version_to_crate = _load_mapping_from_dump(cache_dir)
    console.print(f"[green]✓[/green] {len(crate_names):,} crates, {len(version_to_crate):,} versions")

    # Step 2: per-year version downloads
    year_data: dict[int, dict[int, int]] = {}
    for year in YEARS:
        # Try raw_dir first (pre-aggregated), then cache, then download
        raw_path = os.path.join(raw_dir, f"downloads_{year}.csv") if raw_dir else None
        cache_path = os.path.join(cache_dir, f"downloads_{year}.csv")

        if raw_path and os.path.exists(raw_path) and not os.path.exists(cache_path):
            log.info("Year %d: using raw_dir file %s", year, raw_path)
            data = _load_yearly_from_file(raw_path)
            _save_yearly_file(cache_path, data)
            year_data[year] = data
        else:
            year_data[year] = await _download_year(year, cache_dir)

        console.print(f"[green]✓[/green] {year}: {len(year_data[year]):,} versions with downloads")

    # Step 3: aggregate version-level → crate-level
    console.print("\nAggregating to crate level…")
    crate_dl: dict[int, dict[int, int]] = defaultdict(lambda: {y: 0 for y in YEARS})
    for year, vtotals in year_data.items():
        for vid, dl in vtotals.items():
            crate_id = version_to_crate.get(vid)
            if crate_id is not None:
                crate_dl[crate_id][year] += dl

    # Step 4: build rows, filter, sort
    rows = []
    for crate_id, ydl in crate_dl.items():
        name = crate_names.get(crate_id)
        if not name:
            continue
        avg = sum(ydl[y] for y in YEARS) // len(YEARS)
        if avg < min_avg:
            continue
        rows.append({"package": name, "avg_downloads": avg, **{str(y): ydl[y] for y in YEARS}})

    rows.sort(key=lambda r: r["avg_downloads"], reverse=True)
    console.print(f"[green]✓[/green] {len(rows):,} crates with avg ≥ {min_avg:,}\n")

    # Step 5: write CSV
    fields = ["package", "avg_downloads"] + [str(y) for y in YEARS]
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    # Summary table
    table = Table(title="Top Crates by Avg Annual Downloads", show_header=True)
    table.add_column("Package", style="bold")
    table.add_column("Avg", justify="right")
    for y in YEARS:
        table.add_column(str(y), justify="right")
    for row in rows[:15]:
        table.add_row(
            row["package"],
            f"{row['avg_downloads']:,}",
            *[f"{row[str(y)]:,}" for y in YEARS],
        )
    console.print(table)
    elapsed = (datetime.now() - started).total_seconds()
    console.print(f"\n[bold green]Written {len(rows):,} packages to {output}[/bold green]  ({elapsed:.0f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch crates.io download data")
    parser.add_argument(
        "--raw-dir",
        default="/Users/kv/Dev/osendowment/experiments/exp-oss-index/data/raw/crates",
        help="Directory with pre-aggregated downloads_{year}.csv + crates.csv + versions.csv",
    )
    parser.add_argument("--cache-dir", default=CACHE_DIR)
    parser.add_argument("--min-avg", type=int, default=MIN_AVG_DOWNLOADS)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    # Treat --raw-dir as optional (ignore if path doesn't exist)
    raw_dir = args.raw_dir if args.raw_dir and os.path.isdir(args.raw_dir) else None

    asyncio.run(main(raw_dir, args.cache_dir, args.min_avg, args.output))
