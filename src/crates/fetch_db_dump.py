"""
Download and unpack the crates.io DB dump as fast as possible.

Strategy: parallel byte-range requests (server supports Accept-Ranges).
Downloads N chunks concurrently, assembles into final file, then extracts.

Extracts only:
  data/sources/crates/db-dump/crates.csv
  data/sources/crates/db-dump/versions.csv
  data/sources/crates/db-dump/dependencies.csv
  data/sources/crates/db-dump/default_versions.csv

  - Skips download+extraction if data/sources/crates/db-dump/ already exists
  - Deletes archive after extraction
  - Prints throughput and timing stats at each step

Run:
    uv run src/crates/fetch_db_dump.py
    uv run src/crates/fetch_db_dump.py --chunks 16  # tune parallelism
"""

import argparse
import asyncio
import os
import tarfile
import time

import httpx
from rich.console import Console
from rich.table import Table

DUMP_URL  = "https://static.crates.io/db-dump.tar.gz"
DATA_DIR  = "data/crates"
DUMP_DIR  = f"{DATA_DIR}/db-dump"
DUMP_TAR  = f"{DATA_DIR}/db-dump.tar.gz"

EXTRACT_TARGETS = {
    "crates.csv",
    "versions.csv",
    "dependencies.csv",
    "default_versions.csv",
}

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--chunks", type=int, default=8, help="Parallel download chunks (default: 8)")
args = parser.parse_args()

os.makedirs(DATA_DIR, exist_ok=True)

console.rule("[bold]crates.io DB dump fetcher")
console.print(f"Output : [cyan]{DUMP_DIR}/[/cyan]")
console.print(f"Chunks : {args.chunks} parallel")
console.print()

# ── Skip if already extracted ──────────────────────────────────────────────────

all_present = all(os.path.exists(f"{DUMP_DIR}/{f}") for f in EXTRACT_TARGETS)
if all_present:
    console.print(f"[green]Already extracted[/green]: {DUMP_DIR}/")
    for fname in sorted(EXTRACT_TARGETS):
        size_mb = os.path.getsize(f"{DUMP_DIR}/{fname}") / 1024**2
        console.print(f"  {fname:<40} {size_mb:7.1f} MB")
    raise SystemExit(0)

missing = [f for f in EXTRACT_TARGETS if not os.path.exists(f"{DUMP_DIR}/{f}")]
if missing:
    console.print(f"[yellow]Missing files[/yellow]: {missing} — re-downloading dump")

# ── Get file size via HEAD ─────────────────────────────────────────────────────

with httpx.Client(follow_redirects=True) as client:
    head = client.head(DUMP_URL)
    total_bytes = int(head.headers["content-length"])
    final_url = str(head.url)

console.print(f"URL    : {final_url}")
console.print(f"Size   : [yellow]{total_bytes / 1024**2:.0f} MB[/yellow] compressed\n")

# ── Parallel chunked download ──────────────────────────────────────────────────

# Last chunk gets any remainder bytes to ensure full coverage (integer division drops them)
chunk_size = total_bytes // args.chunks
ranges = [
    (i * chunk_size, (i + 1) * chunk_size - 1 if i < args.chunks - 1 else total_bytes - 1)
    for i in range(args.chunks)
]

downloaded_bytes = [0] * args.chunks
t_start = time.perf_counter()


async def fetch_chunk(client: httpx.AsyncClient, idx: int, start: int, end: int) -> bytes:
    headers = {"Range": f"bytes={start}-{end}"}
    r = await client.get(final_url, headers=headers, timeout=300)
    r.raise_for_status()
    data = r.content
    downloaded_bytes[idx] = len(data)
    return data


async def download_all() -> None:
    async with httpx.AsyncClient(http2=True, timeout=300, follow_redirects=True) as client:
        with console.status(f"[bold]Downloading {args.chunks} chunks in parallel…") as status:
            tasks = [fetch_chunk(client, i, s, e) for i, (s, e) in enumerate(ranges)]
            chunks = await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - t_start
    speed_mb = total_bytes / elapsed / 1024**2

    console.print(f"[green]✓[/green] Downloaded {total_bytes / 1024**2:.0f} MB "
                  f"in [bold]{elapsed:.1f}s[/bold] "
                  f"([yellow]{speed_mb:.1f} MB/s[/yellow])")

    # All chunks are held in memory until here — write them in order to reassemble the file
    console.print("Writing to disk…", end=" ")
    t_write = time.perf_counter()
    with open(DUMP_TAR, "wb") as f:
        for chunk in chunks:
            f.write(chunk)
    console.print(f"done ({time.perf_counter() - t_write:.1f}s)")

asyncio.run(download_all())

# ── Extract ────────────────────────────────────────────────────────────────────

console.print(f"\nExtracting to [cyan]{DUMP_DIR}/[/cyan] …")
os.makedirs(DUMP_DIR, exist_ok=True)

t_extract = time.perf_counter()
total_uncompressed = 0
with tarfile.open(DUMP_TAR, "r:gz") as tar:
    for member in tar.getmembers():
        # The dump has a dated top-level dir (e.g. 2025-03-15/data/crates.csv); skip everything else
        if "/data/" not in member.name or not member.isfile():
            continue
        fname = member.name.split("/data/")[-1]  # strip the dated prefix, keep just "crates.csv" etc.
        if fname not in EXTRACT_TARGETS:
            continue
        # Rewrite member.name so tar.extract places the file flat in DUMP_DIR (no subdirectory)
        member.name = fname
        tar.extract(member, DUMP_DIR, filter="data")
        total_uncompressed += member.size

extract_elapsed = time.perf_counter() - t_extract

# ── Summary table ──────────────────────────────────────────────────────────────

table = Table(title=f"Extracted files → {DUMP_DIR}/", show_header=True)
table.add_column("File", style="bold")
table.add_column("Size (MB)", justify="right")

total_mb = 0
for fname in sorted(EXTRACT_TARGETS):
    size_mb = os.path.getsize(f"{DUMP_DIR}/{fname}") / 1024**2
    total_mb += size_mb
    table.add_row(fname, f"{size_mb:.1f}")
table.add_row("[bold]TOTAL[/bold]", f"[bold]{total_mb:.1f}[/bold]")

console.print(table)
console.print(f"[green]✓[/green] Extracted {total_uncompressed / 1024**2:.0f} MB in [bold]{extract_elapsed:.1f}s[/bold]")

# ── Delete archive ─────────────────────────────────────────────────────────────

os.remove(DUMP_TAR)
console.print(f"[green]✓[/green] Deleted archive: {DUMP_TAR}")

total_elapsed = time.perf_counter() - t_start
console.print(f"\n[bold green]Done in {total_elapsed:.1f}s total[/bold green]")
