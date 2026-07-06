"""Tests for src/build_preview_workbook.py — repos/people/stats -> preview.xlsx."""
import csv

from openpyxl import load_workbook

from src import build_preview_workbook as bpw

_STATS_MD = """# Pipeline Statistics

Some prose that must be skipped.

## Value

### Repo identity coverage

| Step | A | Total | Comment |
|---|--:|--:|---|
| Packages | 3,425 | 17,647 | package universe |
| **Valid repos** | **947** | **11,378** | upstream resolves |

more prose

### Coverage

| Signal | Filled | Pct |
|---|--:|--:|
| `openssf_crit` | 921 | 97.8% |
"""


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _write_stats_md(tmp_path):
    p = tmp_path / "stats.md"
    p.write_text(_STATS_MD, encoding="utf-8")
    return p


def test_cell_value_coerces_numbers_and_keeps_ids_as_text():
    assert bpw._cell_value("") is None
    assert bpw._cell_value("1") == 1
    assert bpw._cell_value("88.00") == 88.0
    assert bpw._cell_value("gh/61137153") == "gh/61137153"
    assert bpw._cell_value("True") == "True"


def test_build_writes_two_named_sheets_with_styled_filtered_headers(tmp_path, monkeypatch):
    repos_csv = tmp_path / "repos.csv"
    people_csv = tmp_path / "people.csv"
    out = tmp_path / "preview.xlsx"
    _write_csv(repos_csv, ["repo_id", "repo", "risk_score"],
               [["gh/1", "a/keep", "88.00"], ["gh/2", "b/drop", "77.00"]])
    _write_csv(people_csv, ["person_id", "platform", "login"],
               [["github/1", "github", "octocat"]])
    monkeypatch.setattr(bpw, "SHEETS", [("repos", repos_csv), ("people", people_csv)])
    monkeypatch.setattr(bpw, "STATS_MD", _write_stats_md(tmp_path))
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    wb = load_workbook(out)
    assert wb.sheetnames == ["repos", "people", "stats"]

    ws = wb["repos"]
    assert ws.max_row == 3          # header + 2 data rows
    assert ws.max_column == 3
    # header styling: bold white font on a filled (non-default) background.
    header_cell = ws.cell(row=1, column=1)
    assert header_cell.font.bold is True
    assert header_cell.fill.start_color.rgb == "001F3864"
    # data rows are NOT styled like the header.
    data_cell = ws.cell(row=2, column=1)
    assert data_cell.font.bold is not True
    # numeric column written as a real number, not text.
    assert ws.cell(row=2, column=3).value == 88.0
    # id column stays text.
    assert ws.cell(row=2, column=1).value == "gh/1"
    # AutoFilter covers the full used range, and the header row is frozen.
    assert ws.auto_filter.ref == "A1:C3"
    assert ws.freeze_panes == "A2"

    ws_people = wb["people"]
    assert ws_people.max_row == 2
    assert ws_people.cell(row=2, column=3).value == "octocat"


def test_build_skips_missing_csv_without_error(tmp_path, monkeypatch):
    repos_csv = tmp_path / "repos.csv"
    missing_csv = tmp_path / "does-not-exist.csv"
    out = tmp_path / "preview.xlsx"
    _write_csv(repos_csv, ["repo_id"], [["gh/1"]])
    monkeypatch.setattr(bpw, "SHEETS", [("repos", repos_csv), ("people", missing_csv)])
    monkeypatch.setattr(bpw, "STATS_MD", _write_stats_md(tmp_path))
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    wb = load_workbook(out)
    assert wb.sheetnames == ["repos", "people", "stats"]
    assert wb["people"].max_row == 1   # empty sheet, no header written


def test_repo_url_per_platform():
    assert bpw._repo_url("a/b", "gh/123") == "https://github.com/a/b"
    assert bpw._repo_url("a/b", "gl/456") == "https://gitlab.com/a/b"
    assert (bpw._repo_url("mpc/mpc", "gl/inria-22470")
            == "https://gitlab.inria.fr/mpc/mpc")
    assert bpw._repo_url("a/b", "gl/unknown-1") is None
    assert bpw._repo_url("a/b", "") is None


def test_repos_sheet_hyperlinks_and_decision_column_fills(tmp_path, monkeypatch):
    repos_csv = tmp_path / "repos.csv"
    out = tmp_path / "preview.xlsx"
    _write_csv(repos_csv,
               ["repo", "value_score", "risk_score", "eligible", "score", "repo_id"],
               [["a/keep", "70.00", "80.00", "True", "90.00", "gh/1"],
                ["c/lab", "60.00", "70.00", "False", "80.00", "gl/debian-9"]])
    monkeypatch.setattr(bpw, "SHEETS", [("repos", repos_csv)])
    monkeypatch.setattr(bpw, "STATS_MD", _write_stats_md(tmp_path))
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    ws = load_workbook(out)["repos"]
    assert ws.cell(row=2, column=1).hyperlink.target == "https://github.com/a/keep"
    assert ws.cell(row=3, column=1).hyperlink.target == "https://salsa.debian.org/c/lab"
    # decision columns carry their light fills; plain columns don't.
    assert ws.cell(row=2, column=2).fill.start_color.rgb == "00D9E1F2"  # value_score
    assert ws.cell(row=2, column=3).fill.start_color.rgb == "00FCE4D6"  # risk_score
    assert ws.cell(row=2, column=4).fill.start_color.rgb == "00E4DFEC"  # eligible
    assert ws.cell(row=2, column=5).fill.start_color.rgb == "00E2EFDA"  # score
    assert ws.cell(row=2, column=6).fill.fill_type is None              # repo_id plain


def test_stats_sheet_renders_markdown_tables(tmp_path, monkeypatch):
    """Every stats.md table lands as a stacked block: bold section heading,
    styled header row, markdown dressing stripped, counts as real numbers."""
    repos_csv = tmp_path / "repos.csv"
    out = tmp_path / "preview.xlsx"
    _write_csv(repos_csv, ["repo_id"], [["gh/1"]])
    monkeypatch.setattr(bpw, "SHEETS", [("repos", repos_csv)])
    monkeypatch.setattr(bpw, "STATS_MD", _write_stats_md(tmp_path))
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    ws = load_workbook(out)["stats"]
    got = [[c.value for c in row] for row in ws.iter_rows()]
    # column A is an empty gutter — every block starts at column B
    assert all(row[0] is None for row in got)
    # block 1: heading, header, two data rows (bold + thousands stripped)
    assert got[0][1] == "Repo identity coverage"
    assert ws.cell(row=1, column=2).font.bold is True
    assert got[1][1:5] == ["Step", "A", "Total", "Comment"]
    assert ws.cell(row=2, column=2).fill.start_color.rgb == "001F3864"
    assert got[2][1:4] == ["Packages", 3425, 17647]
    assert got[3][1:4] == ["Valid repos", 947, 11378]     # **bold** stripped
    # numeric columns: real numbers with thousands format, header right-aligned
    assert ws.cell(row=3, column=3).number_format == "#,##0"
    assert ws.cell(row=2, column=3).alignment.horizontal == "right"   # "A"
    assert ws.cell(row=2, column=2).alignment.horizontal != "right"   # "Step"
    assert ws.cell(row=2, column=5).alignment.horizontal != "right"   # "Comment"
    # blank separator, then block 2 under its own heading (`code` stripped)
    assert got[4] == [None] * len(got[4])
    assert got[5][1] == "Coverage"
    assert got[7][1:3] == ["openssf_crit", 921]
    # percent cells become real fractions with a percent format
    pct = ws.cell(row=8, column=4)
    assert pct.value == 0.978 and pct.number_format == "0.0%"
