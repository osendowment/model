"""Plumb licenses from `data/sources/homebrew/raw/formulas.csv` into `results.csv`.

Homebrew formulas declare an SPDX license natively (`Formula#license` in
the Ruby DSL). It's already collected — `data/sources/homebrew/raw/formulas.csv`
has the column. This script just joins it into the per-formula results
table and lower-cases for consistency.

Output: `data/sources/homebrew/results.csv` gains a `license` column.

Usage:
    uv run python -m src.sources.homebrew.fetch_licenses
"""

import argparse
import csv
import logging
import os
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
RESULTS = DATA_DIR / "sources" / "homebrew" / "results.csv"
RAW = DATA_DIR / "sources" / "homebrew" / "raw" / "formulas.csv"


def load_license_index() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(RAW, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip().lower()
            lic = (r.get("license") or "").strip().lower()
            if name:
                out[name] = lic
    return out


def main() -> None:
    argparse.ArgumentParser().parse_args()
    console.rule("[bold cyan]homebrew/fetch_licenses")

    if not RESULTS.exists() or not RAW.exists():
        raise SystemExit("missing results.csv or raw/formulas.csv")

    lic_idx = load_license_index()
    console.print(f"  loaded license for [bold]{len(lic_idx):,}[/bold] formulas from raw")

    with open(RESULTS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    if "license" not in fields:
        fields.append("license")

    found = 0
    for r in rows:
        # Homebrew results.csv keys on `formula` (not `package`).
        key = (r.get("formula") or r.get("package") or "").lower()
        lic = lic_idx.get(key, "")
        r["license"] = lic
        if lic:
            found += 1

    tmp = RESULTS.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, RESULTS)

    pct = 100 * found / max(len(rows), 1)
    console.print(f"  populated [bold green]{found:,}[/bold green]/{len(rows):,} ({pct:.1f}%)")
    console.print(f"  → wrote [cyan]{RESULTS}[/cyan]")

    ctr = Counter(r["license"] or "(none)" for r in rows)
    tbl = Table(title="[bold]Top licenses[/bold]", header_style="bold dim")
    tbl.add_column("license"); tbl.add_column("count", justify="right")
    for lic, n in ctr.most_common(15):
        tbl.add_row(lic, f"{n:,}")
    console.print(tbl)


if __name__ == "__main__":
    main()
