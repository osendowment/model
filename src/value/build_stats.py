"""Build data/value/stats.csv -- per-ecosystem value-pipeline statistics.

A metric-row x ecosystem-column matrix (columns are the ecosystems; each
row is one metric). Metrics:

  downloads_<year>     total ecosystem downloads that year (2021-2025). Only
                       the five download-bearing ecosystems; cpp is a derived
                       C/C++ rollup with no comparable download count -> blank.
  packages_top         packages selected into top-packages.csv
  packages_with_deps   packages in results.csv (top union dependency-tree nodes)
  github_repos         resolved GitHub repos (github-repos.csv)

Download sources:
  npm      - npm downloads API (fetches missing years, caches in
             data/sources/npm/raw/npm-stats.csv)
  pypi     - BigQuery export (data/sources/pypi/bigquery/bq-package-downloads.csv)
  crates   - monthly version-download CSVs (data/sources/crates/version-downloads/YYYY-MM.csv)
  debian   - popcon install counts (data/sources/debian/raw/downloads.csv)
  homebrew - Wayback analytics snapshots (data/sources/homebrew/raw/downloads.csv)

Package counts come from each ecosystem's data/sources/<eco>/ outputs, so
this step runs after the ecosystem pipelines (it is the value pipeline's
`stats` step and so refreshes on every run).

Output:
  data/value/stats.csv - metric, npm, pypi, crates, debian, homebrew, cpp

Run:
    uv run python -m src.value.build_stats
"""

import csv
import glob
import os
import time

import pandas as pd
from rich.console import Console
from rich.table import Table

from src.common.params import YEARS

console = Console()

OUTPUT = "data/value/stats.csv"
SOURCES_DIR = "data/sources"

# Ecosystems with a download signal (year-by-year totals).
DOWNLOAD_ECOSYSTEMS = ["npm", "pypi", "crates", "debian", "homebrew"]
# All ecosystem columns (cpp is the derived C/C++ rollup - package counts
# only, no downloads).
ALL_ECOSYSTEMS = ["npm", "pypi", "crates", "debian", "homebrew", "cpp"]


# -- downloads --------------------------------------------------------------

def load_npm() -> dict[int, int]:
    """Load npm total downloads per year, fetching missing years from API."""
    from src.sources.npm.fetch_npm_stats import _load_existing, _fetch_year

    stats_file = "data/sources/npm/raw/npm-stats.csv"
    existing = _load_existing()

    to_fetch = [y for y in YEARS if y not in existing]
    if to_fetch:
        console.print(f"  Fetching {len(to_fetch)} years from npm API...")
        for year in to_fetch:
            existing[year] = _fetch_year(year)

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


DOWNLOAD_LOADERS = {
    "npm": load_npm,
    "pypi": load_pypi,
    "crates": load_crates,
    "debian": lambda: load_raw_downloads("data/sources/debian/raw/downloads.csv"),
    "homebrew": lambda: load_raw_downloads("data/sources/homebrew/raw/downloads.csv"),
}


# -- package / repo counts ----------------------------------------------------

def _count_rows(path: str) -> int | str:
    """Row count of a CSV (excluding header). Empty string if the file is absent."""
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return max(sum(1 for _ in f) - 1, 0)


def package_counts(ecosystem: str, sources_dir: str = SOURCES_DIR) -> dict[str, int | str]:
    """Per-ecosystem package/repo counts from its data/sources/<eco>/ outputs."""
    base = os.path.join(sources_dir, ecosystem)
    return {
        "packages_top": _count_rows(os.path.join(base, "top-packages.csv")),
        "packages_with_deps": _count_rows(os.path.join(base, "results.csv")),
        "github_repos": _count_rows(os.path.join(base, "github-repos.csv")),
    }


# -- assembly -----------------------------------------------------------------

COUNT_METRICS = ["packages_top", "packages_with_deps", "github_repos"]


def build_stats_rows(
    downloads: dict[str, dict[int, int]],
    counts: dict[str, dict[str, int | str]],
) -> list[dict]:
    """Assemble the metric-row x ecosystem-column matrix.

    `downloads` is {eco: {year: total}} for the download ecosystems;
    `counts` is {eco: {metric: value}} for every ecosystem. Returns rows
    keyed by `metric` plus one cell per ecosystem in ALL_ECOSYSTEMS. A
    download cell with no datum (missing year, or a non-download ecosystem
    like cpp) is left blank - distinct from a real 0.
    """
    rows: list[dict] = []
    for year in YEARS:
        row = {"metric": f"downloads_{year}"}
        for eco in ALL_ECOSYSTEMS:
            row[eco] = downloads.get(eco, {}).get(year, "") if eco in DOWNLOAD_ECOSYSTEMS else ""
        rows.append(row)
    for metric in COUNT_METRICS:
        row = {"metric": metric}
        for eco in ALL_ECOSYSTEMS:
            row[eco] = counts.get(eco, {}).get(metric, "")
        rows.append(row)
    return rows


def write_stats(rows: list[dict], path: str = OUTPUT) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fields = ["metric"] + ALL_ECOSYSTEMS
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def main():
    t0 = time.perf_counter()
    console.rule("[bold]Ecosystem stats")

    downloads: dict[str, dict[int, int]] = {}
    for eco in DOWNLOAD_ECOSYSTEMS:
        console.print(f"[bold]{eco}[/bold] downloads")
        downloads[eco] = DOWNLOAD_LOADERS[eco]()
    counts = {eco: package_counts(eco) for eco in ALL_ECOSYSTEMS}

    rows = build_stats_rows(downloads, counts)
    write_stats(rows)

    table = Table(title="[bold]Ecosystem stats[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("metric", style="bold")
    for eco in ALL_ECOSYSTEMS:
        table.add_column(eco, justify="right")
    for row in rows:
        table.add_row(row["metric"], *[f"{row[e]:,}" if isinstance(row[e], int) else "-"
                                       for e in ALL_ECOSYSTEMS])
    console.print(table)
    console.print(f"\n[dim]{OUTPUT}  ({time.perf_counter() - t0:.1f}s)[/dim]")


if __name__ == "__main__":
    main()
