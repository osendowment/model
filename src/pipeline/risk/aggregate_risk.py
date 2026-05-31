#!/usr/bin/env python3
"""Stage 3 of the risk pipeline — the overall risk table.

Each dimension builder writes a per-component CSV whose `score` column is that
dimension's risk score (integer 0-100, higher = riskier). This aggregator joins
the five component scores per repo and computes the overall `score` =
geometric mean of the available component scores.

Writes data/risk/risk.csv with ONLY:
    repo, repo_id, concentration, complexity, security, funding, workload, score

(All other per-metric columns stay in the component CSVs.)

Usage:
    uv run python -m src.pipeline.risk.aggregate_risk
    uv run python -m src.pipeline.risk.aggregate_risk --random 5
"""

import argparse
import csv
import random
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.stats import geometric_mean

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"

# Component name (= its risk.csv column) → per-dimension CSV.
COMPONENTS = {
    "concentration": DATA_DIR / "risk" / "concentration.csv",
    "complexity":    DATA_DIR / "risk" / "complexity.csv",
    "security":      DATA_DIR / "risk" / "security.csv",
    "funding":       DATA_DIR / "risk" / "funding.csv",
    "workload":      DATA_DIR / "risk" / "workload.csv",
}
OUTPUT_FILE = DATA_DIR / "risk" / "risk.csv"
FIELDS = ["repo", "repo_id", *COMPONENTS, "score"]


def _scores_by_repo(path: Path) -> dict[str, int]:
    """{repo_lowercased: int(score)} from a component CSV; blanks skipped."""
    out: dict[str, int] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            s = (row.get("score") or "").strip()
            if repo and s:
                try:
                    out[repo] = int(float(s))
                except ValueError:
                    continue
    return out


def overall_score(component_scores: list[int]) -> str:
    """Overall risk score = geometric mean of present component scores (int)."""
    if not component_scores:
        return ""
    return str(max(1, int(round(geometric_mean([float(s) for s in component_scores])))))


def aggregate(sample: set[str] | None = None) -> list[dict]:
    eligible = load_risk_repos()
    by_component = {name: _scores_by_repo(path) for name, path in COMPONENTS.items()}

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        if sample is not None and repo not in sample:
            continue
        row = {"repo": repo, "repo_id": entry.repo_id}
        present: list[int] = []
        for name, scores in by_component.items():
            s = scores.get(repo)
            row[name] = "" if s is None else s
            if s is not None:
                present.append(s)
        row["score"] = overall_score(present)
        rows.append(row)
    return rows


def _print_coverage(rows: list[dict]) -> None:
    total = len(rows)
    table = Table(title="[bold]Risk score coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Column", style="bold")
    table.add_column("Scored", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (*COMPONENTS, "score"):
        n = sum(1 for r in rows if str(r.get(col, "")) != "")
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--random", type=int, default=None, metavar="N")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    sample = None
    if args.random:
        random.seed(args.seed)
        allslugs = [e.repo for e in load_risk_repos()]
        sample = set(random.sample(allslugs, min(args.random, len(allslugs))))

    rows = aggregate(sample=sample)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    _print_coverage(rows)
    console.print(f"\n[dim]Wrote {len(rows):,} repos × {len(FIELDS)} columns → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
