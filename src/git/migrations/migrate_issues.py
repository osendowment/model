"""One-shot migration: data/github/issues/{opened,closed}.csv (wide per-year)
→ data/github/issues.csv (long per (repo, year, metric)).

Source files (read-only):
  - data/github/issues/opened.csv     (repo, 2021..2025)
  - data/github/issues/closed.csv     (repo, 2021..2025)
  - data/eligibility-data.csv         (repo → repo_id lookup)

Destination:
  - data/github/issues.csv with header:
      repo, repo_id, year, metric, value
    where metric ∈ {opened_issues, closed_issues}, one row per non-blank
    cell. Stable sort by (repo, year, metric).

This file is per-year, not sha-pinned, so it does NOT use src.git.long_format
(whose key is (repo, sha, metric)). Stdlib csv only.

Run:
  uv run python -m src.git.migrations.migrate_issues
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[3]
OPENED_CSV = ROOT / "data" / "github" / "issues" / "opened.csv"
CLOSED_CSV = ROOT / "data" / "github" / "issues" / "closed.csv"
ELIGIBILITY_CSV = ROOT / "data" / "eligibility-data.csv"
DEST_CSV = ROOT / "data" / "github" / "issues.csv"

FIELDS = ["repo", "repo_id", "year", "metric", "value"]
SOURCES = (
    (OPENED_CSV, "opened_issues"),
    (CLOSED_CSV, "closed_issues"),
)


def load_repo_ids(path: Path) -> dict[str, str]:
    """Map repo → repo_id from eligibility-data.csv. Empty string if missing."""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip()
            repo_id = (row.get("repo_id") or "").strip()
            if repo:
                out[repo] = repo_id
    return out


def read_wide(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a wide CSV. Returns (year_columns, rows)."""
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        years = [c for c in (reader.fieldnames or []) if c != "repo"]
        rows = list(reader)
    return years, rows


def melt(
    rows: list[dict[str, str]],
    years: list[str],
    metric: str,
    repo_ids: dict[str, str],
) -> tuple[list[dict[str, str]], int, int]:
    """Wide → long. Returns (long_rows, source_cells, blank_cells_skipped)."""
    out: list[dict[str, str]] = []
    source_cells = 0
    blank = 0
    for row in rows:
        repo = (row.get("repo") or "").strip()
        if not repo:
            continue
        for y in years:
            v = (row.get(y) or "").strip()
            source_cells += 1
            if v == "":
                blank += 1
                continue
            out.append({
                "repo": repo,
                "repo_id": repo_ids.get(repo, ""),
                "year": y,
                "metric": metric,
                "value": v,
            })
    return out, source_cells, blank


def main() -> int:
    console = Console()
    console.rule("[bold]migrate_issues[/]")
    console.print(f"src opened     : {OPENED_CSV}")
    console.print(f"src closed     : {CLOSED_CSV}")
    console.print(f"eligibility    : {ELIGIBILITY_CSV}")
    console.print(f"destination    : {DEST_CSV}\n")

    repo_ids = load_repo_ids(ELIGIBILITY_CSV)
    console.print(f"loaded {len(repo_ids)} repo→repo_id mappings\n")

    all_rows: list[dict[str, str]] = []
    per_source_stats: list[tuple[str, int, int, int]] = []  # (name, src_cells, non_blank, written)
    source_lookup: dict[tuple[str, str, str], str] = {}  # (repo, year, metric) → source value

    for path, metric in SOURCES:
        if not path.exists():
            console.print(f"[red]MISSING[/] {path}")
            return 1
        years, rows = read_wide(path)
        long_rows, src_cells, blanks = melt(rows, years, metric, repo_ids)
        per_source_stats.append((metric, src_cells, src_cells - blanks, len(long_rows)))
        all_rows.extend(long_rows)
        for r in long_rows:
            source_lookup[(r["repo"], r["year"], r["metric"])] = r["value"]

    # Stable sort by (repo, year, metric)
    all_rows.sort(key=lambda r: (r["repo"], r["year"], r["metric"]))

    DEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    with DEST_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    # Validate round-trip: every (repo, year, metric) cell with non-blank
    # source value must exist in the long file with the same value.
    with DEST_CSV.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        long_lookup = {(r["repo"], r["year"], r["metric"]): r["value"] for r in reader}

    missing = [k for k, v in source_lookup.items() if long_lookup.get(k) != v]
    extra = [k for k in long_lookup if k not in source_lookup]

    unique_repos = sorted({r["repo"] for r in all_rows})
    years_seen = sorted({r["year"] for r in all_rows})

    table = Table(title="migration stats", show_lines=False)
    table.add_column("metric", style="dim")
    table.add_column("value", justify="right")
    for name, src_cells, non_blank, written in per_source_stats:
        table.add_row(f"{name}: source cells", str(src_cells))
        table.add_row(f"{name}: non-blank", str(non_blank))
        table.add_row(f"{name}: rows written", str(written))
    table.add_row("[bold]total long rows[/]", f"[bold]{len(all_rows)}[/]")
    table.add_row("unique repos", str(len(unique_repos)))
    table.add_row("year coverage", ", ".join(years_seen))
    table.add_row("round-trip mismatches", str(len(missing)))
    table.add_row("extra rows in long", str(len(extra)))
    console.print(table)

    if missing:
        console.print(f"[red]ROUND-TRIP FAIL[/] {len(missing)} cells differ — first 5: {missing[:5]}")
        return 2
    console.print("[green]round-trip OK[/]")

    # First 5 rows of new CSV
    console.print()
    console.rule("[bold]sample (first 5 rows)[/]")
    with DEST_CSV.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            console.print(line.rstrip())
            if i >= 5:
                break

    return 0


if __name__ == "__main__":
    sys.exit(main())
