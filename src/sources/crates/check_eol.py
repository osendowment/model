#!/usr/bin/env python3
"""Check EOL for crates.io packages via the local crates.io DB dump.

Method: a crate is EOL if its default version is yanked. crates.io has no
formal "deprecate" mechanism — `yanked` is the closest registry-level
signal, set by maintainers via `cargo yank`. We use the crate's *default*
version (the latest non-yanked-or-prerelease that crates.io would serve)
because checking only the absolute latest would miss crates that briefly
yank a release and replace it.

Wait — the default version IS the post-yank "current" version. So if the
default version is yanked, every published version of that crate is yanked
(or no usable version exists). That's the strongest possible registry
signal of EOL.

Reads:
    data/sources/crates/results.csv         — crates we care about
    data/sources/crates/db-dump/crates.csv  — crate_id ↔ name
    data/sources/crates/db-dump/default_versions.csv — crate_id → default version_id
    data/sources/crates/db-dump/versions.csv — version_id → yanked

Writes:
    data/sources/crates/eol.csv

Usage:
    uv run python -m src.sources.crates.check_eol
    uv run python -m src.sources.crates.check_eol --limit 100
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

# crates.io readmes can be enormous — bump csv's per-field limit
csv.field_size_limit(sys.maxsize)

from rich.console import Console

from src.eligibility.eol_common import EOL_FIELDS, display_summary, now_iso, write_eol

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
RESULTS_FILE = DATA_DIR / "sources" / "crates" / "results.csv"
DUMP_DIR = DATA_DIR / "sources" / "crates" / "db-dump"
OUTPUT_FILE = DATA_DIR / "sources" / "crates" / "eol.csv"

EOL_METHOD = "crates_yanked"


def load_yank_index() -> dict[str, bool]:
    """Return {crate_name_lowercase: default_version_yanked}."""
    # crate_id -> name
    name_by_id: dict[str, str] = {}
    with open(DUMP_DIR / "crates.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            name_by_id[r["id"]] = r["name"]
    log.info("loaded %d crates", len(name_by_id))

    # crate_id -> default version_id
    default_by_crate: dict[str, str] = {}
    with open(DUMP_DIR / "default_versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            default_by_crate[r["crate_id"]] = r["version_id"]
    log.info("loaded %d default versions", len(default_by_crate))

    # Only fetch yanked status for default versions — versions.csv has 2M+ rows
    needed_versions = set(default_by_crate.values())
    yanked_by_version: dict[str, bool] = {}
    with open(DUMP_DIR / "versions.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["id"] in needed_versions:
                yanked_by_version[r["id"]] = r["yanked"] == "t"
    log.info("loaded yanked status for %d default versions", len(yanked_by_version))

    out: dict[str, bool] = {}
    for crate_id, version_id in default_by_crate.items():
        name = name_by_id.get(crate_id)
        if name is None:
            continue
        out[name.lower()] = yanked_by_version.get(version_id, False)
    return out


def load_packages(limit: int | None) -> list[str]:
    pkgs: list[str] = []
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkgs.append(r["package"])
            if limit and len(pkgs) >= limit:
                break
    return pkgs


def build_rows(packages: list[str], yank_idx: dict[str, bool]) -> list[dict]:
    checked_at = now_iso()
    rows = []
    for p in packages:
        yanked = yank_idx.get(p.lower())
        if yanked is None:
            rows.append({
                "package": p,
                "is_eol": False,
                "eol_method": EOL_METHOD,
                "eol_reason": "not in db dump",
                "source": "not_found",
                "eol_checked_at": checked_at,
            })
        else:
            rows.append({
                "package": p,
                "is_eol": yanked,
                "eol_method": EOL_METHOD,
                "eol_reason": "default version yanked" if yanked else "",
                "source": "db-dump",
                "eol_checked_at": checked_at,
            })
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None, help="limit packages")
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]crates.check_eol[/bold]  limit={args.limit or '∞'}  "
                  f"started={started:%Y-%m-%d %H:%M:%S}")

    pkgs = load_packages(args.limit)
    log.info("loaded %d crates from results.csv", len(pkgs))

    yank_idx = load_yank_index()
    rows = build_rows(pkgs, yank_idx)
    write_eol(OUTPUT_FILE, rows)

    display_summary(console, "crates", rows)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(rows)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
