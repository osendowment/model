#!/usr/bin/env python3
"""Stage 3 of the risk pipeline — the overall risk table.

Each dimension builder writes a per-component CSV whose `score` column is that
dimension's risk score (integer 0-100, higher = riskier). This aggregator joins
the five component scores per repo and computes the overall `score` =
geometric mean of the five component scores.

**Completeness rule:** the overall `score` is calculable only if *every* one of
the five component scores is present. A partial geometric mean (over 3-4 of 5
dimensions) is not comparable across repos, so a repo missing any component
score is left with a blank overall `score`. (Each component score is itself
already blank-unless-complete — `geom_mean_composite` blanks it when any of its
own scored inputs is missing — so this rule propagates input-completeness all
the way up.) `scripts/pipeline_health.py` enforces it.

Writes data/risk/risk.csv with:
    repo, repo_id, concentration, complexity, security, funding, workload,
    score, dims_scored, concentration_imputed

`dims_scored` (0-5) counts how many of the five component scores are present for
the repo — it surfaces under-measured repos whose overall `score` rests on only a
partial set of dimensions. `concentration_imputed` flags repos whose concentration
row was worst-case imputed (no active human contributor in the 5y window), so the
ceiling-tied (score=100) concentration repos stay auditable downstream.

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

# Component name (= its risk.csv column) → per-dimension CSV.
COMPONENTS = {
    "concentration": DATA_DIR / "risk" / "concentration.csv",
    "complexity":    DATA_DIR / "risk" / "complexity.csv",
    "security":      DATA_DIR / "risk" / "security.csv",
    "funding":       DATA_DIR / "risk" / "funding.csv",
    "workload":      DATA_DIR / "risk" / "workload.csv",
}
OUTPUT_FILE = DATA_DIR / "risk" / "risk.csv"
FIELDS = ["repo", "repo_id", *COMPONENTS, "score", "dims_scored", "concentration_imputed"]

# `comment` strings written by src/risk/build_concentration.py (git_metrics) when a
# repo has no active human contributor in the 5y window and its concentration is
# worst-case imputed (bus factor 1, HHI 10000). A row carrying one of these is the
# only kind of imputed concentration. Fetch-failure comments ("git fetch <status>",
# "no git data") are NOT imputation — those rows have a blank score and stay unflagged.
CONCENTRATION_IMPUTE_COMMENTS = {
    "no commits in 5y",
    "no commits through last complete year",
    "no human commits in 5y",
}


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


def _imputed_concentration_repos(path: Path) -> set[str]:
    """{repo_lowercased} whose concentration row was worst-case IMPUTED.

    A repo is imputed when build_concentration found no active human in the 5y
    window and forced bus factor 1 / HHI 10000, recording WHY in `comment`. We
    match those exact reason strings (CONCENTRATION_IMPUTE_COMMENTS) — fetch
    failures carry different comments and a blank score, so they don't match.
    """
    out: set[str] = set()
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            comment = (row.get("comment") or "").strip()
            if repo and comment in CONCENTRATION_IMPUTE_COMMENTS:
                out.add(repo)
    return out


def overall_score(component_scores: list[int]) -> str:
    """Overall risk score = geometric mean of present component scores (int)."""
    if not component_scores:
        return ""
    return str(max(1, int(round(geometric_mean([float(s) for s in component_scores])))))


def aggregate(sample: set[str] | None = None) -> list[dict]:
    eligible = load_top_repos()
    by_component = {name: _scores_by_repo(path) for name, path in COMPONENTS.items()}
    imputed = _imputed_concentration_repos(COMPONENTS["concentration"])

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        if sample is not None and repo not in sample:
            continue
        row = {"repo": repo, "repo_id": entry.repo_id}
        present: list[int] = []
        complete = True
        for name, scores in by_component.items():
            s = scores.get(repo)
            row[name] = "" if s is None else s
            if s is None:
                complete = False
            else:
                present.append(s)
        # Completeness rule: only score a repo whose every component is present;
        # a partial geometric mean over a subset of dimensions isn't comparable.
        row["score"] = overall_score(present) if complete else ""
        # dims_scored explains WHY a score is blank (how many of 5 were present);
        # concentration_imputed flags the worst-case-imputed dormant repos.
        row["dims_scored"] = len(present)
        row["concentration_imputed"] = "True" if repo in imputed else ""
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
        allslugs = [e.repo for e in load_top_repos()]
        sample = set(random.sample(allslugs, min(args.random, len(allslugs))))

    rows = aggregate(sample=sample)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    _print_coverage(rows)
    under = sum(1 for r in rows if r.get("dims_scored", 0) < 5)
    imputed = sum(1 for r in rows if r.get("concentration_imputed") == "True")
    console.print(
        f"[dim]dims_scored<5 (under-measured): {under:,} · "
        f"concentration_imputed=True: {imputed:,}[/dim]"
    )
    console.print(f"\n[dim]Wrote {len(rows):,} repos × {len(FIELDS)} columns → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
