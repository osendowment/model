#!/usr/bin/env python3
"""Build data/preview/preview.xlsx — repos.csv + people.csv + stats as one workbook.

The terminal step of the pipeline: combines the two preview CSVs
(src.build_results -> repos.csv, src.build_people -> people.csv) into a
single spreadsheet for non-technical review, one sheet per CSV ('repos',
'people'). Numeric-looking cells (scores, counts, ids) are written as real
Excel numbers, not text, so sorting/filtering behaves numerically rather
than alphabetically.

A third sheet, 'stats', renders every pipeline count/funnel/coverage table
as stacked blocks — each under its section heading, with the same styled
header row. The tables come straight from the generator
(`scripts/stats.py`, its markdown renderer), computed from the live CSVs at
build time: this sheet IS the single home of the pipeline's numbers.

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
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet
from rich.console import Console

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPOS_CSV = DATA_DIR / "preview" / "repos.csv"
PEOPLE_CSV = DATA_DIR / "preview" / "people.csv"
STATS_SCRIPT = ROOT / "scripts" / "stats.py"
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
    `gh/<n>` → github.com, bare `gl/<n>` → gitlab.com, `gl/<nickname>-<n>` →
    that self-hosted GitLab instance (nickname resolved via
    gitlab_client.HOST_NICKNAMES). Unknown/blank id → no link."""
    if repo_id.startswith("gh/"):
        return f"https://github.com/{repo}"
    if repo_id.startswith("gl/"):
        from src.sources.gitlab.gitlab_client import HOST_NICKNAMES
        rest = repo_id[3:]
        if rest.isdigit():
            return f"https://gitlab.com/{repo}"
        nickname = rest.rsplit("-", 1)[0]
        host = {n: h for h, n in HOST_NICKNAMES.items() if n}.get(nickname)
        return f"https://{host}/{repo}" if host else None
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


def _md_cell(raw: str) -> tuple[int | float | str | None, str | None]:
    """One markdown table cell → (Excel value, number format).

    Strips the markdown dressing (`**bold**`, `` `code` `` — including code
    spans inside prose), then coerces to REAL typed cells so Excel never
    flags "number stored as text": thousands-separated counts become ints
    (format `#,##0`), decimals become floats (`0.00`), and percent strings
    ("97.8%") become fractions with a percent format (`0.0%`, or `0%` when
    the source had no decimals). Prose stays text (no format); a
    blank/placeholder cell becomes an empty cell."""
    s = raw.strip().strip("*").replace("`", "").strip()
    if s in ("", "—", "·"):
        return None, None
    if s.endswith("%"):
        num = s[:-1].replace(",", "").strip()
        try:
            return float(num) / 100.0, ("0.0%" if "." in num else "0%")
        except ValueError:
            return s, None
    plain = s.replace(",", "")
    try:
        return int(plain), "#,##0"
    except ValueError:
        pass
    try:
        return float(plain), "0.00"
    except ValueError:
        return s, None


def _is_md_separator(line: str) -> bool:
    """A markdown table's |---|--:|---| alignment row (no letters/digits)."""
    body = line.strip().strip("|")
    return bool(body) and set(body) <= set("-:| ")


def _stats_markdown() -> str:
    """The stats tables as markdown, straight from the generator.

    `scripts/stats.py` computes every count from the live CSVs; its
    `markdown()` renderer is the one source of truth for the numbers on
    this sheet (the preview stats sheet no longer exists)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_stats_gen", STATS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.markdown(mod.value_stats(), mod.risk_stats(), mod.eligibility_stats())


THIN = Side(style="thin", color="000000")


def _with_sides(cell, **sides) -> None:
    """Merge border sides onto a cell without clobbering the ones it has."""
    b = cell.border
    cell.border = Border(left=sides.get("left", b.left),
                         right=sides.get("right", b.right),
                         top=sides.get("top", b.top),
                         bottom=sides.get("bottom", b.bottom))


def _write_stats_sheet(ws: Worksheet, md_text: str) -> int:
    """Render every markdown table in `md_text` as a stacked block: its nearest
    heading above as a bold section title, then the table with a styled header
    row. Prose between tables is skipped. Returns the number of tables written.

    Layout: gridlines are off — each table carries its own thin box outline.
    Column A stays empty (a gutter, like a document margin); every block
    starts at column B. Numeric/percent cells are written as typed values
    with number formats, and a column whose data is entirely numeric/percent
    gets its header right-aligned to sit over the numbers.

    Row markup (from the generator's markdown):
      - a first cell starting with `^` draws a thin separator line ABOVE the
        row (subsection breaks, total rows);
      - a `**bold**` first cell bolds the whole row (totals / emphasis).
    """
    ws.sheet_view.showGridLines = False
    lines = md_text.splitlines()
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
        ws.append([None, heading])
        ws.cell(row=ws.max_row, column=2).font = SECTION_FONT
        header_row_idx = ws.max_row + 1
        n_cols = max(len(cells) for cells in block)
        numeric_col = [True] * n_cols  # column has ONLY numeric/percent data
        has_data_col = [False] * n_cols
        separator_rows: list[int] = []
        for j, cells in enumerate(block):
            first = cells[0].strip()
            sep = first.startswith("^")
            bold = first.lstrip("^").strip().startswith("**")
            if sep:
                cells = [first.lstrip("^")] + cells[1:]
            parsed = [_md_cell(c) for c in cells]
            ws.append([None] + [v for v, _ in parsed])
            if sep:
                separator_rows.append(ws.max_row)
            for k, (v, fmt) in enumerate(parsed):
                cell = ws.cell(row=ws.max_row, column=k + 2)
                if fmt:
                    cell.number_format = fmt
                if j == 0:
                    cell.font = HEADER_FONT
                    cell.fill = HEADER_FILL
                else:
                    if bold:
                        cell.font = Font(bold=True)
                    if v is not None:
                        has_data_col[k] = True
                        if not isinstance(v, (int, float)):
                            numeric_col[k] = False
        for k in range(n_cols):
            if has_data_col[k] and numeric_col[k]:
                ws.cell(row=header_row_idx, column=k + 2).alignment = \
                    Alignment(horizontal="right")
        # thin box outline around the table + separator lines above marked rows
        last_row = ws.max_row
        first_col, last_col = 2, n_cols + 1
        for row in range(header_row_idx, last_row + 1):
            _with_sides(ws.cell(row=row, column=first_col), left=THIN)
            _with_sides(ws.cell(row=row, column=last_col), right=THIN)
        for col in range(first_col, last_col + 1):
            _with_sides(ws.cell(row=header_row_idx, column=col), top=THIN)
            _with_sides(ws.cell(row=last_row, column=col), bottom=THIN)
            for row in separator_rows:
                _with_sides(ws.cell(row=row, column=col), top=THIN)
        tables += 1

    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 60  # metric labels carry the folded descriptions
    for col in "CDEFGH":
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
    n = _write_stats_sheet(ws, _stats_markdown())
    console.print(f"  [cyan]stats[/cyan]: {n} tables ← scripts/stats.py (live CSVs)")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT_FILE)


def main() -> None:
    console.print("[bold]Building preview.xlsx...[/bold]\n")
    build()
    console.print(f"\n[dim]Wrote {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
