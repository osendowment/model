#!/usr/bin/env python3
"""Build data/visibility.csv — visibility metrics per eligible repo.

Reads:
    data/eligibility-data.csv   — eligible repo set
    data/github/repos.csv       — stars, forks, watchers, fetched_at

Writes:
    data/visibility.csv  with columns:
        repo, repo_id, stars, forks, watchers, fetched_at

Period: [most recent] — values reflect GitHub state at the last
`fetch_repo_owner_data` run.

Usage:
    uv run python -m src.pipeline.build_visibility
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.repos import load_eligible_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
REPOS_FILE = DATA_DIR / "github" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "visibility.csv"

FIELDS = ["repo", "repo_id", "stars", "forks", "watchers", "fetched_at"]


def _load_repo_meta() -> dict[str, dict]:
    """Load {repo_lowercased: {stars, forks, watchers, fetched_at}}."""
    out: dict[str, dict] = {}
    if not REPOS_FILE.exists():
        return out
    with open(REPOS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = {
                "stars": (row.get("stars") or "").strip(),
                "forks": (row.get("forks") or "").strip(),
                "watchers": (row.get("watchers") or "").strip(),
                "fetched_at": (row.get("fetched_at") or "").strip(),
            }
    return out


def build() -> list[dict]:
    eligible = load_eligible_repos()
    meta = _load_repo_meta()
    rows: list[dict] = []
    for entry in eligible:
        m = meta.get(entry.repo, {})
        rows.append({
            "repo": entry.repo,
            "repo_id": entry.repo_id,
            "stars": m.get("stars", ""),
            "forks": m.get("forks", ""),
            "watchers": m.get("watchers", ""),
            "fetched_at": m.get("fetched_at", ""),
        })
    return rows


def main() -> None:
    console.print("[bold]Building visibility.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    with_stars = sum(1 for r in rows if r["stars"])
    table = Table(title="[bold]Visibility coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("stars", "forks", "watchers"):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)
    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
