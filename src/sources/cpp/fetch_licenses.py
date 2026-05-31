"""Add a `license` column to `data/sources/cpp/results.csv` by joining Homebrew.

`cpp` is a synthetic ecosystem unifying Debian + Homebrew C/C++ packages.
Debian's `debian/copyright` files are unstructured prose and don't yield
clean SPDX, but the same project usually ships as a Homebrew formula and
Homebrew **does** declare an SPDX license (see `homebrew/fetch_licenses.py`).

Strategy: join cpp `package` ↔ homebrew `formula` by name (case-insensitive).
Anything unmatched falls back to empty — eligibility's GitHub-API license
will catch it.

Run order:
    uv run python -m src.sources.homebrew.fetch_licenses   # populate first
    uv run python -m src.sources.cpp.fetch_licenses

Usage:
    uv run python -m src.sources.cpp.fetch_licenses
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
CPP_RESULTS = DATA_DIR / "sources" / "cpp" / "results.csv"
HOMEBREW_RESULTS = DATA_DIR / "sources" / "homebrew" / "results.csv"


def load_homebrew_licenses() -> dict[str, str]:
    if not HOMEBREW_RESULTS.exists():
        raise SystemExit(
            f"missing {HOMEBREW_RESULTS} — run "
            "`uv run python -m src.sources.homebrew.fetch_licenses` first"
        )
    out: dict[str, str] = {}
    with open(HOMEBREW_RESULTS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name = (r.get("formula") or r.get("package") or "").lower()
            lic = (r.get("license") or "").strip().lower()
            if name and lic:
                out[name] = lic
    return out


def main() -> None:
    argparse.ArgumentParser().parse_args()
    console.rule("[bold cyan]cpp/fetch_licenses")

    if not CPP_RESULTS.exists():
        raise SystemExit(f"missing {CPP_RESULTS}")

    lic_idx = load_homebrew_licenses()
    console.print(f"  joined against [bold]{len(lic_idx):,}[/bold] homebrew licenses")

    with open(CPP_RESULTS, encoding="utf-8") as f:
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

    tmp = CPP_RESULTS.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CPP_RESULTS)

    pct = 100 * found / max(len(rows), 1)
    console.print(f"  populated [bold green]{found:,}[/bold green]/{len(rows):,} ({pct:.1f}%)")
    console.print(f"  → wrote [cyan]{CPP_RESULTS}[/cyan]")

    ctr = Counter(r["license"] or "(none)" for r in rows)
    tbl = Table(title="[bold]Top licenses[/bold]", header_style="bold dim")
    tbl.add_column("license"); tbl.add_column("count", justify="right")
    for lic, n in ctr.most_common(15):
        tbl.add_row(lic, f"{n:,}")
    console.print(tbl)


if __name__ == "__main__":
    main()
