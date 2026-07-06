#!/usr/bin/env python3
"""Build data/preview/preview.xlsx — repos.csv + people.csv as one workbook.

The terminal step of the pipeline: combines the two preview CSVs
(src.build_results -> repos.csv, src.build_people -> people.csv) into a
single spreadsheet for non-technical review, one sheet per CSV ('repos',
'people'). Numeric-looking cells (scores, counts, ids) are written as real
Excel numbers, not text, so sorting/filtering behaves numerically rather
than alphabetically.

Both sheets get:
    - a styled header row (bold white text on a dark-blue fill) so it reads
      as a header at a glance, distinct from the data rows
    - an AutoFilter over the full used range, so every column has a
      dropdown filter/sort control
    - a frozen header row (`freeze_panes`), so the header (and its filter
      dropdowns) stays visible while scrolling a long sheet

Usage:
    uv run python -m src.build_preview_workbook
"""

import csv
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from rich.console import Console

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPOS_CSV = DATA_DIR / "preview" / "repos.csv"
PEOPLE_CSV = DATA_DIR / "preview" / "people.csv"
OUTPUT_FILE = DATA_DIR / "preview" / "preview.xlsx"

SHEETS = [("repos", REPOS_CSV), ("people", PEOPLE_CSV)]

HEADER_FILL = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def _cell_value(raw: str) -> int | float | str | None:
    """Coerce a CSV string to a real Excel number where possible.

    Blank -> None (an empty cell, not the literal string ""). Otherwise try
    int, then float, else keep the original string (ids like `gh/12345`,
    booleans like `True`, free text).
    """
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _write_sheet(ws: Worksheet, csv_path: Path) -> int:
    """Write one CSV's contents into `ws`, styling row 1 as the header.
    Returns the number of data rows written (0 if the CSV is missing/empty).
    """
    if not csv_path.exists():
        return 0
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0

    header, *data_rows = rows
    ws.append(header)
    for row in data_rows:
        ws.append([_cell_value(v) for v in row])

    for col_idx in range(1, len(header) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL

    ws.auto_filter.ref = ws.dimensions
    ws.freeze_panes = "A2"
    return len(data_rows)


def build() -> None:
    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet; we name our own

    for name, path in SHEETS:
        ws = wb.create_sheet(name)
        n = _write_sheet(ws, path)
        console.print(f"  [cyan]{name}[/cyan]: {n:,} rows ← {path}")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT_FILE)


def main() -> None:
    console.print("[bold]Building preview.xlsx...[/bold]\n")
    build()
    console.print(f"\n[dim]Wrote {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
