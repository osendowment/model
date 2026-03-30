"""
Transform package-github-mapping.csv into nice-registry/metadata.csv.

No network calls — reads from the existing github mapping file and writes
to the nice-registry output location used by process_data.py.

Run:
    uv run src/npm/fetch_nice_registry.py
"""

import csv
import os
import time

from rich.console import Console
from rich.table import Table

INPUT_CSV  = "data/npm/package-github-mapping.csv"
OUTPUT_CSV = "data/npm/nice-registry/metadata.csv"

console = Console()


def main() -> None:
    console.rule("[bold]npm — fetch_nice_registry")
    t0 = time.perf_counter()

    rows = []
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append({
                "package":    row["package"],
                "github_repo": row.get("github_repo", ""),
                "repo_url":   row.get("repo_url", ""),
            })

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    tmp = OUTPUT_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["package", "github_repo", "repo_url"],
                           quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, OUTPUT_CSV)

    with_repo = sum(1 for r in rows if r["github_repo"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Packages", f"{len(rows):,}")
    tbl.add_row("With GitHub repo", f"{with_repo:,}")
    tbl.add_row("Output", OUTPUT_CSV)
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)


if __name__ == "__main__":
    main()
