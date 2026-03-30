"""
Process monthly version download files into top-package-downloads.csv.

Reads:
  - data/crates/version-downloads/YYYY-MM.csv  — monthly per-version downloads
  - data/crates/db-dump/versions.csv           — version_id → crate_id
  - data/crates/db-dump/crates.csv             — crate_id → package name

Outputs:
  - data/crates/top-package-downloads.csv

Run:
    uv run src/crates/process_data.py
    uv run src/crates/process_data.py --min-avg 500000
"""

import argparse
import csv
import glob
import os
import time

import pandas as pd
from rich.console import Console
from rich.table import Table

YEARS          = list(range(2021, 2026))
MIN_AVG        = 1_000_000
DATA_DIR       = "data/crates"
MONTHLY_DIR    = "data/crates/version-downloads"
VERSIONS_CSV   = "data/crates/db-dump/versions.csv"
CRATES_CSV     = "data/crates/db-dump/crates.csv"
OUT_FILE       = "data/crates/top-package-downloads.csv"

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--min-avg", type=int, default=MIN_AVG)
args = parser.parse_args()

console.rule("[bold]crates.io — process downloads")
console.print(f"DB dump   : [cyan]{DATA_DIR}/db-dump/[/cyan]")
console.print(f"Monthly   : [cyan]{MONTHLY_DIR}/[/cyan]")
console.print(f"Years     : {YEARS[0]}–{YEARS[-1]}")
console.print(f"Min avg   : {args.min_avg:,}")
console.print()

t0 = time.perf_counter()

# ── Step 1: Load version_id → crate_id mapping ────────────────────────────────

console.print("[bold]Step 1:[/bold] Loading versions.csv …")
t = time.perf_counter()

csv.field_size_limit(10 * 1024 * 1024)
version_to_crate: dict[int, int] = {}
with open(VERSIONS_CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        version_to_crate[int(row["id"])] = int(row["crate_id"])

console.print(f"  {len(version_to_crate):,} versions loaded  ({time.perf_counter()-t:.1f}s)")

# ── Step 2: Load crate_id → name mapping ──────────────────────────────────────

console.print("[bold]Step 2:[/bold] Loading crates.csv …")
t = time.perf_counter()

crate_names: dict[int, str] = {}
with open(CRATES_CSV, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        crate_names[int(row["id"])] = row["name"]

console.print(f"  {len(crate_names):,} crates loaded  ({time.perf_counter()-t:.1f}s)")

# ── Step 3: Aggregate monthly files by year → version_id totals ───────────────

console.print("[bold]Step 3:[/bold] Aggregating monthly downloads by year …")
year_totals: dict[int, dict[int, int]] = {}  # year → {version_id: downloads}

for year in YEARS:
    t = time.perf_counter()
    monthly_files = sorted(glob.glob(f"{MONTHLY_DIR}/{year}-*.csv"))

    if not monthly_files:
        console.print(f"  [red]{year}: no files found[/red]")
        year_totals[year] = {}
        continue

    if len(monthly_files) != 12:
        console.print(f"  [yellow]{year}: {len(monthly_files)}/12 months[/yellow]")

    dfs = [pd.read_csv(f, dtype={"version_id": "int32", "downloads": "int64"}) for f in monthly_files]
    df  = pd.concat(dfs, ignore_index=True).groupby("version_id", sort=False)["downloads"].sum()
    year_totals[year] = df.to_dict()

    console.print(f"  {year}: {len(monthly_files)} months, {len(df):,} versions  ({time.perf_counter()-t:.1f}s)")

# ── Step 4: Map version_id → crate_id → downloads per year ───────────────────

console.print("[bold]Step 4:[/bold] Mapping versions → crates …")
t = time.perf_counter()

# crate_year[crate_id][year] = total downloads
from collections import defaultdict
crate_year: dict[int, dict[int, int]] = defaultdict(lambda: {y: 0 for y in YEARS})

unmapped = 0
for year, vtotals in year_totals.items():
    for vid, dl in vtotals.items():
        crate_id = version_to_crate.get(vid)
        if crate_id is None:
            unmapped += 1
            continue
        crate_year[crate_id][year] += dl

console.print(f"  {len(crate_year):,} crates  |  {unmapped:,} unmapped version_ids  ({time.perf_counter()-t:.1f}s)")

# ── Step 5: Build output dataframe ────────────────────────────────────────────

console.print("[bold]Step 5:[/bold] Building output …")
t = time.perf_counter()

year_cols = [str(y) for y in YEARS]
rows = []
for crate_id, ydl in crate_year.items():
    name = crate_names.get(crate_id)
    if not name:
        continue
    yr_vals = [ydl[y] for y in YEARS]
    avg = sum(yr_vals) // len(YEARS)
    if avg < args.min_avg:
        continue
    rows.append({"package": name, "avg_downloads": avg, **{str(y): ydl[y] for y in YEARS}})

rows.sort(key=lambda r: r["avg_downloads"], reverse=True)
console.print(f"  {len(rows):,} packages with avg ≥ {args.min_avg:,}  ({time.perf_counter()-t:.1f}s)")

# ── Step 6: Write CSV ──────────────────────────────────────────────────────────

fields = ["package", "avg_downloads"] + year_cols
tmp = OUT_FILE + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
    w.writeheader()
    w.writerows(rows)
os.replace(tmp, OUT_FILE)

# ── Summary ────────────────────────────────────────────────────────────────────

table = Table(title="Top crates by avg annual downloads", show_header=True)
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

console.print()
console.print(table)
console.print(f"\n[bold green]Written {len(rows):,} packages → {OUT_FILE}[/bold green]  (total {time.perf_counter()-t0:.1f}s)")
