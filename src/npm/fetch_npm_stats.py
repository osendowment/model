#!/usr/bin/env python3
"""Fetch total npm download counts per year and save to data/npm/raw/npm-stats.csv.

Skips years that already have data. npm registry launched in 2010.

Usage:
    uv run python -m src.npm.fetch_npm_stats
"""
from __future__ import annotations

import csv
import os
import logging

import requests
from rich.console import Console
from rich.table import Table

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

console = Console()

API_URL = "https://api.npmjs.org/downloads/point"
START_YEAR = 2015  # npm API data starts Jan 10, 2015
OUTPUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "npm", "raw", "npm-stats.csv")
FIELDS = ["year", "downloads"]


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
    current_year = date.today().year

    years = list(range(START_YEAR, current_year))  # exclude current year
    existing = _load_existing()

    to_fetch = [y for y in years if y not in existing]

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
