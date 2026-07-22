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


def load_rows_by_id(path: Path | str) -> dict[str, dict[str, str]]:
    """Read a wide CSV → ``{repo_id: row}``, keyed by GitHub's stable numeric id.

    This is the rename-proof analogue of `load_rows_by_repo`: joining on
    `repo_id` means a renamed/moved repo (`facebook/react` → `react/react`) still
    matches its already-collected data. Use it for every join keyed on the repo's
    GitHub identity; keep `load_rows_by_repo` (or an external key) only where
    repo_id is irrelevant — a funding match on another platform (OpenCollective
    slug, outbound sponsoring by owner login, a FLOSS-manifest URL).

    Rows with a blank `repo_id` are skipped; last row wins on a duplicate id.
    """
    out: dict[str, dict[str, str]] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if rid:
                out[rid] = row
    return out


def load_column_by_id(path: Path | str, column: str) -> dict[str, str]:
    """Read a wide CSV → ``{repo_id: row[column]}`` (value stripped). The
    repo_id-keyed analogue of `load_column_by_repo`."""
    return {
        rid: (row.get(column) or "").strip()
        for rid, row in load_rows_by_id(path).items()
    }


def merge_preserved_columns(
    path: Path | str,
    fieldnames: list[str],
    rows: list[dict],
    key: str = "package",
) -> tuple[list[dict], list[str]]:
    """Carry columns an existing CSV has but this writer does not produce.

    A `process_data` step rebuilds `results.csv` from raw data and knows only
    its own columns. Later value-stage steps then enrich the same file in
    place — `git` and `eco_guess` from build_git_urls, `repo_id` and
    `canonical_url` from apply_ecosystems_authority, `license` from the
    per-ecosystem fetch_licenses. Rewriting with a fixed column list silently
    dropped every one of them, so re-running a single `process` step outside
    the full pipeline left `results.csv` short of the columns its consumers
    require, and `build_licenses` fell back to weaker sources.

    Columns already present in `fieldnames` are NOT touched: the freshly
    computed value always wins. Only genuinely extra columns are carried, and
    only onto rows whose `key` still exists — a package that left the set
    takes its enrichment with it.

    Returns `(rows, fieldnames)` with the preserved columns appended.
    """
    p = Path(path)
    if not p.is_file():
        return rows, fieldnames
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_cols = [c for c in (reader.fieldnames or []) if c not in fieldnames]
        if not existing_cols:
            return rows, fieldnames
        preserved = {
            r[key]: {c: r.get(c, "") for c in existing_cols}
            for r in reader if r.get(key)
        }
    for row in rows:
        extra = preserved.get(row.get(key), {})
        for c in existing_cols:
            row.setdefault(c, extra.get(c, ""))
    return rows, fieldnames + existing_cols
