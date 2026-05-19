"""Shared loaders for the wide per-repo CSVs the risk builders read.

Every `build_<dimension>.py` joins one or more wide CSVs onto the
risk-scope repo set, keyed by the `repo` column. Before this module each
builder carried its own near-identical `_load_*` reader (`_load_repo_meta`,
`_load_lifetime`, `_load_raw_funding`, `_load_churn`, `_load_column`, …);
these two helpers are the one shared primitive behind all of them.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path


def load_rows_by_repo(path: Path | str) -> dict[str, dict[str, str]]:
    """Read a wide CSV → ``{repo_lowercased: row}``.

    The `repo` column is `.strip().lower()`-normalised; rows with a blank
    `repo` are skipped. On a duplicate repo the last row wins. A missing
    file yields ``{}`` (callers treat that as "no data").
    """
    out: dict[str, dict[str, str]] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if slug:
                out[slug] = row
    return out


def load_column_by_repo(path: Path | str, column: str) -> dict[str, str]:
    """Read a wide CSV → ``{repo_lowercased: row[column]}`` (value stripped).

    Same keying rules as `load_rows_by_repo`. A repo whose `column` cell is
    blank is still included with value ``""`` — callers using
    ``.get(repo, "")`` see "present but empty" and "absent" identically.
    """
    return {
        repo: (row.get(column) or "").strip()
        for repo, row in load_rows_by_repo(path).items()
    }
