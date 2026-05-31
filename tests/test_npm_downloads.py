"""Integration test: verify npm/top-packages.csv download counts against the npm API."""

import csv
import random

import httpx

DATA_FILE = "data/sources/npm/top-packages.csv"


def _load_packages() -> tuple[list[dict], list[str]]:
    with open(DATA_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        years = [col for col in (reader.fieldnames or []) if col.isdigit()]
    return rows, years


def test_npm_downloads_match_csv():
    """Pick a random package+year, fetch from npm API, assert download count matches CSV."""
    rows, years = _load_packages()
    assert rows, "top-packages.csv is empty"
    assert years, "No year columns found in top-packages.csv"

    # Pick a random row and year with a non-empty count
    rng = random.Random()
    candidates = [
        (row, year)
        for row in rows
        for year in years
        if row.get(year, "").strip()
    ]
    assert candidates, "No valid package/year combinations found"

    row, year = rng.choice(candidates)
    package = row["package"]
    expected = int(row[year])
    start, end = f"{year}-01-01", f"{year}-12-31"

    url = f"https://api.npmjs.org/downloads/point/{start}:{end}/{package}"
    response = httpx.get(url, timeout=15)
    assert response.status_code == 200, f"npm API returned {response.status_code} for {package}"

    data = response.json()
    assert "downloads" in data, f"Unexpected API response: {data}"
    actual = data["downloads"]

    assert actual == expected, (
        f"Download mismatch for {package} in {year}: "
        f"CSV={expected:,}, npm API={actual:,}"
    )
