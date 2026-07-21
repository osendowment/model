"""
Download the crates.io DB dump and produce slim pipeline extracts.

Strategy: parallel byte-range requests (server supports Accept-Ranges).
Downloads N chunks concurrently, assembles the archive, extracts the four
source tables — all inside the gitignored tmp/ scratch dir — then writes
slim extracts holding ONLY the columns the pipeline reads into
data/sources/crates/db-dump/ and deletes the tmp artifacts.

Slim extracts (data/sources/crates/db-dump/, gitignored — ~560 MB vs the
3.9 GB raw dump, regenerable, never committed):
  crates.csv           — id, name, repository, homepage
  versions.csv         — id, crate_id, license, yanked
  dependencies.csv     — version_id, crate_id, kind
  default_versions.csv — crate_id, version_id (copied as-is)

Consumers: crates process_data / fetch_licenses / check_eol and
value.build_git_urls — all read these columns by name, per the data-org
rule that source-derived inputs live under data/sources/<source>/.

  - Skips everything while the four slim extracts are all present and inside
    the TTL (TTL_DAYS, from settings.json — the gate reads the newest extract)
  - Deletes the archive and full extraction after slimming
  - Prints throughput and timing stats at each step

Run:
    uv run python -m src.sources.crates.fetch_db_dump
    uv run python -m src.sources.crates.fetch_db_dump --chunks 16  # tune parallelism
    uv run python -m src.sources.crates.fetch_db_dump --refresh    # ignore the TTL
"""

import argparse
import asyncio
import os
import shutil
import tarfile
import time

import httpx
import polars as pl
from rich.console import Console
from rich.table import Table

from src.common.freshness import file_is_fresh
from src.common.lfs import has_real_data
from src.common.params import fetch_ttl_days

DUMP_URL = "https://static.crates.io/db-dump.tar.gz"
SLIM_DIR = "data/sources/crates/db-dump"   # pipeline-read extracts (gitignored)
TMP_DIR  = "tmp/crates-db-dump"            # full raw extraction — transient
TMP_TAR  = "tmp/crates-db-dump.tar.gz"

# Whole-file TTL: 365 days, declared in settings.json. The dump is a complete
# snapshot regenerated wholesale, and the pipeline reads slow-moving columns
# (crate name, repository URL, license, dependency edges), so the extracts stay
# usable for the whole window. A 3.9 GB download is far too heavy to repeat on
# every pipeline run. --refresh overrides it.
TTL_DAYS = fetch_ttl_days("sources/crates/fetch_db_dump")

# file -> columns the pipeline reads; None = keep file as-is (already minimal)
SLIM_COLUMNS: dict[str, list[str] | None] = {
    "crates.csv":           ["id", "name", "repository", "homepage"],
    "versions.csv":         ["id", "crate_id", "license", "yanked"],
    "dependencies.csv":     ["version_id", "crate_id", "kind"],
    "default_versions.csv": None,
}

console = Console()


def newest_csv(directory: str) -> str | None:
    """Path of the most recently written .csv in `directory`, None if empty.

    The output here is a DIRECTORY, but the whole-file TTL gate needs one
    mtime: the newest extract dates the run that last wrote the set.
    """
    if not os.path.isdir(directory):
        return None
    files = [e.path for e in os.scandir(directory)
             if e.is_file() and e.name.endswith(".csv")]
    return max(files, key=os.path.getmtime) if files else None


async def fetch_chunk(client: httpx.AsyncClient, url: str, start: int, end: int) -> bytes:
    headers = {"Range": f"bytes={start}-{end}"}
    r = await client.get(url, headers=headers, timeout=300)
    r.raise_for_status()
    return r.content


async def download_all(url: str, ranges: list[tuple[int, int]],
                       total_bytes: int, t_start: float) -> None:
    async with httpx.AsyncClient(http2=True, timeout=300, follow_redirects=True) as client:
        with console.status(f"[bold]Downloading {len(ranges)} chunks in parallel…"):
            tasks = [fetch_chunk(client, url, s, e) for s, e in ranges]
            chunks = await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - t_start
    speed_mb = total_bytes / elapsed / 1024**2

    console.print(f"[green]✓[/green] Downloaded {total_bytes / 1024**2:.0f} MB "
                  f"in [bold]{elapsed:.1f}s[/bold] "
                  f"([yellow]{speed_mb:.1f} MB/s[/yellow])")

    # All chunks are held in memory until here — write them in order to reassemble the file
    console.print("Writing to disk…", end=" ")
    t_write = time.perf_counter()
    with open(TMP_TAR, "wb") as f:
        for chunk in chunks:
            f.write(chunk)
    console.print(f"done ({time.perf_counter() - t_write:.1f}s)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=int, default=8, help="Parallel download chunks (default: 8)")
    parser.add_argument("--refresh", action="store_true",
                        help=f"Force re-download, ignoring the {TTL_DAYS}-day output TTL")
    args = parser.parse_args()

    console.rule("[bold]crates.io DB dump fetcher")
    console.print(f"Output : [cyan]{SLIM_DIR}/[/cyan] (slim pipeline extracts)")
    console.print(f"Scratch: [cyan]{TMP_DIR}/[/cyan]")
    console.print(f"Chunks : {args.chunks} parallel")
    console.print()

    # ── Skip while the slim extracts are complete AND fresh ────────────────────

    # has_real_data also treats stray LFS pointers (from the era when the dump
    # lived in git) as missing.
    complete = all(has_real_data(f"{SLIM_DIR}/{f}") for f in SLIM_COLUMNS)
    newest = newest_csv(SLIM_DIR)
    if complete and not args.refresh and newest and file_is_fresh(newest, TTL_DAYS):
        age_days = (time.time() - os.path.getmtime(newest)) / 86400
        console.print(f"[green]Already extracted[/green]: {SLIM_DIR}/")
        for fname in sorted(SLIM_COLUMNS):
            size_mb = os.path.getsize(f"{SLIM_DIR}/{fname}") / 1024**2
            console.print(f"  {fname:<40} {size_mb:7.1f} MB")
        console.print(f"[dim]output fresh ({age_days:.1f}d < {TTL_DAYS}d) — "
                      f"skipping; --refresh to force[/dim]")
        return

    missing = [f for f in SLIM_COLUMNS if not has_real_data(f"{SLIM_DIR}/{f}")]
    if missing:
        console.print(f"[yellow]Missing[/yellow]: {missing} — downloading dump")
    elif args.refresh:
        console.print("[yellow]--refresh[/yellow]: ignoring the output TTL — downloading dump")
    else:
        console.print("[yellow]Stale[/yellow]: extracts are past the "
                      f"{TTL_DAYS}-day TTL — downloading dump")

    os.makedirs(SLIM_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    # ── Get file size via HEAD ─────────────────────────────────────────────────

    with httpx.Client(follow_redirects=True) as client:
        head = client.head(DUMP_URL)
        total_bytes = int(head.headers["content-length"])
        final_url = str(head.url)

    console.print(f"URL    : {final_url}")
    console.print(f"Size   : [yellow]{total_bytes / 1024**2:.0f} MB[/yellow] compressed\n")

    # ── Parallel chunked download ──────────────────────────────────────────────

    # Last chunk gets any remainder bytes to ensure full coverage (integer division drops them)
    chunk_size = total_bytes // args.chunks
    ranges = [
        (i * chunk_size, (i + 1) * chunk_size - 1 if i < args.chunks - 1 else total_bytes - 1)
        for i in range(args.chunks)
    ]

    t_start = time.perf_counter()
    asyncio.run(download_all(final_url, ranges, total_bytes, t_start))

    # ── Extract full tables into tmp/ ──────────────────────────────────────────

    console.print(f"\nExtracting to [cyan]{TMP_DIR}/[/cyan] …")

    t_extract = time.perf_counter()
    total_uncompressed = 0
    with tarfile.open(TMP_TAR, "r:gz") as tar:
        for member in tar.getmembers():
            # The dump has a dated top-level dir (e.g. 2025-03-15/data/crates.csv); skip everything else
            if "/data/" not in member.name or not member.isfile():
                continue
            fname = member.name.split("/data/")[-1]  # strip the dated prefix, keep just "crates.csv" etc.
            if fname not in SLIM_COLUMNS:
                continue
            # Rewrite member.name so tar.extract places the file flat in TMP_DIR (no subdirectory)
            member.name = fname
            tar.extract(member, TMP_DIR, filter="data")
            total_uncompressed += member.size

    console.print(f"[green]✓[/green] Extracted {total_uncompressed / 1024**2:.0f} MB "
                  f"in [bold]{time.perf_counter() - t_extract:.1f}s[/bold]")

    # ── Slim to pipeline columns ───────────────────────────────────────────────

    console.print(f"\nSlimming to [cyan]{SLIM_DIR}/[/cyan] …")
    t_slim = time.perf_counter()

    table = Table(title=f"Slim extracts → {SLIM_DIR}/", show_header=True)
    table.add_column("File", style="bold")
    table.add_column("Full (MB)", justify="right")
    table.add_column("Slim (MB)", justify="right")

    total_mb = 0
    for fname, cols in SLIM_COLUMNS.items():
        src, dst = f"{TMP_DIR}/{fname}", f"{SLIM_DIR}/{fname}"
        if cols is None:
            shutil.copy(src, dst)
        else:
            pl.scan_csv(src, infer_schema_length=0).select(cols) \
              .collect(engine="streaming").write_csv(dst)
        full_mb, slim_mb = os.path.getsize(src) / 1024**2, os.path.getsize(dst) / 1024**2
        total_mb += slim_mb
        table.add_row(fname, f"{full_mb:.1f}", f"{slim_mb:.1f}")
    table.add_row("[bold]TOTAL[/bold]", "", f"[bold]{total_mb:.1f}[/bold]")

    console.print(table)
    console.print(f"[green]✓[/green] Slimmed in [bold]{time.perf_counter() - t_slim:.1f}s[/bold]")

    # ── Delete tmp artifacts ───────────────────────────────────────────────────

    os.remove(TMP_TAR)
    shutil.rmtree(TMP_DIR)
    console.print(f"[green]✓[/green] Deleted scratch: {TMP_TAR}, {TMP_DIR}/")

    total_elapsed = time.perf_counter() - t_start
    console.print(f"\n[bold green]Done in {total_elapsed:.1f}s total[/bold green]")


if __name__ == "__main__":
    main()
