#!/usr/bin/env python3
"""Build data/preview/preview.xlsx — repos.csv + people.csv + stats as one workbook.

The terminal step of the pipeline: combines the two preview CSVs
(src.build_results -> repos.csv, src.build_people -> people.csv) into a
single spreadsheet for non-technical review, one sheet per CSV ('repos',
'people'). Numeric-looking cells (scores, counts, ids) are written as real
Excel numbers, not text, so sorting/filtering behaves numerically rather
than alphabetically.

A third sheet, 'stats', renders every markdown table from `docs/stats.md`
(the single source of truth for pipeline counts/funnels) as stacked blocks —
each under its section heading, with the same styled header row. Refresh
stats.md first (`scripts/stats.py --markdown`) so the sheet reflects the
current data.

The CSV sheets get:
    - a styled header row (bold white text on a dark-blue fill) so it reads
      as a header at a glance, distinct from the data rows
    - an AutoFilter over the full used range, so every column has a
      dropdown filter/sort control
    - a frozen header row (`freeze_panes`), so the header (and its filter
      dropdowns) stays visible while scrolling a long sheet

The repos sheet is additionally decorated: each `repo` cell hyperlinks to
the repo's home page (host derived from `repo_id`), and the four decision
columns (`value_score` / `risk_score` / `eligible` / `score`) carry light
background fills (blue / orange / purple / green).

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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPOS_CSV = DATA_DIR / "preview" / "repos.csv"
PEOPLE_CSV = DATA_DIR / "preview" / "people.csv"
STATS_MD = ROOT / "docs" / "stats.md"
OUTPUT_FILE = DATA_DIR / "preview" / "preview.xlsx"

SHEETS = [("repos", REPOS_CSV), ("people", PEOPLE_CSV)]

HEADER_FILL = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF")
SECTION_FONT = Font(bold=True, size=13)

# repos sheet: light column fills on the four decision columns so they pop
# against the raw metrics. Consistent hues: value=blue, risk=orange,
# eligible=purple, score=green.
def _fill(rgb: str) -> PatternFill:
    return PatternFill(start_color=rgb, end_color=rgb, fill_type="solid")

REPOS_COLUMN_FILLS = {
    "value_score": _fill("D9E1F2"),   # light blue
    "risk_score": _fill("FCE4D6"),    # light orange
    "eligible": _fill("E4DFEC"),      # light purple
    "score": _fill("E2EFDA"),         # light green
}


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


def _repo_url(repo: str, repo_id: str) -> str | None:
    """The repo's home page, derived from its platform-qualified id:
    `gh/<n>` → github.com, bare `gl/<n>` → gitlab.com, `gl/<host>-<n>` → that
    self-hosted GitLab instance. Unknown/blank id → no link."""
    if repo_id.startswith("gh/"):
        return f"https://github.com/{repo}"
    if repo_id.startswith("gl/"):
        rest = repo_id[3:]
        if rest.isdigit():
            return f"https://gitlab.com/{repo}"
        return f"https://{rest.rsplit('-', 1)[0]}/{repo}"
    return None


def _decorate_repos_sheet(ws: Worksheet) -> None:
    """repos-only dressing: link each `repo` cell to the repo's home page
    (host from `repo_id`) and fill the decision columns per REPOS_COLUMN_FILLS."""
    headers = {c.value: c.column for c in ws[1]}  # name -> 1-based col index
    repo_col, id_col = headers.get("repo"), headers.get("repo_id")
    fill_cols = {headers[n]: f for n, f in REPOS_COLUMN_FILLS.items() if n in headers}
    for row in ws.iter_rows(min_row=2):
        if repo_col and id_col:
            cell = row[repo_col - 1]
            url = _repo_url(str(cell.value or ""), str(row[id_col - 1].value or ""))
            if url:
                cell.hyperlink = url
                cell.style = "Hyperlink"
        for col, fill in fill_cols.items():
            row[col - 1].fill = fill


def _md_cell(raw: str) -> int | float | str | None:
    """One markdown table cell → an Excel value.

    Strips the markdown dressing (`**bold**`, `` `code` `` — including code
    spans inside prose), then coerces thousands-separated counts to real
    numbers. Percent strings ("97.8%") and prose stay text; a
    blank/placeholder cell becomes an empty cell."""
    s = raw.strip().strip("*").replace("`", "").strip()
    if s in ("", "—", "·"):
        return None
    plain = s.replace(",", "")
    try:
        return int(plain)
    except ValueError:
        pass
    try:
        return float(plain)
    except ValueError:
        return s


def _is_md_separator(line: str) -> bool:
    """A markdown table's |---|--:|---| alignment row (no letters/digits)."""
    body = line.strip().strip("|")
    return bool(body) and set(body) <= set("-:| ")


def _write_stats_sheet(ws: Worksheet, md_path: Path) -> int:
    """Render every markdown table in `md_path` as a stacked block: its nearest
    heading above as a bold section title, then the table with a styled header
    row. Prose between tables is skipped. Returns the number of tables written.
    """
    lines = md_path.read_text(encoding="utf-8").splitlines()
    heading = ""
    tables = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            i += 1
            continue
        if not line.startswith("|"):
            i += 1
            continue
        # A table block: consecutive `|`-rows; second row is the alignment row.
        block = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            if not _is_md_separator(lines[i]):
                block.append([c for c in lines[i].strip().strip("|").split("|")])
            i += 1
        if not block:
            continue
        if tables:
            ws.append([])  # blank separator between blocks
        ws.append([heading])
        ws.cell(row=ws.max_row, column=1).font = SECTION_FONT
        header_row_idx = ws.max_row + 1
        for j, cells in enumerate(block):
            ws.append([_md_cell(c) for c in cells])
            if j == 0:
                for col_idx in range(1, len(cells) + 1):
                    cell = ws.cell(row=header_row_idx, column=col_idx)
                    cell.font = HEADER_FONT
                    cell.fill = HEADER_FILL
        tables += 1

    ws.column_dimensions["A"].width = 36
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 14
    return tables


def build() -> None:
    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet; we name our own

    for name, path in SHEETS:
        ws = wb.create_sheet(name)
        n = _write_sheet(ws, path)
        if name == "repos" and n:
            _decorate_repos_sheet(ws)
        console.print(f"  [cyan]{name}[/cyan]: {n:,} rows ← {path}")

    ws = wb.create_sheet("stats")
    n = _write_stats_sheet(ws, STATS_MD)
    console.print(f"  [cyan]stats[/cyan]: {n} tables ← {STATS_MD}")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT_FILE)


def main() -> None:
    console.print("[bold]Building preview.xlsx...[/bold]\n")
    build()
    console.print(f"\n[dim]Wrote {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
