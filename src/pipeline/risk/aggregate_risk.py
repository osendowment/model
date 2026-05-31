#!/usr/bin/env python3
"""Stage 3 of the pipeline — final risk-metrics aggregator.

Reads the six per-dimension intermediate CSVs (each produced by its own
`build_<dim>.py` script) and joins them on `repo` into one flat table:

    data/risk/concentration.csv     ← src.pipeline.risk.build_concentration
    data/risk/complexity.csv        ← src.pipeline.risk.build_complexity
    data/risk/security.csv          ← src.pipeline.risk.build_security
    data/risk/funding.csv           ← src.pipeline.risk.build_funding
    data/risk/visibility.csv        ← src.pipeline.risk.build_visibility
    data/risk/workload.csv          ← src.pipeline.risk.build_workload

Writes:
    data/risk/risk.csv  — one row per risk-scope repo with every metric
                          column from the six intermediates.

This script does **no classification** (no A/B/C/D classes). Class
thresholds and rollups are deliberately deferred — the goal here is to
get a clean, validated metric matrix per repo.

Usage:
    uv run python -m src.pipeline.risk.aggregate_risk
    uv run python -m src.pipeline.risk.aggregate_risk --random 5      # test on 5 repos
    uv run python -m src.pipeline.risk.aggregate_risk --random 50

`--random N` runs the aggregator on a random sample of N eligible repos
(seeded by `--seed` if provided, default 42 for reproducibility) and
writes only those rows. Useful for sanity-checking the join + spotting
anomalies before running on all 899 repos.
"""

import argparse
import csv
import random
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"

# Intermediate CSVs feeding the aggregator. Each has its own builder.
INTERMEDIATES = {
    "concentration": DATA_DIR / "risk" / "concentration.csv",
    "complexity":    DATA_DIR / "risk" / "complexity.csv",
    "security":      DATA_DIR / "risk" / "security.csv",
    "funding":       DATA_DIR / "risk" / "funding.csv",
    "visibility":    DATA_DIR / "risk" / "visibility.csv",
    "workload":      DATA_DIR / "risk" / "workload.csv",
}

OUTPUT_FILE = DATA_DIR / "risk" / "risk.csv"

# Identity columns shared across all intermediates — emitted once.
ID_COLUMNS = ("repo", "repo_id")

# fetched_at columns get a per-dimension prefix in the final output so each
# metric's freshness is traceable.
FETCHED_AT_COL = "fetched_at"

# Columns kept in a per-dimension intermediate but deliberately NOT carried
# into risk.csv — informational signals that must not feed risk calculation.
# For a dimension listed here, ONLY these columns are carried into risk.csv
# (plus the always-kept `<dim>_fetched_at`). The funding dimension keeps a rich
# set of raw signals in funding.csv but contributes just its two headline
# metrics and the composite percentile to the risk table.
RISK_INCLUDED: dict[str, set[str]] = {
    "funding": {"oc_avg_funding", "gh_sponsorships", "funding_p"},
}


def _load_intermediate(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Return (column_order, {repo: row_dict}). Empty if file missing."""
    if not path.exists():
        return [], {}
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            rows[slug] = row
    return cols, rows


def _qualify_columns(dim: str, cols: list[str]) -> list[tuple[str, str]]:
    """For each non-identity column return (source_col, output_col).

    `fetched_at` is renamed to `<dim>_fetched_at` so each dimension's
    freshness is independently tracked. Other column names already include
    the dimension's semantics (e.g. `loc_2025_eoy`, `bf_commits_lifetime`)
    so they pass through unchanged.
    """
    included = RISK_INCLUDED.get(dim)  # None → carry every (non-id) column
    out: list[tuple[str, str]] = []
    for c in cols:
        if c in ID_COLUMNS:
            continue
        if included is not None and c not in included:
            continue  # whitelisted dim carries ONLY its listed columns
        # `fetched_at` → `<dim>_fetched_at` so freshness is traceable; a
        # whitelisted dim that omits it (e.g. funding) drops it above.
        out.append((c, f"{dim}_fetched_at" if c == FETCHED_AT_COL else c))
    return out


def aggregate(sample: set[str] | None = None) -> tuple[list[str], list[dict[str, str]]]:
    """Read all intermediates, join on repo, return (fieldnames, rows).

    `sample`, if given, restricts output to the listed repo slugs.
    """
    eligible = load_risk_repos()

    intermediates: dict[str, tuple[list[str], dict[str, dict[str, str]]]] = {}
    fieldnames: list[str] = list(ID_COLUMNS)
    qualified_per_dim: dict[str, list[tuple[str, str]]] = {}
    seen_outputs: set[str] = set(ID_COLUMNS)

    missing_files: list[str] = []
    for dim, path in INTERMEDIATES.items():
        cols, rows = _load_intermediate(path)
        intermediates[dim] = (cols, rows)
        if not cols:
            missing_files.append(str(path))
            continue
        # Drop any column already emitted by an earlier dimension — first
        # writer wins. `active_contributors` legitimately lives in both
        # concentration.csv and workload.csv (the workload builder reads it
        # straight from concentration.csv); without this dedup it would
        # land twice in risk-data.csv, breaking the DictWriter header.
        qualified: list[tuple[str, str]] = []
        for src_col, out_col in _qualify_columns(dim, cols):
            if out_col in seen_outputs:
                continue
            seen_outputs.add(out_col)
            qualified.append((src_col, out_col))
        qualified_per_dim[dim] = qualified
        fieldnames.extend(out for _, out in qualified)

    if missing_files:
        raise SystemExit(
            "Missing intermediate file(s):\n  - "
            + "\n  - ".join(missing_files)
            + "\n\nRun the corresponding `src.pipeline.risk.build_<dim>` first."
        )

    out_rows: list[dict[str, str]] = []
    for entry in eligible:
        repo = entry.repo
        if sample is not None and repo not in sample:
            continue
        row: dict[str, str] = {"repo": repo, "repo_id": entry.repo_id}
        for dim, qualified in qualified_per_dim.items():
            src = intermediates[dim][1].get(repo, {})
            for src_col, out_col in qualified:
                row[out_col] = src.get(src_col, "")
        out_rows.append(row)

    return fieldnames, out_rows


def _print_coverage(fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Per-dimension coverage table."""
    total = len(rows)
    if not total:
        console.print("[yellow]No rows to report[/yellow]")
        return

    # Group columns by dimension prefix.
    table = Table(title="[bold]Risk-data column coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Column", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in fieldnames:
        if col in ID_COLUMNS:
            continue
        if col.endswith("_fetched_at"):
            continue
        n = sum(1 for r in rows if r.get(col, ""))
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--random", type=int, metavar="N", default=0,
        help="Aggregate N random risk-scope repos instead of all (for testing)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for --random sampling (default: 42)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help=f"Override output path (default: {OUTPUT_FILE})",
    )
    args = parser.parse_args()

    sample: set[str] | None = None
    if args.random > 0:
        random.seed(args.seed)
        eligible = load_risk_repos()
        if args.random < len(eligible):
            chosen = random.sample(eligible, args.random)
        else:
            chosen = eligible
        sample = {e.repo for e in chosen}
        console.print(
            f"[bold]Aggregating risk metrics on {len(sample)} random "
            f"risk-scope repos (seed={args.seed})[/bold]\n"
        )
    else:
        console.print("[bold]Aggregating risk metrics on all risk-scope repos[/bold]\n")

    fieldnames, rows = aggregate(sample=sample)

    out_path = args.output or OUTPUT_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    _print_coverage(fieldnames, rows)
    console.print(
        f"\n[dim]Wrote {len(rows):,} repos × {len(fieldnames):,} columns "
        f"→ {out_path}[/dim]"
    )


if __name__ == "__main__":
    main()
