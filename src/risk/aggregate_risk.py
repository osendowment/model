#!/usr/bin/env python3
"""Stage 3 of the risk pipeline — the overall risk table.

Each dimension builder writes a per-component CSV whose `score` column is that
dimension's risk score (0-100, two decimals, higher = riskier). This aggregator
joins the four component scores per repo and computes the overall `risk_score` =
geometric mean of the four component scores (floored to 1, two decimals).

**Completeness rule:** the overall `risk_score` is calculable only if *every*
one of the four component scores is present. A partial geometric mean is not
comparable across repos, so a repo missing any component score is left with a
blank overall `risk_score`. `scripts/pipeline_health.py` enforces it.

Writes data/risk/risk.csv with:
    repo, repo_id, concentration, complexity, security, workload, risk_score

Funding signals (`intent` / `nonprofit`) are no longer part of the risk table —
they live in the eligibility stage (data/eligibility/funding.csv, rolled up
into data/eligibility/eligibility.csv by src.eligibility.build_eligibility).

(All other per-metric columns stay in the component CSVs.)

Usage:
    uv run python -m src.risk.aggregate_risk
    uv run python -m src.risk.aggregate_risk --random 5
"""

import argparse
import csv
import random
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_top_repos
from src.common.stats import geometric_mean

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# Component name (= its risk.csv column) → per-dimension CSV. Order is the
# column order in risk.csv: concentration · complexity · security · workload.
COMPONENTS = {
    "concentration": DATA_DIR / "risk" / "concentration.csv",
    "complexity":    DATA_DIR / "risk" / "complexity.csv",
    "security":      DATA_DIR / "risk" / "security.csv",
    "workload":      DATA_DIR / "risk" / "workload.csv",
}
OUTPUT_FILE = DATA_DIR / "risk" / "risk.csv"
FIELDS = ["repo", "repo_id", *COMPONENTS, "risk_score"]

def _scores_by_repo(path: Path) -> dict[str, float]:
    """{repo_id: float(score)} from a component CSV; blanks skipped."""
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()  # join on stable id, not name
            s = (row.get("score") or "").strip()
            if rid and s:
                try:
                    out[rid] = float(s)
                except ValueError:
                    continue
    return out


def overall_score(component_scores: list[float]) -> str:
    """Overall risk score = geometric mean of present component scores, xx.xx."""
    if not component_scores:
        return ""
    return f"{max(1.0, geometric_mean(component_scores)):.2f}"


def aggregate(sample: set[str] | None = None) -> list[dict]:
    eligible = load_top_repos()
    by_component = {name: _scores_by_repo(path) for name, path in COMPONENTS.items()}

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        if sample is not None and repo not in sample:
            continue
        rid = str(entry.repo_id)
        row = {"repo": repo, "repo_id": entry.repo_id}
        present: list[float] = []
        complete = True
        for name, scores in by_component.items():
            s = scores.get(rid)
            row[name] = "" if s is None else f"{s:.2f}"
            if s is None:
                complete = False
            else:
                present.append(s)
        # Completeness rule: only score a repo whose every dimension is present.
        row["risk_score"] = overall_score(present) if complete else ""
        rows.append(row)

    # Ranked by overall risk score, highest first; unscored (incomplete) repos
    # sink to the end, ordered by repo for a stable, deterministic file.
    rows.sort(key=lambda r: (r["risk_score"] == "",
                             -float(r["risk_score"]) if r["risk_score"] else 0.0,
                             (r["repo"] or "").lower()))
    return rows


def _print_coverage(rows: list[dict]) -> None:
    total = len(rows)
    table = Table(title="[bold]Risk score coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Column", style="bold")
    table.add_column("Scored", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (*COMPONENTS, "risk_score"):
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
        allslugs = [e.repo for e in load_top_repos()]
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
