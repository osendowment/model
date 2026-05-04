#!/usr/bin/env python3
"""Extract per-check sub-scores from data/openssf/data.json into a wide CSV.

OpenSSF Scorecard returns a top-level aggregate `score` (0-10) plus per-check
scores for ~18 individual checks (Maintained, Code-Review, CI-Tests,
Branch-Protection, Vulnerabilities, etc.). The aggregate is in the
sha-pinned long file `data/git/openssf.csv`; the per-check breakdown also
lives in `data.json`.

This script flattens the JSON into a wide CSV so downstream pipeline stages
can join per-check scores by repo without parsing JSON.

Reads:
    data/openssf/data.json   {repo: {checks: [{name, score}, ...], ...}}

Writes:
    data/openssf/checks.csv  with columns:
        repo, score, <Check-Name-1>, <Check-Name-2>, ..., date

Where each check column is the integer score 0..10, or `-1` (the upstream
sentinel for "not applicable / unable to evaluate"), or empty if the check
wasn't run for that repo.

Usage:
    uv run python -m src.openssf.extract_checks
"""

import csv
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
INPUT_FILE = DATA_DIR / "openssf" / "data.json"
OUTPUT_FILE = DATA_DIR / "openssf" / "checks.csv"

# Standard scorecard checks (stable ordering for the wide CSV).
CHECK_NAMES = [
    "Binary-Artifacts",
    "Branch-Protection",
    "CI-Tests",
    "CII-Best-Practices",
    "Code-Review",
    "Contributors",
    "Dangerous-Workflow",
    "Dependency-Update-Tool",
    "Fuzzing",
    "License",
    "Maintained",
    "Packaging",
    "Pinned-Dependencies",
    "SAST",
    "Security-Policy",
    "Signed-Releases",
    "Token-Permissions",
    "Vulnerabilities",
    "Webhooks",
]


def build() -> list[dict]:
    if not INPUT_FILE.exists():
        raise SystemExit(f"missing {INPUT_FILE}")
    with open(INPUT_FILE, encoding="utf-8") as f:
        data = json.load(f)

    rows: list[dict] = []
    for repo_key, payload in data.items():
        if not isinstance(payload, dict):
            continue
        slug = repo_key.strip().lower()
        # The repo key is already in `owner/name` form.
        per_check = {c.get("name"): c.get("score") for c in payload.get("checks", [])}
        row = {
            "repo": slug,
            "score": payload.get("score", ""),
            "date": payload.get("date", ""),
        }
        for name in CHECK_NAMES:
            v = per_check.get(name)
            row[name] = "" if v is None else v
        rows.append(row)
    rows.sort(key=lambda r: r["repo"])
    return rows


def main() -> None:
    console.print("[bold]Extracting OpenSSF per-check scores...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fields = ["repo", "score", *CHECK_NAMES, "date"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Per-check coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Check", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for name in CHECK_NAMES:
        n = sum(1 for r in rows if r[name] not in ("", None))
        pct = 100 * n / total if total else 0
        table.add_row(name, f"{n:,}", f"{pct:.1f}%")
    console.print(table)
    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
