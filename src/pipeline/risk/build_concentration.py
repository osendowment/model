#!/usr/bin/env python3
"""Build data/concentration.csv — contributor-concentration metrics per risk-scope repo.

Reads:
    data/value-data.csv                                 — A/B value-class set
    data/concentration-data.csv                         — lifetime totals (BF/HHI also,
                                                          but we override BF/HHI w/ 5y)
    data/github/contributors/hhi.csv                    — wide per-year + 2021-2025
    data/github/contributors/bus-factor.csv             — wide per-year + 2021-2025
    data/github/contributors/contributors.csv           — wide per-year + 2021-2025
    data/github/contributors/commits.csv                — wide per-year + 2021-2025

Writes:
    data/concentration.csv  with columns:
        repo, repo_id,
        total_commits_lifetime,         (lifetime, /commits Link header)
        total_contributors_lifetime,    (lifetime, /contributors?anon=true Link header)
        commits_lifetime,                     (sum of 2021-2025 from contributors fetcher)
        contributors_lifetime,                (distinct contributors with any commit in 2021-2025)
        hhi_commits_lifetime,                 (HHI 0-10000 over 2021-2025 commits)
        bf_commits_lifetime,                  (bus factor over 2021-2025 commits)
        fetched_at

Periods:
    `*_lifetime` — entire repo history at fetch time (≈ "now").

The roadmap targets `[2025 EOY]` for total_commits / total_contributors
and `[2021–2025]` for hhi/bf. Our current contributors fetcher uses
`/contributors` (lifetime contribution counts per contributor) — not
`/stats/contributors` (per-week, time-bounded), which is documented to
return 202-forever for ~90% of repos. As a result, every metric here
is **lifetime aggregate at fetch time**. For repos created post-2020
this is effectively the 5y window; for older repos it includes earlier
contributors. Documented gap.

Usage:
    uv run python -m src.pipeline.risk.build_concentration
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
LIFETIME_FILE = DATA_DIR / "concentration-data.csv"
CONTRIB_DIR = DATA_DIR / "github" / "contributors"
HHI_FILE = CONTRIB_DIR / "hhi.csv"
BF_FILE = CONTRIB_DIR / "bus-factor.csv"
CONTRIB_FILE = CONTRIB_DIR / "contributors.csv"
COMMITS_FILE = CONTRIB_DIR / "commits.csv"
OUTPUT_FILE = DATA_DIR / "concentration.csv"

AGG_COL = "2021-2025"  # column name in wide CSVs; data is actually lifetime

FIELDS = [
    "repo", "repo_id",
    "total_commits_lifetime", "total_contributors_lifetime",
    "hhi_commits_lifetime", "bf_commits_lifetime",
    "fetched_at",
]


def _load_lifetime() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not LIFETIME_FILE.exists():
        return out
    with open(LIFETIME_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_agg_column(path: Path) -> dict[str, str]:
    """Read the `2021-2025` (aggregate) column from a wide per-year CSV.
    Despite the column name, the underlying data is currently lifetime —
    see module docstring."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            v = (row.get(AGG_COL) or "").strip()
            if v:
                out[slug] = v
    return out


def build() -> list[dict]:
    eligible = load_risk_repos()
    lifetime = _load_lifetime()
    hhi_agg = _load_agg_column(HHI_FILE)
    bf_agg = _load_agg_column(BF_FILE)

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        lt = lifetime.get(repo, {})
        # Prefer the wide-CSV aggregate values (computed by the contributors
        # fetcher with full pagination); fall back to concentration-data.csv's
        # in-line `bus_factor`/`hhi` (computed at the same time, same source —
        # they should agree).
        hhi_val = hhi_agg.get(repo) or (lt.get("hhi") or "").strip()
        bf_val = bf_agg.get(repo) or (lt.get("bus_factor") or "").strip()
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "total_commits_lifetime": (lt.get("total_commits") or "").strip(),
            "total_contributors_lifetime": (lt.get("total_contributors") or "").strip(),
            "hhi_commits_lifetime": hhi_val,
            "bf_commits_lifetime": bf_val,
            "fetched_at": (lt.get("fetched_at") or "").strip(),
        })
    return rows


def main() -> None:
    console.print("[bold]Building concentration.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Concentration coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (
        "total_commits_lifetime", "total_contributors_lifetime",
        "hhi_commits_lifetime", "bf_commits_lifetime",
    ):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)
    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
