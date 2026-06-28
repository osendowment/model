"""Per-repo, per-source coverage report.

Counts data points present per eligible repo across every source-related
file (long-format raw + per-year wide). Highlights repos that are missing
data and where, so we know what's still fixable vs structurally absent.

Usage:
    uv run python scripts/coverage_report.py
    uv run python scripts/coverage_report.py --top-missing 50
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common.repos import load_top_repos  # noqa: E402

# (label, path, repo_col, expectation_per_repo)
#   expectation_per_repo = how many rows per repo we expect at minimum.
#   For long-format sha-pinned files it's the metric count for one snapshot.
SOURCES = [
    ("commits-years", "data/sources/github/git/commits-years.csv", "repo", 1),
    ("scc",           "data/sources/git/scc.csv",                    "repo", 6),  # 6 metrics
    ("lizard",        "data/sources/git/lizard.csv",                 "repo", 4),  # min: cognitive set
    ("openssf",       "data/sources/git/openssf.csv",                "repo", 19), # score+18 checks
    ("depsdev",       "data/sources/git/depsdev.csv",                "repo", 1),
    ("semgrep",       "data/sources/git/semgrep.csv",                "repo", 8),  # findings_*
    ("churn",         "data/sources/github/git/churn.csv",           "repo", 1),
    ("issues",        "data/sources/github/issues.csv",              "repo", 1),
    ("cves-queried",  "data/sources/osv/queried.csv",                "repo", 1),
    ("repos",         "data/sources/github/repos.csv",               "repo", 1),
    ("contrib-commits-git", "data/sources/git/contributor-commits.csv",    "repo", 1),
    ("contrib-commits-gh",  "data/sources/github/contributor-commits.csv", "repo", 1),
    ("sponsors",      "data/sources/github/sponsors.csv",            "repo", 1),
    ("sponsorships",  "data/sources/github/sponsorships.csv",        "repo", 1),
    ("funding-yml",   "data/sources/github/funding-yml.csv",         "repo", 1),
    ("funding-json",  "data/sources/floss-fund/funding-json.csv",    "repo", 1),
    ("oc-budgets",    "data/sources/opencollective/budgets.csv",     "repo", 1),
    ("openssf-checks","data/sources/openssf/checks.csv",             "repo", 1),
]

CATEGORY_FILES = [
    ("complexity",    "data/risk/complexity.csv"),
    ("security",      "data/risk/security.csv"),
    ("concentration", "data/risk/concentration.csv"),
    ("funding",       "data/risk/funding.csv"),
    ("workload",      "data/risk/workload.csv"),
]


def load_eligible() -> set[str]:
    """Risk-scope set (value-class A/B, non-archived, valid) — the same set
    the risk fetchers/builders run on, and now also the eligibility input."""
    return {e.repo for e in load_top_repos(
        value_file=str(ROOT / "data/value/value.csv"),
        repos_file=str(ROOT / "data/sources/github/repos.csv"),
    ) if e.repo}


def count_rows_per_repo(path: Path, repo_col: str) -> Counter[str]:
    if not path.exists():
        return Counter()
    c: Counter[str] = Counter()
    with path.open() as f:
        for row in csv.DictReader(f):
            r = row.get(repo_col)
            if r:
                c[r] += 1
    return c


def category_coverage(path: Path, eligible: set[str]) -> tuple[int, int, dict[str, int]]:
    """Return (rows, populated_eligible, per_column_populated_eligible_count)."""
    if not path.exists():
        return 0, 0, {}
    rows: list[dict[str, str]] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows = list(reader)
    populated = 0
    per_col_populated: dict[str, int] = {c: 0 for c in cols if c not in ("repo", "repo_id") and not c.endswith("fetched_at")}
    for r in rows:
        repo = r.get("repo")
        if repo not in eligible:
            continue
        # any non-id field populated counts as a present row
        if any(r.get(c, "").strip() for c in cols if c not in ("repo", "repo_id")):
            populated += 1
        for c in per_col_populated:
            if r.get(c, "").strip():
                per_col_populated[c] += 1
    return len(rows), populated, per_col_populated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-missing", type=int, default=20,
                        help="Show top N repos by missing-source count")
    args = parser.parse_args()

    eligible = load_eligible()
    console.print(f"[bold]Risk-scope repos:[/bold] {len(eligible)}\n")

    # Source-level table
    src_table = Table(title="Source coverage", show_header=True, header_style="bold dim")
    src_table.add_column("source")
    src_table.add_column("path", style="dim")
    src_table.add_column("eligible covered", justify="right")
    src_table.add_column("avg rows/repo", justify="right")
    src_table.add_column("status")

    per_repo_present: dict[str, set[str]] = defaultdict(set)

    for label, path, repo_col, expected in SOURCES:
        c = count_rows_per_repo(ROOT / path, repo_col)
        elig_covered = {r for r in c if r in eligible}
        for r in elig_covered:
            per_repo_present[r].add(label)
        avg = sum(c[r] for r in elig_covered) / max(1, len(elig_covered))
        pct = 100 * len(elig_covered) / len(eligible)
        status = (
            "[green]100%[/green]" if len(elig_covered) == len(eligible) else
            f"[yellow]{pct:.1f}%[/yellow]"
        )
        src_table.add_row(label, path, f"{len(elig_covered)}/{len(eligible)}",
                          f"{avg:.1f}", status)
    console.print(src_table)

    # Category-file table
    cat_table = Table(title="Category coverage (rows × populated cells per column)",
                      show_header=True, header_style="bold dim")
    cat_table.add_column("category")
    cat_table.add_column("path", style="dim")
    cat_table.add_column("rows", justify="right")
    cat_table.add_column("eligible populated", justify="right")
    cat_table.add_column("min col cov %", justify="right")
    cat_table.add_column("median col cov %", justify="right")

    for label, path in CATEGORY_FILES:
        rows, pop, per_col = category_coverage(ROOT / path, eligible)
        if per_col:
            covs = sorted(100 * v / len(eligible) for v in per_col.values())
            min_c = covs[0]
            med_c = covs[len(covs) // 2]
            cat_table.add_row(label, path, str(rows), str(pop),
                              f"{min_c:.1f}%", f"{med_c:.1f}%")
        else:
            cat_table.add_row(label, path, str(rows), str(pop), "-", "-")
    console.print(cat_table)

    # Top missing repos
    sources_total = len(SOURCES)
    missing_per_repo = {r: sources_total - len(per_repo_present.get(r, set()))
                        for r in eligible}
    by_missing = sorted(missing_per_repo.items(), key=lambda x: -x[1])
    if args.top_missing > 0 and by_missing[0][1] > 0:
        miss_table = Table(title=f"Top {args.top_missing} repos by missing-source count",
                           show_header=True, header_style="bold dim")
        miss_table.add_column("repo")
        miss_table.add_column("missing", justify="right")
        miss_table.add_column("present sources", style="dim")
        for repo, miss in by_missing[: args.top_missing]:
            if miss == 0:
                break
            present = sorted(per_repo_present.get(repo, set()))
            missing = sorted({s for s, *_ in [(label,) for label, *_ in SOURCES]} - set(present))
            miss_table.add_row(repo, f"{miss}/{sources_total}", ", ".join(missing[:5]) +
                              ("…" if len(missing) > 5 else ""))
        console.print(miss_table)

    # Final risk-data.csv shape
    risk_path = ROOT / "data/risk/risk.csv"
    if risk_path.exists():
        with risk_path.open() as f:
            reader = csv.DictReader(f)
            cols = list(reader.fieldnames or [])
            risk_rows = list(reader)
        elig_in_risk = sum(1 for r in risk_rows if r["repo"] in eligible)
        per_col = {c: sum(1 for r in risk_rows if r["repo"] in eligible and r.get(c, "").strip())
                   for c in cols if c not in ("repo", "repo_id")}
        if per_col:
            covs = sorted(100 * v / len(eligible) for v in per_col.values())
            console.print(
                f"\n[bold]risk.csv:[/bold] {len(risk_rows)} rows · "
                f"{elig_in_risk}/{len(eligible)} risk-scope populated · "
                f"{len(cols)} cols · "
                f"min col coverage {covs[0]:.1f}% · "
                f"median {covs[len(covs)//2]:.1f}%"
            )
            # 10 worst columns
            worst = sorted(per_col.items(), key=lambda x: x[1])[:10]
            wt = Table(title="Lowest-coverage columns in risk.csv",
                       show_header=True, header_style="bold dim")
            wt.add_column("column"); wt.add_column("populated", justify="right")
            wt.add_column("coverage %", justify="right")
            for c, n in worst:
                wt.add_row(c, str(n), f"{100*n/len(eligible):.1f}%")
            console.print(wt)


if __name__ == "__main__":
    main()
