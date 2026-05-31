"""
Download and aggregate daily crates.io download archives for a given year,
writing one file per month into data/sources/crates/version-downloads/.

Each monthly file is written atomically only after all daily files for that
month are successfully fetched. Already-complete months are skipped on re-run.

Output: data/sources/crates/version-downloads/YYYY-MM.csv
Schema: version_id, downloads  (total for that month)

Run:
    uv run src/crates/fetch_version_downloads.py --years 2025
    uv run src/crates/fetch_version_downloads.py --years 2021 2022 2023 2024 2025
    uv run src/crates/fetch_version_downloads.py --years 2025 --concurrency 128
"""

import argparse
import asyncio
import calendar
import csv
import os
import time
from collections import defaultdict

import httpx
from rich.console import Console
from rich.table import Table

ARCHIVE_INDEX = "https://static.crates.io/archive/version-downloads/index.json"
ARCHIVE_BASE  = "https://static.crates.io/archive/version-downloads"
OUT_DIR       = "data/sources/crates/version-downloads"
CONCURRENCY   = 64

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--years",       type=int, nargs="+", required=True, metavar="YEAR")
parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
args = parser.parse_args()

os.makedirs(OUT_DIR, exist_ok=True)
years_str = " ".join(str(y) for y in sorted(args.years))
console.rule(f"[bold]crates.io downloads — {years_str}")

# ── Fetch archive index ────────────────────────────────────────────────────────

with httpx.Client(follow_redirects=True) as client:
    index = client.get(ARCHIVE_INDEX).json()

# Build: month_str -> list of daily filenames for that month (used to validate completeness later)
by_month: dict[str, list[str]] = defaultdict(list)
year_prefixes = {str(y) for y in args.years}
for entry in index:
    name = entry["name"]          # e.g. "2025-03-15.csv"
    if name[:4] in year_prefixes:
        month = name[:7]          # "2025-03"
        by_month[month].append(name)

months = sorted(by_month)
if not months:
    console.print(f"[red]No archive entries found for {years_str}[/red]")
    raise SystemExit(1)

console.print(f"Months     : {months[0]} → {months[-1]}  ({len(months)} months)")
console.print(f"Concurrency: {args.concurrency}\n")

# ── Async fetch helpers ────────────────────────────────────────────────────────

async def fetch_day(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    name: str,
) -> tuple[str, list[tuple[int, int]] | None]:
    """Return (name, [(version_id, downloads), ...]) or (name, None) on failure."""
    url = f"{ARCHIVE_BASE}/{name}"
    async with sem:
        for attempt in range(4):
            try:
                r = await client.get(url, timeout=60)
                if r.status_code >= 500:
                    # Server errors are transient; back off and retry rather than failing the month
                    if attempt < 3:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return name, None
                r.raise_for_status()
                pairs = []
                for line in r.text.splitlines()[1:]:  # skip CSV header row
                    if "," in line:
                        vid, dl = line.split(",", 1)
                        pairs.append((int(vid), int(dl)))
                return name, pairs
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return name, None
    return name, None


async def fetch_month(
    month: str,
    filenames: list[str],
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    stats: dict,
) -> dict[int, int] | None:
    """
    Download all daily files for a month concurrently.
    Returns aggregated totals, or None if any file failed.
    Validates that the number of fetched files matches expected calendar days.
    """
    year_int, month_int = map(int, month.split("-"))
    expected_days = calendar.monthrange(year_int, month_int)[1]

    tasks = [fetch_day(client, sem, name) for name in filenames]
    results = await asyncio.gather(*tasks)

    failed = [name for name, pairs in results if pairs is None]
    if failed:
        console.print(f"  [red]✗ {month}: {len(failed)} file(s) failed — skipping month[/red]")
        stats["months_failed"] += 1
        return None

    # Validate day count
    if len(results) != expected_days:
        console.print(
            f"  [red]✗ {month}: got {len(results)} files, expected {expected_days} — skipping month[/red]"
        )
        stats["months_failed"] += 1
        return None

    totals: dict[int, int] = defaultdict(int)
    for _, pairs in results:
        for vid, dl in pairs:
            totals[vid] += dl

    stats["files_ok"]    += len(results)
    stats["bytes_total"] += sum(len(r.content) for r in [] )  # accounted per-fetch
    return dict(totals)


# ── Process months ─────────────────────────────────────────────────────────────

async def run() -> None:
    sem    = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )

    year_stats = {
        "months_done":   0,
        "months_skipped": 0,
        "months_failed": 0,
        "files_ok":      0,
        "bytes_total":   0,
    }

    summary_rows = []
    t_year = time.perf_counter()

    async with httpx.AsyncClient(http2=True, follow_redirects=True, limits=limits, timeout=60) as client:
        for month in months:
            out_file = f"{OUT_DIR}/{month}.csv"

            # Skip already-complete months
            if os.path.exists(out_file):
                row_count = sum(1 for _ in open(out_file)) - 1
                console.print(f"  [dim]skip {month} — already exists ({row_count:,} versions)[/dim]")
                year_stats["months_skipped"] += 1
                summary_rows.append((month, "skipped", row_count, 0, 0))
                continue

            filenames  = by_month[month]
            month_size = sum(
                e["size"] for e in index if e["name"][:7] == month
            ) / 1024**2

            t_month = time.perf_counter()

            # Fetch all days for this month in parallel
            completed_files = 0
            bytes_month = 0
            failed_count = 0

            tasks = [fetch_day(client, sem, name) for name in filenames]

            totals: dict[int, int] = defaultdict(int)
            month_failed = []

            with console.status(f"[bold]{month}[/bold]  0/{len(filenames)} files…") as status:
                for coro in asyncio.as_completed(tasks):
                    name, pairs = await coro
                    if pairs is None:
                        month_failed.append(name)
                        failed_count += 1
                    else:
                        for vid, dl in pairs:
                            totals[vid] += dl
                        completed_files += 1

                    elapsed_m = time.perf_counter() - t_month
                    status.update(
                        f"[bold]{month}[/bold]  "
                        f"{completed_files + failed_count}/{len(filenames)} files  "
                        + (f"[red]{failed_count} failed  [/red]" if failed_count else "")
                        + f"{elapsed_m:.0f}s"
                    )

            month_elapsed = time.perf_counter() - t_month

            # Validate: all days must succeed — a partial month would silently undercount downloads
            if month_failed:
                console.print(
                    f"  [red]✗ {month}: {len(month_failed)} failed "
                    f"({', '.join(sorted(month_failed)[:3])}{'…' if len(month_failed) > 3 else ''}) "
                    f"— not written[/red]"
                )
                year_stats["months_failed"] += 1
                summary_rows.append((month, "failed", 0, month_elapsed, month_size))
                continue

            # Validate: day count matches calendar — guards against gaps in the archive index itself
            year_int, month_int = map(int, month.split("-"))
            expected_days = calendar.monthrange(year_int, month_int)[1]
            if completed_files != expected_days:
                console.print(
                    f"  [red]✗ {month}: {completed_files} files fetched, "
                    f"expected {expected_days} — not written[/red]"
                )
                year_stats["months_failed"] += 1
                summary_rows.append((month, "failed", 0, month_elapsed, month_size))
                continue

            # Atomic write: write to .tmp first so a crash mid-write never leaves a partial file
            # that would be treated as complete on re-run
            tmp = out_file + ".tmp"
            with open(tmp, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["version_id", "downloads"])
                for vid, dl in sorted(totals.items()):
                    w.writerow([vid, dl])
            os.replace(tmp, out_file)

            speed = month_size / month_elapsed if month_elapsed > 0 else 0
            console.print(
                f"  [green]✓[/green] {month}  "
                f"{completed_files} days  "
                f"{month_size:.0f} MB  "
                f"{month_elapsed:.0f}s  "
                f"[yellow]{speed:.1f} MB/s[/yellow]  "
                f"{len(totals):,} versions"
            )

            year_stats["months_done"]  += 1
            year_stats["files_ok"]     += completed_files
            year_stats["bytes_total"]  += month_size
            summary_rows.append((month, "done", len(totals), month_elapsed, month_size))

    # ── Year summary ───────────────────────────────────────────────────────────

    year_elapsed = time.perf_counter() - t_year
    console.print()

    table = Table(title=f"{years_str} summary", show_header=True)
    table.add_column("Month")
    table.add_column("Status")
    table.add_column("Versions",  justify="right")
    table.add_column("MB",        justify="right")
    table.add_column("Time",      justify="right")

    for month, status, versions, elapsed_m, size_mb in summary_rows:
        color = {"done": "green", "skipped": "dim", "failed": "red"}[status]
        table.add_row(
            month,
            f"[{color}]{status}[/{color}]",
            f"{versions:,}" if versions else "—",
            f"{size_mb:.0f}" if size_mb else "—",
            f"{elapsed_m:.0f}s" if elapsed_m else "—",
        )

    console.print(table)
    console.print(
        f"Months: [green]{year_stats['months_done']} done[/green]  "
        f"[dim]{year_stats['months_skipped']} skipped[/dim]  "
        f"[red]{year_stats['months_failed']} failed[/red]  "
        f"| Total: {year_stats['bytes_total']:.0f} MB  {year_elapsed:.0f}s"
    )


asyncio.run(run())
