#!/usr/bin/env python3
"""Aggregate value-data.csv per GitHub repo (or per orphan package).

Reads `data/value-data.csv` (one row per package × ecosystem) and writes
`data/value-by-repo.csv` (one row per github_repo, plus one row per
package with an empty github_repo so nothing is dropped).

Per-ecosystem PR is summed within the group, then groups are ranked
within each ecosystem by that sum and assigned a cumulative-share
percentage (`pr_cum_pct_<eco>`) using the same A/B/C/D cutoffs as the
package-level value pipeline. `class_total` is the strongest class
across ecosystems where A < B < C < D.

Why not recompute PageRank on a repo-level dep graph? Cross-ecosystem
deps don't exist in our data — the repo graph is still four disconnected
subgraphs. Sum-within-ecosystem preserves per-ecosystem semantics; class
via max-class avoids skewing toward small ecosystems with concentrated
PR mass.

Usage:
    uv run python -m src.aggregate_by_repo
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.params import assign_value_class

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VALUE_FILE = DATA_DIR / "value-data.csv"
OUTPUT_FILE = DATA_DIR / "value-by-repo.csv"

ECOSYSTEMS = ("npm", "pypi", "crates", "cpp")
CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

FIELDS = (
    ["id", "github_repo", "packages"]
    + [f"pkgs_{e}" for e in ECOSYSTEMS]
    + ["pkgs_total"]
    + [f"pr_cum_pct_{e}" for e in ECOSYSTEMS]
    + [f"class_{e}" for e in ECOSYSTEMS]
    + ["class_total", "is_eol"]
)


def group_key(row: dict) -> str:
    """github_repo if set, else a unique synthetic key so the orphan stays its own group."""
    repo = row["github_repo"].strip()
    if repo:
        return repo
    return f"__orphan__:{row['ecosystem']}:{row['package']}"


def aggregate() -> list[dict]:
    rows = list(csv.DictReader(open(VALUE_FILE, encoding="utf-8")))

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[group_key(r)].append(r)

    aggs: list[dict] = []
    for key, members in groups.items():
        a: dict = {
            "group_key": key,
            "github_repo": members[0]["github_repo"],
            # "package (ecosystem)" so polyglot repos read clearly
            "packages": "; ".join(
                f"{m['package']} ({m['ecosystem']})" for m in
                sorted(members, key=lambda m: (m["ecosystem"], m["package"]))
            ),
            "is_eol_all": all(m.get("is_eol") == "True" for m in members),
        }
        for eco in ECOSYSTEMS:
            eco_rows = [m for m in members if m["ecosystem"] == eco]
            a[f"pkgs_{eco}"] = len(eco_rows)
            a[f"_pr_sum_{eco}"] = sum(
                float(m["pagerank"]) for m in eco_rows if m.get("pagerank")
            )
        a["pkgs_total"] = sum(a[f"pkgs_{e}"] for e in ECOSYSTEMS)
        aggs.append(a)

    # Per ecosystem: rank groups by PR sum desc → cumulative share → class
    for eco in ECOSYSTEMS:
        present = [a for a in aggs if a[f"pkgs_{eco}"] > 0]
        present.sort(key=lambda a: a[f"_pr_sum_{eco}"], reverse=True)
        total = sum(a[f"_pr_sum_{eco}"] for a in present)
        cum = 0.0
        for a in present:
            cum += a[f"_pr_sum_{eco}"]
            pct = (cum / total * 100.0) if total else 0.0
            a[f"pr_cum_pct_{eco}"] = round(pct, 4)
            a[f"class_{eco}"] = assign_value_class(pct / 100.0)
        # Defaults for groups not present in this ecosystem
        for a in aggs:
            a.setdefault(f"pr_cum_pct_{eco}", "")
            a.setdefault(f"class_{eco}", "")

    # class_total = strongest class across ecosystems
    for a in aggs:
        present_classes = [a[f"class_{e}"] for e in ECOSYSTEMS if a[f"class_{e}"]]
        a["class_total"] = (
            min(present_classes, key=lambda c: CLASS_RANK[c]) if present_classes else ""
        )
        a["is_eol"] = a["is_eol_all"]

    # Sort: stronger class first, then more packages, then repo name
    aggs.sort(key=lambda a: (
        CLASS_RANK.get(a["class_total"], 4),
        -a["pkgs_total"],
        a["github_repo"] or a["group_key"],
    ))
    for i, a in enumerate(aggs, 1):
        a["id"] = i

    return aggs


def write_output(aggs: list[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(aggs)


def display_summary(aggs: list[dict]) -> None:
    grouped = sum(1 for a in aggs if a["github_repo"])
    orphans = sum(1 for a in aggs if not a["github_repo"])
    monorepos = sum(1 for a in aggs if a["pkgs_total"] > 1 and a["github_repo"])
    multi_eco = sum(
        1 for a in aggs
        if sum(1 for e in ECOSYSTEMS if a[f"pkgs_{e}"] > 0) > 1
    )

    s = Table(title="[bold]value-by-repo summary[/bold]", header_style="bold dim")
    s.add_column("metric")
    s.add_column("count", justify="right")
    s.add_row("total groups", f"{len(aggs):,}")
    s.add_row("with github_repo", f"{grouped:,}")
    s.add_row("orphans (no github_repo)", f"{orphans:,}")
    s.add_row("monorepos (>1 pkg, github_repo set)", f"{monorepos:,}")
    s.add_row("cross-ecosystem groups", f"{multi_eco:,}")
    s.add_row("[red]is_eol=True[/red]", f"[red]{sum(1 for a in aggs if a['is_eol']):,}[/red]")
    console.print(s)
    console.print()

    c = Table(title="[bold]class distribution[/bold]", header_style="bold dim")
    c.add_column("class")
    for eco in ECOSYSTEMS:
        c.add_column(eco, justify="right")
    c.add_column("total (strongest)", justify="right", style="bold")
    for cls in ("A", "B", "C", "D"):
        row = [cls]
        for eco in ECOSYSTEMS:
            n = sum(1 for a in aggs if a[f"class_{eco}"] == cls)
            row.append(f"{n:,}")
        n_total = sum(1 for a in aggs if a["class_total"] == cls)
        row.append(f"{n_total:,}")
        c.add_row(*row)
    console.print(c)


def main() -> None:
    started = datetime.now()
    console.print(f"[bold]aggregate_by_repo[/bold]  started={started:%Y-%m-%d %H:%M:%S}")

    aggs = aggregate()
    write_output(aggs)
    display_summary(aggs)

    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(aggs):,} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
