#!/usr/bin/env python3
"""Aggregate contributor metrics into risk classifications per repo.

Reads data/github/repo-contrib-metrics.csv and writes data/risk-metrics.csv
with columns: repo, active_contributors, hhi_commits, bus_factor_commits,
est_locs, concentration_class, complexity_class.

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
LOCS_FILE = DATA_DIR / "github" / "locs.csv"
OUTPUT_FILE = DATA_DIR / "risk-metrics.csv"
AGG_COL = "2021-2025"

REPOS_FILE = DATA_DIR / "github" / "top-repos.csv"
FIELDS = ["repo", "repo_id", "active_contributors", "hhi_commits", "bus_factor_commits",
          "est_locs", "concentration_class", "complexity_class"]


def concentration_class(bus_factor: int, hhi: int) -> str:
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


def complexity_class(est_locs: int | None) -> str:
    """Classify repo complexity based on estimated lines of code.

    A (massive):   1M+ LOC   — enormous codebase, high cognitive load
    B (large):     100K–1M   — significant, requires dedicated team
    C (moderate):  10K–100K  — typical project, manageable but non-trivial
    D (small):     <10K LOC  — easy to audit and understand
    """
    if est_locs is None or est_locs == 0:
        return ""
    if est_locs >= 1_000_000:
        return "A"
    if est_locs >= 100_000:
        return "B"
    if est_locs >= 10_000:
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


def _load_locs() -> dict[str, int]:
    """Load repo → est_locs mapping from locs.csv."""
    mapping: dict[str, int] = {}
    if LOCS_FILE.exists():
        with open(LOCS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("est_locs"):
                    mapping[row["repo"]] = int(row["est_locs"])
    return mapping


def aggregate() -> tuple[list[dict], int]:
    """Read contributor metrics and return risk-classified rows."""
    repo_ids = _load_repo_ids()
    repo_locs = _load_locs()
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
        locs = repo_locs.get(repo)

        rows.append({
            "repo": repo,
            "repo_id": repo_ids.get(repo, ""),
            "active_contributors": contributors,
            "hhi_commits": hhi,
            "bus_factor_commits": bf,
            "est_locs": locs if locs is not None else "",
            "concentration_class": concentration_class(bf, hhi),
            "complexity_class": complexity_class(locs),
        })

    return rows, skipped


CONCENTRATION_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

CONCENTRATION_CRITERIA = {
    "A": "BF=1, HHI≥8000",
    "B": "BF≤2, HHI≥5000",
    "C": "BF≤4, HHI≥2500",
    "D": "otherwise",
}

COMPLEXITY_LABELS = {
    "A": ("massive", "red"),
    "B": ("large", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("small", "green"),
}

COMPLEXITY_CRITERIA = {
    "A": "≥1M LOC",
    "B": "100K–1M",
    "C": "10K–100K",
    "D": "<10K LOC",
}


def _print_class_table(
    title: str, rows: list[dict], field: str,
    labels: dict, criteria: dict,
) -> None:
    """Print a classification distribution table."""
    total = len(rows)
    dist = Counter(r[field] for r in rows if r[field])

    table = Table(title=f"[bold]{title}[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Class", style="dim")
    table.add_column("", min_width=10)
    table.add_column("Criteria", style="dim")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")

    for cls in "ABCD":
        label, color = labels[cls]
        n = dist.get(cls, 0)
        pct = 100 * n / total if total else 0
        table.add_row(f"[{color} bold]{cls}[/{color} bold]",
                      f"[{color}]{label}[/{color}]",
                      criteria[cls],
                      f"{n:,}", f"{pct:.1f}%")

    classified = sum(dist.values())
    unclassified = total - classified
    table.add_section()
    table.add_row("[bold]–[/bold]", "[bold]classified[/bold]", "", f"[bold]{classified:,}[/bold]", "")
    if unclassified:
        table.add_row("", f"[dim]no data[/dim]", "", f"[dim]{unclassified:,}[/dim]", "")

    console.print(table)


def main():
    console.print("[bold]Aggregating risk metrics...[/bold]\n")
    rows, skipped = aggregate()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _print_class_table("Concentration", rows, "concentration_class",
                       CONCENTRATION_LABELS, CONCENTRATION_CRITERIA)
    console.print()
    _print_class_table("Complexity", rows, "complexity_class",
                       COMPLEXITY_LABELS, COMPLEXITY_CRITERIA)

    total = len(rows)
    console.print()
    if skipped:
        console.print(f"[dim]{skipped:,} repos skipped (missing BF/HHI)[/dim]")
    console.print(f"[dim]Written {total:,} repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
