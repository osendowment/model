"""
Build packages-summary.csv for PyPI by following dependency edges.

All data comes from local files — no network requests.

Uses data/pypi/bigquery/package-downloads.csv (full BigQuery export, ~849K packages)
to look up downloads for any package that appears as a dependency.

Starting from top-package-downloads.csv, each round:
  1. Find dependency packages without download data
  2. Look them up in the BigQuery export
  3. Repeat until no new packages remain

Outputs:
  data/pypi/packages-summary.csv  — all packages with downloads

Run:
    uv run src/pypi/expand_packages.py
"""

import csv
import os
import shutil
import time

from rich.console import Console
from rich.table import Table

TOP_CSV     = "data/pypi/top-package-downloads.csv"
DEPS_CSV    = "data/pypi/package-dependencies.csv"
BQ_CSV      = "data/pypi/bigquery/package-downloads.csv"
SUMMARY_CSV = "data/pypi/packages-summary.csv"
YEARS       = list(range(2021, 2026))

console = Console()


def load_summary(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["package"]: row for row in csv.DictReader(f)}


def load_dep_names(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {
            row["dependency"].strip().lower()
            for row in csv.DictReader(f)
            if row.get("dependency") and row["dependency"].strip()
        }


def load_bq_index(path: str) -> dict[str, dict]:
    """Load BigQuery export as {lowercase_package: row}."""
    console.print(f"  Loading BigQuery index from {path} …", end=" ")
    t = time.perf_counter()
    index: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            index[row["package"].lower()] = row
    console.print(f"{len(index):,} packages  ({time.perf_counter()-t:.1f}s)")
    return index


def write_summary(path: str, rows: dict[str, dict]) -> None:
    fields = ["package", "avg_downloads"] + [str(y) for y in YEARS]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore",
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: -int(r.get("avg_downloads") or 0)))
    os.replace(tmp, path)


def main() -> None:
    console.rule("[bold]PyPI — expand packages")
    console.print()

    if not os.path.exists(SUMMARY_CSV):
        shutil.copy(TOP_CSV, SUMMARY_CSV)
        console.print(f"[dim]Initialised {SUMMARY_CSV} from {TOP_CSV}[/dim]\n")

    bq = load_bq_index(BQ_CSV)
    console.print()

    t0 = time.perf_counter()
    round_num = 0

    while True:
        round_num += 1
        console.rule(f"[bold]Round {round_num}")

        summary   = load_summary(SUMMARY_CSV)
        dep_names = load_dep_names(DEPS_CSV)

        # Deps not yet in summary (case-insensitive match)
        summary_lower = {k.lower() for k in summary}
        missing = dep_names - summary_lower

        console.print(f"  Known packages    : {len(summary):,}")
        console.print(f"  Dep names in graph: {len(dep_names):,}")
        console.print(f"  Missing downloads : {len(missing):,}")

        if not missing:
            console.print("\n[bold green]Nothing left to do — graph is complete.[/bold green]")
            break

        found = not_found = 0
        for name_lower in missing:
            bq_row = bq.get(name_lower)
            if bq_row:
                summary[bq_row["package"]] = bq_row
                found += 1
            else:
                # Not in BigQuery — add with zeros so we don't re-check
                summary[name_lower] = {
                    "package": name_lower,
                    "avg_downloads": 0,
                    **{str(y): 0 for y in YEARS},
                }
                not_found += 1

        write_summary(SUMMARY_CSV, summary)

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim")
        table.add_column(justify="right")
        table.add_row("Found in BigQuery",     f"[green]{found:,}[/green]")
        table.add_row("Not in BigQuery (→ 0)", f"[dim]{not_found:,}[/dim]")
        table.add_row("packages-summary.csv",  f"{len(summary):,} packages")
        console.print()
        console.print(table)

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
