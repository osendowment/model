"""Tests for src/common/tables.py — shared wide-CSV loaders."""

import csv

from src.common.tables import load_column_by_repo, load_rows_by_repo


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_load_rows_by_repo_keys_lowercased(tmp_path):
    p = tmp_path / "wide.csv"
    _write(p, ["repo", "stars"], [["Owner/Repo", "10"], ["a/b", "20"]])
    out = load_rows_by_repo(p)
    assert set(out) == {"owner/repo", "a/b"}
    assert out["owner/repo"]["stars"] == "10"


def test_load_rows_by_repo_skips_blank_repo(tmp_path):
    p = tmp_path / "wide.csv"
    _write(p, ["repo", "stars"], [["", "10"], ["  ", "20"], ["a/b", "30"]])
    assert set(load_rows_by_repo(p)) == {"a/b"}


def test_load_rows_by_repo_last_duplicate_wins(tmp_path):
    p = tmp_path / "wide.csv"
    _write(p, ["repo", "stars"], [["a/b", "1"], ["a/b", "2"]])
    assert load_rows_by_repo(p)["a/b"]["stars"] == "2"


def test_load_rows_by_repo_missing_file(tmp_path):
    assert load_rows_by_repo(tmp_path / "nope.csv") == {}


def test_load_column_by_repo_strips_value(tmp_path):
    p = tmp_path / "wide.csv"
    _write(p, ["repo", "host"], [["a/b", " apache "], ["c/d", "lf"]])
    assert load_column_by_repo(p, "host") == {"a/b": "apache", "c/d": "lf"}


def test_load_column_by_repo_blank_cell_kept_as_empty(tmp_path):
    # A blank column cell stays in the dict as "" — callers using
    # .get(repo, "") then can't tell "present-but-empty" from "absent".
    p = tmp_path / "wide.csv"
    _write(p, ["repo", "host"], [["a/b", ""], ["c/d", "lf"]])
    out = load_column_by_repo(p, "host")
    assert out == {"a/b": "", "c/d": "lf"}


def test_load_column_by_repo_missing_file(tmp_path):
    assert load_column_by_repo(tmp_path / "nope.csv", "host") == {}
