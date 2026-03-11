#!/usr/bin/env python3
"""Aggregate contributor metrics into risk classifications per repo.

Reads data/github/repo-contrib-metrics.csv and writes data/risk-metrics.csv
with columns: repo, active_contributors, hhi_commits, bus_factor_commits, risk_class.

Usage:
    python -m src.risk
"""

import csv
from collections import Counter, defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
METRICS_FILE = DATA_DIR / "github" / "repo-contrib-metrics.csv"
OUTPUT_FILE = DATA_DIR / "risk-metrics.csv"
AGG_COL = "2021-2025"

REPOS_FILE = DATA_DIR / "github" / "top-repos.csv"
FIELDS = ["repo", "repo_id", "active_contributors", "hhi_commits", "bus_factor_commits", "risk_class"]


def concentration_risk_class(bus_factor: int, hhi: int) -> str:
    """Classify repo concentration risk as A/B/C/D.

    A (critical):  BF=1  and HHI >= 8000 — single maintainer, extreme concentration
    B (high risk): BF<=2 and HHI >= 5000 — tiny core, high concentration
    C (moderate):  BF<=4 and HHI >= 2500 — small team, moderate concentration
    D (healthy):   everything else        — distributed enough
    """
    if bus_factor == 1 and hhi >= 8000:
        return "A"
    if bus_factor <= 2 and hhi >= 5000:
        return "B"
    if bus_factor <= 4 and hhi >= 2500:
        return "C"
    return "D"


def _load_repo_ids() -> dict[str, str]:
    """Load repo slug → repo_id mapping from top-repos.csv."""
    mapping: dict[str, str] = {}
    if REPOS_FILE.exists():
        with open(REPOS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mapping[row["repo"]] = row["repo_id"]
    return mapping


def aggregate() -> tuple[list[dict], int]:
    """Read contributor metrics and return risk-classified rows."""
    repo_ids = _load_repo_ids()
    repo_metrics: dict[str, dict[str, str]] = defaultdict(dict)

    with open(METRICS_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            repo = row["github_repo"]
            metric = row["metric"]
            value = row.get(AGG_COL, "")
            if value:
                repo_metrics[repo][metric] = value

    rows = []
    skipped = 0
    for repo, metrics in sorted(repo_metrics.items()):
        bf_str = metrics.get("bus_factor", "")
        hhi_str = metrics.get("hhi", "")
        contributors_str = metrics.get("contributors", "")

        if not bf_str or not hhi_str:
            skipped += 1
            continue

        bf = int(bf_str)
        hhi = int(hhi_str)
        contributors = int(contributors_str) if contributors_str else 0
        risk_class = concentration_risk_class(bf, hhi)

        rows.append({
            "repo": repo,
            "repo_id": repo_ids.get(repo, ""),
            "active_contributors": contributors,
            "hhi_commits": hhi,
            "bus_factor_commits": bf,
            "risk_class": risk_class,
        })

    return rows, skipped


RISK_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

RISK_CRITERIA = {
    "A": "BF=1, HHI≥8000",
    "B": "BF≤2, HHI≥5000",
    "C": "BF≤4, HHI≥2500",
    "D": "otherwise",
}


def main():
    console.print("[bold]Aggregating risk metrics...[/bold]\n")
    rows, skipped = aggregate()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    dist = Counter(r["risk_class"] for r in rows)

    table = Table(title="[bold]Risk Classification[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Class", style="dim")
    table.add_column("", min_width=10)
    table.add_column("Criteria", style="dim")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")

    for cls in "ABCD":
        label, color = RISK_LABELS[cls]
        n = dist.get(cls, 0)
        pct = 100 * n / total if total else 0
        table.add_row(f"[{color} bold]{cls}[/{color} bold]",
                      f"[{color}]{label}[/{color}]",
                      RISK_CRITERIA[cls],
                      f"{n:,}", f"{pct:.1f}%")

    table.add_section()
    table.add_row("[bold]–[/bold]", "[bold]classified[/bold]", "", f"[bold]{total:,}[/bold]", "")
    if skipped:
        table.add_row("", f"[dim]skipped[/dim]", "", f"[dim]{skipped:,}[/dim]", "")

    console.print(table)
    console.print()
    console.print(f"[dim]Written {total:,} repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
