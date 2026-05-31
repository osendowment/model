"""Calculate total ecosystem downloads per year.

Sources:
  npm      — npm downloads API (fetches missing years, caches in data/sources/npm/raw/npm-stats.csv)
  pypi     — BigQuery export (data/sources/pypi/bigquery/bq-package-downloads.csv)
  crates   — monthly version-download CSVs (data/sources/crates/version-downloads/YYYY-MM.csv)
  debian   — popcon install counts (data/sources/debian/raw/downloads.csv)
  homebrew — Wayback analytics snapshots (data/sources/homebrew/raw/downloads.csv)

Output:
  data/value/ecosystem-downloads.csv — year, npm, pypi, crates, debian, homebrew

Run:
    uv run python -m src.pipeline.value.build_ecosystem_downloads
"""

import csv
import glob
import os
import time

import pandas as pd
from rich.console import Console
from rich.table import Table

from src.pipeline.common.params import YEARS

console = Console()

OUTPUT = "data/value/ecosystem-downloads.csv"


def load_npm() -> dict[int, int]:
    """Load npm total downloads per year, fetching missing years from API."""
    from src.npm.fetch_npm_stats import _load_existing, _fetch_year

    stats_file = "data/sources/npm/raw/npm-stats.csv"
    existing = _load_existing()

    to_fetch = [y for y in YEARS if y not in existing]
    if to_fetch:
        console.print(f"  Fetching {len(to_fetch)} years from npm API...")
        for year in to_fetch:
            existing[year] = _fetch_year(year)

        # Save back
        rows = [{"year": y, "downloads": existing[y]} for y in sorted(existing)]
        os.makedirs(os.path.dirname(os.path.abspath(stats_file)), exist_ok=True)
        with open(stats_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["year", "downloads"])
            w.writeheader()
            w.writerows(rows)

    return {y: existing[y] for y in YEARS if y in existing}


def load_pypi() -> dict[int, int]:
    """Sum all package downloads per year from BigQuery export."""
    bq_csv = "data/sources/pypi/bigquery/bq-package-downloads.csv"
    totals = {y: 0 for y in YEARS}

    with open(bq_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for y in YEARS:
                totals[y] += int(row.get(str(y)) or 0)

    return totals


def load_crates() -> dict[int, int]:
    """Sum all version downloads per year from monthly CSVs."""
    monthly_dir = "data/sources/crates/version-downloads"
    totals = {y: 0 for y in YEARS}

    for year in YEARS:
        files = sorted(glob.glob(f"{monthly_dir}/{year}-*.csv"))
        if not files:
            continue
        dfs = [pd.read_csv(f, dtype={"version_id": "int32", "downloads": "int64"}) for f in files]
        totals[year] = int(pd.concat(dfs, ignore_index=True)["downloads"].sum())

    return totals


def load_raw_downloads(path: str) -> dict[int, int]:
    """Sum downloads per year from a raw downloads CSV (year, downloads columns)."""
    totals: dict[int, int] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            y = int(row["year"])
            if y in YEARS:
                totals[y] = totals.get(y, 0) + int(row["downloads"] or 0)
    return totals


ECOSYSTEMS = ["npm", "pypi", "crates", "debian", "homebrew"]


def main():
    t0 = time.perf_counter()
    console.rule("[bold]Ecosystem Downloads")

    loaders = {
        "npm": load_npm,
        "pypi": load_pypi,
        "crates": load_crates,
        "debian": lambda: load_raw_downloads("data/sources/debian/raw/downloads.csv"),
        "homebrew": lambda: load_raw_downloads("data/sources/homebrew/raw/downloads.csv"),
    }

    data = {}
    for eco in ECOSYSTEMS:
        console.print(f"[bold]{eco}[/bold]")
        data[eco] = loaders[eco]()
        years_with_data = sum(1 for y in YEARS if data[eco].get(y, 0) > 0)
        console.print(f"  {years_with_data} years with data")
    console.print()

    # Write CSV — wide format: year rows × ecosystem columns
    rows = []
    for y in YEARS:
        row = {"year": y}
        for eco in ECOSYSTEMS:
            row[eco] = data[eco].get(y, 0)
        rows.append(row)

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["year"] + ECOSYSTEMS, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)

    # Display table
    table = Table(title="Total Ecosystem Downloads", show_header=True, header_style="bold dim")
    table.add_column("Year", style="dim")
    for eco in ECOSYSTEMS:
        table.add_column(eco, justify="right")

    for y in YEARS:
        table.add_row(str(y), *[f"{data[eco].get(y, 0):,}" for eco in ECOSYSTEMS])

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        *[f"[bold]{sum(data[eco].get(y, 0) for y in YEARS):,}[/bold]" for eco in ECOSYSTEMS],
    )
    console.print(table)
    console.print(f"\n[dim]{OUTPUT}  ({time.perf_counter() - t0:.1f}s)[/dim]")


if __name__ == "__main__":
    main()
