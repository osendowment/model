"""Tests for src/build_preview_workbook.py — repos.csv + people.csv -> preview.xlsx."""
import csv

from openpyxl import load_workbook

from src import build_preview_workbook as bpw


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


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
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    wb = load_workbook(out)
    assert wb.sheetnames == ["repos", "people"]

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
    monkeypatch.setattr(bpw, "OUTPUT_FILE", out)

    bpw.build()

    wb = load_workbook(out)
    assert wb.sheetnames == ["repos", "people"]
    assert wb["people"].max_row == 1   # empty sheet, no header written
