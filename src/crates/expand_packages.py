"""
Expand crates packages-summary.csv by following dependency edges.

All data comes from local files — no network requests.

Starting from top-package-downloads.csv, each round:
  1. Find dependency crates without download data
  2. Compute their annual downloads from version-downloads/ monthly files
     (using db-dump/ name mappings)
  3. Repeat until no new crates remain

Outputs:
  data/crates/packages-summary.csv  — all crates with downloads (grows each round)

Run:
    uv run src/crates/expand_packages.py
    uv run src/crates/expand_packages.py --min-avg 0    # include all, even zero-download deps
"""

import argparse
import csv
import glob
import os
import shutil
import time
from collections import defaultdict

import pandas as pd
from rich.console import Console
from rich.table import Table

csv.field_size_limit(10 * 1024 * 1024)

TOP_CSV     = "data/crates/top-package-downloads.csv"
DEPS_CSV    = "data/crates/package-dependencies.csv"
SUMMARY_CSV = "data/crates/packages-summary.csv"
MONTHLY_DIR = "data/crates/version-downloads"
VERSIONS_CSV = "data/crates/db-dump/versions.csv"
CRATES_CSV   = "data/crates/db-dump/crates.csv"
YEARS        = list(range(2021, 2026))

console = Console()


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_summary(path: str) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["package"]: row for row in csv.DictReader(f)}


def load_dep_names(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["dependency"] for row in csv.DictReader(f) if row.get("dependency")}


def write_summary(path: str, rows: dict[str, dict]) -> None:
    fields = ["package", "avg_downloads"] + [str(y) for y in YEARS]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore",
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: -int(r.get("avg_downloads") or 0)))
    os.replace(tmp, path)


# ── Download computation ───────────────────────────────────────────────────────

def load_name_mappings() -> tuple[dict[int, int], dict[int, str]]:
    """Return (version_to_crate, crate_to_name)."""
    console.print("  Loading db-dump name mappings …", end=" ")
    t = time.perf_counter()

    version_to_crate: dict[int, int] = {}
    with open(VERSIONS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            version_to_crate[int(row["id"])] = int(row["crate_id"])

    crate_to_name: dict[int, str] = {}
    with open(CRATES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            crate_to_name[int(row["id"])] = row["name"]

    console.print(f"{len(version_to_crate):,} versions, {len(crate_to_name):,} crates  "
                  f"({time.perf_counter()-t:.1f}s)")
    return version_to_crate, crate_to_name


def compute_downloads_for(
    target_names: set[str],
    version_to_crate: dict[int, int],
    crate_to_name: dict[int, str],
) -> dict[str, dict[int, int]]:
    """
    Aggregate annual downloads from monthly files for the given crate names.
    Returns {crate_name: {year: total_downloads}}.
    """
    name_to_crate_id = {v: k for k, v in crate_to_name.items() if v in target_names}
    target_crate_ids = set(name_to_crate_id.values())

    result: dict[str, dict[int, int]] = {n: {y: 0 for y in YEARS} for n in target_names}

    for year in YEARS:
        monthly_files = sorted(glob.glob(f"{MONTHLY_DIR}/{year}-*.csv"))
        if not monthly_files:
            continue

        t = time.perf_counter()
        dfs = [pd.read_csv(f, dtype={"version_id": "int32", "downloads": "int64"})
               for f in monthly_files]
        df = pd.concat(dfs, ignore_index=True).groupby("version_id", sort=False)["downloads"].sum()
        year_totals = df.to_dict()

        for vid, dl in year_totals.items():
            crate_id = version_to_crate.get(vid)
            if crate_id not in target_crate_ids:
                continue
            name = crate_to_name.get(crate_id)
            if name in result:
                result[name][year] += dl

        console.print(f"    {year}: {len(monthly_files)} months  ({time.perf_counter()-t:.1f}s)")

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--min-avg",    type=int, default=0,
                        help="Min avg downloads to include (default: 0 = all)")
    args = parser.parse_args()

    console.rule("[bold]crates — expand packages")
    console.print(f"Max rounds : {args.max_rounds}")
    console.print(f"Min avg DL : {args.min_avg:,}")
    console.print()

    if not os.path.exists(SUMMARY_CSV):
        shutil.copy(TOP_CSV, SUMMARY_CSV)
        console.print(f"[dim]Initialised {SUMMARY_CSV} from {TOP_CSV}[/dim]\n")

    # Load name mappings once — reused every round
    version_to_crate, crate_to_name = load_name_mappings()
    console.print()

    t0 = time.perf_counter()

    for round_num in range(1, args.max_rounds + 1):
        console.rule(f"[bold]Round {round_num}")

        summary    = load_summary(SUMMARY_CSV)
        dep_names  = load_dep_names(DEPS_CSV)
        missing    = dep_names - set(summary.keys())

        console.print(f"  Known packages    : {len(summary):,}")
        console.print(f"  Dep names in graph: {len(dep_names):,}")
        console.print(f"  Missing downloads : {len(missing):,}")

        if not missing:
            console.print("\n[bold green]Nothing left to do — graph is complete.[/bold green]")
            break

        console.print(f"\n  Computing downloads for {len(missing):,} crates …")
        t = time.perf_counter()
        dl_data = compute_downloads_for(missing, version_to_crate, crate_to_name)

        added = 0
        for name in missing:
            yearly = [dl_data[name].get(y, 0) for y in YEARS]
            avg = sum(yearly) // len(YEARS)
            if avg < args.min_avg:
                summary[name] = {
                    "package": name,
                    "avg_downloads": avg,
                    **{str(y): yearly[i] for i, y in enumerate(YEARS)},
                }
            else:
                summary[name] = {
                    "package": name,
                    "avg_downloads": avg,
                    **{str(y): yearly[i] for i, y in enumerate(YEARS)},
                }
            added += 1

        write_summary(SUMMARY_CSV, summary)
        console.print(f"  Added {added:,} crates  ({time.perf_counter()-t:.1f}s)")

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="dim")
        table.add_column(justify="right")
        table.add_row("packages-summary.csv", f"{len(summary):,} packages")
        console.print()
        console.print(table)

    console.print(f"\n[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
