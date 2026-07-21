#!/usr/bin/env python3
"""Fetch total npm download counts per year and save to data/sources/npm/raw/npm-stats.csv.

Two gates keep a warm re-run at zero network calls. The whole output file is
TTL-gated on its mtime — it is a complete snapshot, rewritten wholesale on
every run. Inside a run, each year that already holds a count is skipped.
npm registry launched in 2010; its download API serves data from Jan 10, 2015.

Usage:
    uv run python -m src.sources.npm.fetch_npm_stats
    uv run python -m src.sources.npm.fetch_npm_stats --refresh   # ignore the TTL
"""
from __future__ import annotations

import argparse
import csv
import os
import logging
import time

import requests
from rich.console import Console
from rich.table import Table

from src.common.freshness import file_is_fresh
from src.common.params import fetch_ttl_days

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

console = Console()

API_URL = "https://api.npmjs.org/downloads/point"
START_YEAR = 2015  # npm API data starts Jan 10, 2015
OUTPUT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "sources", "npm", "raw", "npm-stats.csv")
FIELDS = ["year", "downloads"]

# Whole-file TTL: 365 days, declared in settings.json. The file holds one total
# per CLOSED year, and a closed year's total never changes, so only a new
# calendar year can make it stale. --refresh overrides it.
TTL_DAYS = fetch_ttl_days("sources/npm/fetch_npm_stats")


def _load_existing() -> dict[int, int]:
    """Load existing year -> downloads mapping."""
    data: dict[int, int] = {}
    if os.path.exists(OUTPUT):
        with open(OUTPUT, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                year = int(row["year"])
                dl = row["downloads"].strip()
                if dl:
                    data[year] = int(dl)
    return data


def _fetch_year(year: int) -> int:
    """Fetch total npm downloads for a given year."""
    url = f"{API_URL}/{year}-01-01:{year}-12-31"
    log.debug("Fetching %s", url)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()["downloads"]


def main():
    from datetime import date

    p = argparse.ArgumentParser(description="Fetch total npm downloads per year")
    p.add_argument("--refresh", action="store_true",
                   help=f"Force refetch, ignoring the {TTL_DAYS}-day output TTL")
    args = p.parse_args()

    current_year = date.today().year

    years = list(range(START_YEAR, current_year))  # exclude current year
    existing = _load_existing()

    to_fetch = [y for y in years if y not in existing]

    # ── TTL gate: a warm run makes ZERO network calls ─────────────────────────
    # "Fresh" means young AND complete. The mtime alone is not enough: when the
    # calendar rolls over, the file is still young but has lost a year, and
    # gating on age alone would leave that year missing for up to a full TTL.
    # A closed year's total never changes, so a complete file needs no work.
    if not args.refresh and not to_fetch and file_is_fresh(OUTPUT, TTL_DAYS):
        age_days = (time.time() - os.path.getmtime(OUTPUT)) / 86400
        console.print(f"[dim]output fresh ({age_days:.1f}d < {TTL_DAYS}d) and "
                      f"complete ({len(years)} years) — skipping; --refresh to force[/]")
        return

    console.print(f"[bold]npm stats:[/] {len(years)} years ({START_YEAR}–{current_year - 1}), "
                  f"{len(existing)} cached, [yellow]{len(to_fetch)} to fetch[/]\n")

    for year in to_fetch:
        dl = _fetch_year(year)
        existing[year] = dl
        log.debug("%d: %s downloads", year, f"{dl:,}")

    # Write all years
    rows = [{"year": y, "downloads": existing[y]} for y in sorted(existing)]
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT)), exist_ok=True)
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    # Display
    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Year", style="dim")
    table.add_column("Downloads", justify="right")
    for r in rows:
        style = "green" if r["year"] in to_fetch else ""
        table.add_row(str(r["year"]), f"{r['downloads']:,}", style=style)
    console.print(table)
    console.print(f"\n[dim]{OUTPUT}[/]")


if __name__ == "__main__":
    main()
