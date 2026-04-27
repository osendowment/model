#!/usr/bin/env python3
"""Check EOL for the unified C/C++ ecosystem (Debian + Homebrew).

Status: **unsupported** for now.

Neither Debian nor Homebrew expose explicit per-package deprecation flags
in the data we currently scrape:

- Debian: packages can be removed entirely from the archive (an implicit
  EOL), but our `data/debian/raw/package-metadata.csv` does not record
  removal events.
- Homebrew: formulae have `deprecate!` and `disable!` DSL stanzas in the
  Ruby source, but our `data/homebrew/raw/formulas.csv` does not capture
  them.

To keep the schema uniform across ecosystems, this script writes an
`eol.csv` with `is_eol=False`, `eol_method=unsupported` for every cpp
package. Future work: extract Homebrew `deprecate!`/`disable!` flags,
or use Repology's `removed` status.

Reads:
    data/cpp/results.csv

Writes:
    data/cpp/eol.csv

Usage:
    uv run python -m src.cpp.check_eol
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path

from rich.console import Console

from src.eol_common import display_summary, now_iso, write_eol

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RESULTS_FILE = DATA_DIR / "cpp" / "results.csv"
OUTPUT_FILE = DATA_DIR / "cpp" / "eol.csv"

EOL_METHOD = "unsupported"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]cpp.check_eol[/bold]  limit={args.limit or '∞'}  "
                  f"started={started:%Y-%m-%d %H:%M:%S}")

    checked_at = now_iso()
    rows: list[dict] = []
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "package": r["package"],
                "is_eol": False,
                "eol_method": EOL_METHOD,
                "eol_reason": "no registry-level deprecation signal for cpp",
                "source": "unsupported",
                "eol_checked_at": checked_at,
            })
            if args.limit and len(rows) >= args.limit:
                break

    write_eol(OUTPUT_FILE, rows)
    display_summary(console, "cpp", rows)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(rows)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
