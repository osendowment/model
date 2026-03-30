"""
Build package-github-mapping.csv for top crates.io packages.

Reads the GitHub repository URL from db-dump/crates.csv and outputs one
row per top package that has a GitHub URL.

Reads:
  - data/crates/db-dump/crates.csv
  - data/crates/top-package-downloads.csv

Outputs:
  - data/crates/package-github-mapping.csv

Run:
    uv run src/crates/generate_github_mapping.py
"""

import csv
import time
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table

csv.field_size_limit(10 * 1024 * 1024)

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data" / "crates"

CRATES_CSV       = DATA_DIR / "db-dump" / "crates.csv"
TOP_PACKAGES_CSV = DATA_DIR / "top-package-downloads.csv"
OUTPUT_CSV       = DATA_DIR / "package-github-mapping.csv"

console = Console()


def normalize_github_url(url: str) -> str | None:
    if not url or "github.com" not in url.lower():
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return f"https://github.com/{parts[0].lower()}/{parts[1].lower()}"


console.rule("[bold]crates.io — GitHub mapping")
t0 = time.perf_counter()

console.print("[bold]Step 1:[/bold] Loading top packages …")
top_packages: set[str] = set()
with open(TOP_PACKAGES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        top_packages.add(row["package"])
console.print(f"  {len(top_packages):,} packages")

console.print("[bold]Step 2:[/bold] Scanning crates.csv for GitHub URLs …")
t = time.perf_counter()
results: list[tuple[str, str]] = []
total = github_count = 0

with open(CRATES_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        total += 1
        name = row["name"]
        if name not in top_packages:
            continue
        url = normalize_github_url(row.get("repository", ""))
        if url:
            results.append((name, url))
            github_count += 1

results.sort(key=lambda x: x[0])
console.print(f"  {total:,} crates scanned  |  {github_count:,} matched  ({time.perf_counter()-t:.1f}s)")

console.print("[bold]Step 3:[/bold] Writing output …")
tmp = str(OUTPUT_CSV) + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["package", "github_url"])
    w.writerows(results)
import os; os.replace(tmp, OUTPUT_CSV)

table = Table(title="Sample mappings", show_header=True)
table.add_column("Package", style="bold")
table.add_column("GitHub URL")
for pkg, url in results[:10]:
    table.add_row(pkg, url)

console.print()
console.print(table)
console.print(
    f"\n[bold green]Written {len(results):,} packages → {OUTPUT_CSV}[/bold green]"
    f"  (total {time.perf_counter()-t0:.1f}s)"
)
