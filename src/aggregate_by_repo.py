#!/usr/bin/env python3
"""Aggregate value-data.csv per GitHub repo (or per orphan package).

Reads `data/value-data.csv` (one row per package × ecosystem) and writes
`data/value-by-repo.csv` with columns:

    id              sequential numeric id
    github_repo     lowercase owner/name (empty for orphan packages)
    ecosystems      comma-separated list of ecosystems where this group
                    has at least one package
    packages        total package count in the group
    top_eco         the ecosystem where this group is highest-ranked by
                    PR percentile (= 100 - cum_share). Empty if the group
                    has no packages with PR data.
    top_eco_pct     PR percentile in `top_eco` (0–100, higher = better).
    class_<eco>     A/B/C/D for each of npm/pypi/crates/cpp, empty if
                    the group has no package in that ecosystem

Class-per-ecosystem is computed by summing the group's package PR within
the ecosystem, ranking groups by that sum, and applying the same
cumulative-share cutoffs as the package-level value pipeline (≤50% A,
≤75% B, ≤90% C, rest D).

Rows with an empty `github_repo` (e.g. cpp packages like glibc, gcc) are
kept as their own one-package groups so nothing is dropped.

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
    ["id", "github_repo", "ecosystems", "packages",
     "top_eco", "top_eco_pkg", "top_eco_pct", "class"]
    + [f"class_{e}" for e in ECOSYSTEMS]
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
            "packages": len(members),
        }
        present_ecos: list[str] = []
        for eco in ECOSYSTEMS:
            eco_rows = [m for m in members if m["ecosystem"] == eco]
            a[f"_pkgs_{eco}"] = len(eco_rows)
            a[f"_pr_sum_{eco}"] = sum(
                float(m["pagerank"]) for m in eco_rows if m.get("pagerank")
            )
            if eco_rows:
                present_ecos.append(eco)
                # Top package in this ecosystem by PR (used for top_eco_pkg)
                top_pkg = max(
                    eco_rows,
                    key=lambda m: float(m.get("pagerank") or 0),
                )
                a[f"_top_pkg_{eco}"] = top_pkg["package"]
        a["ecosystems"] = ",".join(present_ecos)
        aggs.append(a)

    # Per ecosystem: rank groups by PR sum desc → cumulative share → class.
    # `_pr_pct_<eco>` = 100 - cum_share so higher is better; used to pick top_eco.
    for eco in ECOSYSTEMS:
        present = [a for a in aggs if a[f"_pkgs_{eco}"] > 0]
        present.sort(key=lambda a: a[f"_pr_sum_{eco}"], reverse=True)
        total = sum(a[f"_pr_sum_{eco}"] for a in present)
        cum = 0.0
        for a in present:
            cum += a[f"_pr_sum_{eco}"]
            cum_pct = (cum / total * 100.0) if total else 0.0
            a[f"class_{eco}"] = assign_value_class(cum_pct / 100.0)
            a[f"_pr_pct_{eco}"] = 100.0 - cum_pct
        for a in aggs:
            a.setdefault(f"class_{eco}", "")

    # top_eco / top_eco_pkg / top_eco_pct: ecosystem with the best (max) PR
    # percentile, the top package within it, and that percentile.
    # class: strongest of the per-eco classes (A < B < C < D)
    for a in aggs:
        pcts = {e: a[f"_pr_pct_{e}"] for e in ECOSYSTEMS if f"_pr_pct_{e}" in a}
        if pcts:
            top_eco = max(pcts, key=pcts.get)
            a["top_eco"] = top_eco
            a["top_eco_pkg"] = a.get(f"_top_pkg_{top_eco}", "")
            a["top_eco_pct"] = round(pcts[top_eco], 4)
        else:
            a["top_eco"] = ""
            a["top_eco_pkg"] = ""
            a["top_eco_pct"] = ""
        present_classes = [a[f"class_{e}"] for e in ECOSYSTEMS if a[f"class_{e}"]]
        a["class"] = (
            min(present_classes, key=lambda c: CLASS_RANK[c]) if present_classes else ""
        )

    # Sort by top_eco_pct desc (highest importance first); fall back to repo
    # name for stability when percentiles tie.
    def sort_key(a: dict) -> tuple:
        pct = a["top_eco_pct"] if isinstance(a["top_eco_pct"], (int, float)) else -1
        return (-pct, a["github_repo"] or a["group_key"])

    aggs.sort(key=sort_key)
    for i, a in enumerate(aggs, 1):
        a["id"] = i

    return aggs


def write_output(aggs: list[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(aggs)


def propagate_to_value_data(aggs: list[dict]) -> None:
    """Add top_eco_pct to data/value-data.csv (per-package), keyed by group.

    Every package row gets the `top_eco_pct` of its repo group — so all 202
    babel/babel packages share the repo's percentile. Orphans (no
    github_repo) carry their own one-package group's percentile.
    """
    pct_by_group: dict[str, float | str] = {a["group_key"]: a["top_eco_pct"] for a in aggs}

    with open(VALUE_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        existing_fields = list(reader.fieldnames or [])

    for r in rows:
        key = r["github_repo"] or f"__orphan__:{r['ecosystem']}:{r['package']}"
        r["top_eco_pct"] = pct_by_group.get(key, "")

    # Insert top_eco_pct just before is_eol if present, else append
    new_fields = [f for f in existing_fields if f != "top_eco_pct"]
    if "is_eol" in new_fields:
        new_fields.insert(new_fields.index("is_eol"), "top_eco_pct")
    else:
        new_fields.append("top_eco_pct")

    with open(VALUE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=new_fields, quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def display_summary(aggs: list[dict]) -> None:
    grouped = sum(1 for a in aggs if a["github_repo"])
    orphans = sum(1 for a in aggs if not a["github_repo"])
    monorepos = sum(1 for a in aggs if a["packages"] > 1 and a["github_repo"])
    multi_eco = sum(1 for a in aggs if "," in a["ecosystems"])

    s = Table(title="[bold]value-by-repo summary[/bold]", header_style="bold dim")
    s.add_column("metric")
    s.add_column("count", justify="right")
    s.add_row("total groups", f"{len(aggs):,}")
    s.add_row("with github_repo", f"{grouped:,}")
    s.add_row("orphans (no github_repo)", f"{orphans:,}")
    s.add_row("monorepos (>1 pkg, github_repo set)", f"{monorepos:,}")
    s.add_row("cross-ecosystem groups", f"{multi_eco:,}")
    console.print(s)
    console.print()

    c = Table(title="[bold]class distribution[/bold]", header_style="bold dim")
    c.add_column("class")
    for eco in ECOSYSTEMS:
        c.add_column(eco, justify="right")
    c.add_column("strongest", justify="right", style="bold")
    for cls in ("A", "B", "C", "D"):
        row = [cls]
        for eco in ECOSYSTEMS:
            n = sum(1 for a in aggs if a[f"class_{eco}"] == cls)
            row.append(f"{n:,}")
        n_strongest = sum(
            1 for a in aggs
            if min(
                (CLASS_RANK[a[f"class_{e}"]] for e in ECOSYSTEMS if a[f"class_{e}"]),
                default=4,
            ) == CLASS_RANK[cls]
        )
        row.append(f"{n_strongest:,}")
        c.add_row(*row)
    console.print(c)


def main() -> None:
    started = datetime.now()
    console.print(f"[bold]aggregate_by_repo[/bold]  started={started:%Y-%m-%d %H:%M:%S}")

    aggs = aggregate()
    write_output(aggs)
    propagate_to_value_data(aggs)
    display_summary(aggs)

    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(aggs):,} rows, {(datetime.now() - started).total_seconds():.1f}s)")
    console.print(f"[green]updated[/green] {VALUE_FILE.relative_to(DATA_DIR.parent)}  "
                  f"(added top_eco_pct column)")


if __name__ == "__main__":
    main()
