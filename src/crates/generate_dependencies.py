"""
Build package-dependencies.csv for top crates.io packages.

Uses default_versions.csv to select one canonical version per crate,
then extracts its dependencies from dependencies.csv.

Reads:
  - data/crates/db-dump/default_versions.csv
  - data/crates/db-dump/versions.csv
  - data/crates/db-dump/crates.csv
  - data/crates/db-dump/dependencies.csv
  - data/crates/top-package-downloads.csv

Outputs:
  - data/crates/package-dependencies.csv

Run:
    uv run src/crates/generate_dependencies.py
"""

import csv
import os
import time
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

csv.field_size_limit(10 * 1024 * 1024)

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data" / "crates"
DUMP_DIR = DATA_DIR / "db-dump"

DEFAULT_VERSIONS_CSV = DUMP_DIR / "default_versions.csv"
VERSIONS_CSV         = DUMP_DIR / "versions.csv"
CRATES_CSV           = DUMP_DIR / "crates.csv"
DEPENDENCIES_CSV     = DUMP_DIR / "dependencies.csv"
TOP_PACKAGES_CSV     = DATA_DIR / "top-package-downloads.csv"
OUTPUT_CSV           = DATA_DIR / "package-dependencies.csv"

KIND_MAP = {"0": "normal", "1": "build", "2": "dev"}

console = Console()
console.rule("[bold]crates.io — dependencies")
t0 = time.perf_counter()

# ── Step 1: top packages ───────────────────────────────────────────────────────

console.print("[bold]Step 1:[/bold] Loading top packages …")
top_packages: set[str] = set()
with open(TOP_PACKAGES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        top_packages.add(row["package"])
console.print(f"  {len(top_packages):,} packages")

# ── Step 2: default version IDs ───────────────────────────────────────────────

console.print("[bold]Step 2:[/bold] Loading default_versions.csv …")
t = time.perf_counter()
default_version_ids: set[str] = set()
with open(DEFAULT_VERSIONS_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        default_version_ids.add(row["version_id"])
console.print(f"  {len(default_version_ids):,} default versions  ({time.perf_counter()-t:.1f}s)")

# ── Step 3: version_id → crate_id ─────────────────────────────────────────────

console.print("[bold]Step 3:[/bold] Loading versions.csv …")
t = time.perf_counter()
version_to_crate: dict[str, str] = {}
with open(VERSIONS_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        version_to_crate[row["id"]] = row["crate_id"]
console.print(f"  {len(version_to_crate):,} versions  ({time.perf_counter()-t:.1f}s)")

# ── Step 4: crate_id → name ───────────────────────────────────────────────────

console.print("[bold]Step 4:[/bold] Loading crates.csv …")
t = time.perf_counter()
crate_names: dict[str, str] = {}
with open(CRATES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        crate_names[row["id"]] = row["name"]
console.print(f"  {len(crate_names):,} crates  ({time.perf_counter()-t:.1f}s)")

# ── Step 5: process dependencies ──────────────────────────────────────────────

console.print("[bold]Step 5:[/bold] Processing dependencies.csv …")
t = time.perf_counter()

seen: set[tuple[str, str, str]] = set()
results: list[tuple[str, str, str, str]] = []
stats: dict[str, int] = defaultdict(int)

with open(DEPENDENCIES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        stats["total"] += 1
        vid = row["version_id"]
        if vid not in default_version_ids:
            stats["skip_not_default"] += 1
            continue
        crate_id = version_to_crate.get(vid)
        if not crate_id:
            stats["skip_missing"] += 1
            continue
        pkg = crate_names.get(crate_id)
        if not pkg or pkg not in top_packages:
            stats["skip_not_top"] += 1
            continue
        dep_name = crate_names.get(row["crate_id"])
        if not dep_name:
            stats["skip_missing"] += 1
            continue
        kind = KIND_MAP.get(row["kind"], row["kind"])
        key = (pkg, dep_name, kind)
        if key in seen:
            continue
        seen.add(key)
        results.append((pkg, dep_name, kind, "manifest_parsing"))

results.sort(key=lambda x: (x[0], x[1]))
console.print(
    f"  {stats['total']:,} rows  |  "
    f"{stats['skip_not_default']:,} non-default  |  "
    f"{stats['skip_not_top']:,} not-top  |  "
    f"{len(results):,} kept  ({time.perf_counter()-t:.1f}s)"
)

# ── Step 6: write output ───────────────────────────────────────────────────────

console.print("[bold]Step 6:[/bold] Writing output …")
tmp = str(OUTPUT_CSV) + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["package", "dependency", "type", "detection_method"])
    w.writerows(results)
os.replace(tmp, OUTPUT_CSV)

table = Table(title="Sample dependencies", show_header=True)
table.add_column("Package", style="bold")
table.add_column("Dependency")
table.add_column("Type")
for pkg, dep, kind, _ in results[:10]:
    table.add_row(pkg, dep, kind)

console.print()
console.print(table)
console.print(
    f"\n[bold green]Written {len(results):,} dependencies → {OUTPUT_CSV}[/bold green]"
    f"  (total {time.perf_counter()-t0:.1f}s)"
)
