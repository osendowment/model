"""Pipeline health check — internal consistency of the value/risk pipeline.

Verifies the *derived* CSVs are in sync with their builders and inputs. It
catches the class of staleness bug where an upstream file is regenerated
but a downstream file is not re-run (e.g. complexity.csv changing without
build_workload being re-run — a real bug found mid-development).

Complements `scripts/data_anomalies.py`, which checks metric *values* for
outliers/types; this script checks *consistency*.

Checks:
  1. Each risk dimension CSV (complexity / concentration / funding /
     security / workload) matches its `build_<dim>.build()`.
  2. data/risk/risk.csv matches `aggregate_risk.aggregate()`.
  3. data/value/value.csv matches `unify_value_data` (class assignments).
  4. Long-format git files have no duplicate (repo, sha, metric) keys.

Usage:
    uv run python scripts/pipeline_health.py
    uv run python scripts/pipeline_health.py --strict   # exit 1 if unhealthy
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (label, ok, detail)
Result = tuple[str, bool, str]


def _norm(rows: list[dict]) -> dict[str, dict[str, str]]:
    """list[dict] → {repo: {col: str-value}}, None rendered as ''."""
    return {
        r["repo"]: {k: ("" if v is None else str(v)) for k, v in r.items()}
        for r in rows
    }


def _read_csv_by_repo(path: Path) -> dict[str, dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        return {r["repo"]: dict(r) for r in csv.DictReader(f)}


def _cell_diffs(built: dict, disk: dict) -> int:
    return sum(
        1
        for repo in built
        for k in set(built[repo]) | set(disk[repo])
        if built[repo].get(k, "") != disk[repo].get(k, "")
    )


def check_dimension_csvs() -> list[Result]:
    """Each dimension CSV must equal its builder's current output."""
    from src.risk import (
        build_complexity,
        build_concentration,
        build_funding,
        build_security,
        build_workload,
    )

    builders = [
        ("complexity", build_complexity),
        ("concentration", build_concentration),
        ("funding", build_funding),
        ("security", build_security),
        ("workload", build_workload),
    ]
    out: list[Result] = []
    for name, mod in builders:
        built = _norm(mod.build())
        disk = _read_csv_by_repo(ROOT / "data" / "risk" / f"{name}.csv")
        if set(built) != set(disk):
            out.append((f"{name}.csv", False,
                        f"repo set differs (builder {len(built)}, disk {len(disk)})"))
            continue
        diffs = _cell_diffs(built, disk)
        out.append((f"{name}.csv", diffs == 0,
                    "in sync" if diffs == 0
                    else f"{diffs} stale cells — re-run build_{name}"))
    return out


def check_risk_data() -> list[Result]:
    """risk.csv must equal aggregate_risk's join of the dimension CSVs."""
    from src.risk.aggregate_risk import aggregate

    rows = aggregate()
    built = _norm(rows)
    disk = _read_csv_by_repo(ROOT / "data" / "risk" / "risk.csv")
    if set(built) != set(disk):
        return [("risk.csv", False,
                 f"repo set differs (builder {len(built)}, disk {len(disk)})")]
    diffs = _cell_diffs(built, disk)
    return [("risk.csv", diffs == 0,
             "in sync" if diffs == 0
             else f"{diffs} stale cells — re-run aggregate_risk")]


def check_value_data() -> list[Result]:
    """value.csv class assignments must match a fresh unify run."""
    from src.value.unify_value_data import (
        ECOSYSTEMS,
        aggregate_by_repo,
        collect_ecosystem,
    )

    all_rows: list[dict] = []
    for eco in ECOSYSTEMS:
        rows, _ = collect_ecosystem(eco)
        all_rows.extend(rows)
    built = aggregate_by_repo(all_rows)
    disk = list(csv.DictReader(open(ROOT / "data" / "value" / "value.csv", encoding="utf-8")))

    if len(built) != len(disk):
        return [("value.csv", False,
                 f"row count differs (unify {len(built)}, disk {len(disk)})")]

    def cls_by_gh(rows: list[dict]) -> dict[str, str]:
        seen = Counter((r.get("github_repo") or "").strip().lower() for r in rows)
        return {
            (r.get("github_repo") or "").strip().lower(): r.get("class", "")
            for r in rows
            if (r.get("github_repo") or "").strip()
            and seen[(r.get("github_repo") or "").strip().lower()] == 1
        }

    b, d = cls_by_gh(built), cls_by_gh(disk)
    mismatch = sum(1 for g in set(b) & set(d) if b[g] != d[g])
    return [("value.csv", mismatch == 0,
             f"class assignments in sync ({len(set(b) & set(d)):,} repos)"
             if mismatch == 0 else f"{mismatch} class mismatches — re-run unify")]


def check_long_format_keys() -> list[Result]:
    """Long-format git files must have unique (repo, sha, metric) keys."""
    out: list[Result] = []
    for path in sorted(glob.glob(str(ROOT / "data" / "sources" / "git" / "*.csv"))):
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        if not rows or not {"repo", "commit_sha", "metric"} <= set(rows[0]):
            continue  # not a long-format file
        keys = Counter((r["repo"], r["commit_sha"], r["metric"]) for r in rows)
        dups = sum(1 for v in keys.values() if v > 1)
        name = Path(path).name
        out.append((f"git/{name}", dups == 0,
                    f"{len(rows):,} rows, keys unique" if dups == 0
                    else f"{dups} duplicate (repo,sha,metric) keys"))
    return out


CHECKS = [check_dimension_csvs, check_risk_data, check_value_data,
          check_long_format_keys]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any check fails")
    args = parser.parse_args()

    console.print("\n[bold]Pipeline health check[/bold]\n")
    results: list[Result] = []
    for check in CHECKS:
        results.extend(check())

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")
    failures = 0
    for label, ok, detail in results:
        if not ok:
            failures += 1
        table.add_row(label, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)
    console.print(table)

    if failures:
        console.print(f"\n[red]{failures} check(s) failed[/red] of {len(results)}.\n")
    else:
        console.print(f"\n[green]All {len(results)} checks passed — pipeline consistent.[/green]\n")

    return 1 if (args.strict and failures) else 0


if __name__ == "__main__":
    sys.exit(main())
