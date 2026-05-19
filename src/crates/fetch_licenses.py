"""Add a `license` column to `data/crates/results.csv` from the local DB dump.

`crates.io` requires every published crate to declare a license — it lives
on each version, in the `license` field of `versions.csv`. We use the
default version (same logic as `check_eol.py`) so the value matches what
`cargo install <crate>` would pull. Output is **lowercase SPDX** to match
the rest of the pipeline.

Common values: `mit`, `apache-2.0`, `mit or apache-2.0` (dual licensing is
the Rust default), `bsd-3-clause`, `unlicense or mit`.

Usage:
    uv run python -m src.crates.fetch_licenses
"""

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

csv.field_size_limit(sys.maxsize)

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RESULTS = DATA_DIR / "crates" / "results.csv"
DUMP = DATA_DIR / "crates" / "db-dump"


def load_license_index() -> dict[str, str]:
    """{crate_name_lowercase: lowercase SPDX} from the dump's default versions."""
    name_by_id: dict[str, str] = {}
    with open(DUMP / "crates.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name_by_id[r["id"]] = r["name"]

    default_by_crate: dict[str, str] = {}
    with open(DUMP / "default_versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            default_by_crate[r["crate_id"]] = r["version_id"]

    needed = set(default_by_crate.values())
    license_by_version: dict[str, str] = {}
    with open(DUMP / "versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["id"] in needed:
                license_by_version[r["id"]] = (r.get("license") or "").strip()

    out: dict[str, str] = {}
    for crate_id, vid in default_by_crate.items():
        name = name_by_id.get(crate_id)
        if name:
            out[name.lower()] = license_by_version.get(vid, "").lower()
    return out


def main() -> None:
    argparse.ArgumentParser().parse_args()
    console.rule("[bold cyan]crates/fetch_licenses")

    if not RESULTS.exists():
        raise SystemExit(f"missing {RESULTS}")

    lic_idx = load_license_index()
    console.print(f"  loaded license for [bold]{len(lic_idx):,}[/bold] crates from db-dump")

    with open(RESULTS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys()) if rows else []
    if "license" not in fields:
        fields.append("license")

    found = 0
    for r in rows:
        lic = lic_idx.get(r["package"].lower(), "")
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

    # Top licenses summary
    from collections import Counter
    ctr = Counter(r["license"] or "(none)" for r in rows)
    tbl = Table(title="[bold]Top licenses[/bold]", header_style="bold dim")
    tbl.add_column("license"); tbl.add_column("count", justify="right")
    for lic, n in ctr.most_common(15):
        tbl.add_row(lic, f"{n:,}")
    console.print(tbl)


if __name__ == "__main__":
    main()
