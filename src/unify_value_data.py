#!/usr/bin/env python3
"""Unify per-ecosystem value-pipeline results into a single CSV.

Reads `data/{ecosystem}/results.csv` for each ecosystem (npm, pypi, crates,
cpp) and writes `data/value-data.csv` with one row per (package, ecosystem):

    package, ecosystem, github_repo, pagerank, value_class, is_eol

`github_repo` is normalised to lowercase `owner/repo`. `is_eol` is joined
from `data/{ecosystem}/eol.csv` (produced by `src/{eco}/check_eol.py`);
packages with no EOL row default to `is_eol=False`. cpp is the unified
C/C++ ecosystem (Debian + Homebrew, joined via Repology) -- see
`src/cpp/process_data.py`.

Also prints the full value-pipeline funnel for each ecosystem and the
combined totals: # top packages, # after dep-tree expansion, # results,
class distribution, % GitHub coverage, # EOL.

Usage:
    uv run -m src.unify_value_data
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "value-data.csv"

# Ecosystems and the column that holds the package name in their results.csv.
ECOSYSTEMS: list[tuple[str, str]] = [
    ("npm", "package"),
    ("pypi", "package"),
    ("crates", "package"),
    ("cpp", "package"),
]

FIELDS = ["package", "ecosystem", "github_repo", "pagerank", "value_class", "is_eol"]


def _normalise_repo(repo: str) -> str:
    """Lowercase `owner/repo`, stripping whitespace."""
    return repo.strip().lower()


def _read_top_packages(path: Path, pkg_col: str) -> set[str]:
    """Return the set of package names from a top-packages.csv."""
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        return {row[pkg_col] for row in csv.DictReader(f)}


def _read_dep_tree_nodes(path: Path) -> set[str]:
    """Return the set of unique package + dependency nodes in a dep-tree CSV."""
    nodes: set[str] = set()
    if not path.exists():
        return nodes
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nodes.add(row["package"])
            nodes.add(row["dependency"])
    return nodes


def _read_eol_index(path: Path) -> dict[str, bool]:
    """Return {package: is_eol} from a per-ecosystem eol.csv. {} if missing."""
    if not path.exists():
        return {}
    idx: dict[str, bool] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            idx[r["package"]] = r["is_eol"] == "True"
    return idx


def collect_ecosystem(ecosystem: str, pkg_col: str) -> tuple[list[dict], dict]:
    """Read per-ecosystem files, return unified rows and funnel stats."""
    eco_dir = DATA_DIR / ecosystem
    top_path = eco_dir / "top-packages.csv"
    deps_path = eco_dir / "dependency-tree.csv"
    results_path = eco_dir / "results.csv"
    eol_path = eco_dir / "eol.csv"

    top_set = _read_top_packages(top_path, pkg_col)
    dep_nodes = _read_dep_tree_nodes(deps_path)
    # "After dep tree" = top packages plus their transitive deps. Some top
    # packages have no declared deps and no inbound deps, so they don't appear
    # in any dep-tree edge -- union with `top_set` to keep the count monotonic.
    after_deps = len(top_set | dep_nodes)
    top_count = len(top_set)
    eol_idx = _read_eol_index(eol_path)

    rows: list[dict] = []
    with open(results_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkg = r[pkg_col]
            rows.append({
                "package": pkg,
                "ecosystem": ecosystem,
                "github_repo": _normalise_repo(r.get("github_repo", "")),
                "pagerank": r.get("pagerank", ""),
                "value_class": r.get("value_class", ""),
                "is_eol": eol_idx.get(pkg, False),
            })

    classes = Counter(r["value_class"] for r in rows if r["value_class"])
    with_gh = sum(1 for r in rows if r["github_repo"])
    eol_count = sum(1 for r in rows if r["is_eol"])
    eol_covered = bool(eol_idx)

    ab_rows = [r for r in rows if r["value_class"] in ("A", "B")]
    ab_with_gh = sum(1 for r in ab_rows if r["github_repo"])
    ab_eol = sum(1 for r in ab_rows if r["is_eol"])

    stats = {
        "ecosystem": ecosystem,
        "top": top_count,
        "deps_unique": after_deps,
        "results": len(rows),
        "with_gh": with_gh,
        "gh_pct": (100.0 * with_gh / len(rows)) if rows else 0.0,
        "classes": classes,
        "ab_total": len(ab_rows),
        "ab_with_gh": ab_with_gh,
        "ab_gh_pct": (100.0 * ab_with_gh / len(ab_rows)) if ab_rows else 0.0,
        "eol_covered": eol_covered,
        "eol_count": eol_count,
        "ab_eol": ab_eol,
    }
    return rows, stats


def _print_funnel_table(stats_per_eco: list[dict]) -> None:
    table = Table(title="[bold]Value pipeline funnel[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    table.add_column("Top", justify="right")
    table.add_column("After deps", justify="right")
    table.add_column("Results", justify="right")
    table.add_column("With GH", justify="right")
    table.add_column("GH %", justify="right")

    tot = {"top": 0, "deps_unique": 0, "results": 0, "with_gh": 0}
    for s in stats_per_eco:
        table.add_row(
            s["ecosystem"],
            f"{s['top']:,}",
            f"{s['deps_unique']:,}",
            f"{s['results']:,}",
            f"{s['with_gh']:,}",
            f"{s['gh_pct']:.0f}%",
        )
        for k in tot:
            tot[k] += s[k]

    table.add_section()
    gh_pct = (100.0 * tot["with_gh"] / tot["results"]) if tot["results"] else 0.0
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{tot['top']:,}[/bold]",
        f"[bold]{tot['deps_unique']:,}[/bold]",
        f"[bold]{tot['results']:,}[/bold]",
        f"[bold]{tot['with_gh']:,}[/bold]",
        f"[bold]{gh_pct:.0f}%[/bold]",
    )
    console.print(table)


def _print_eol_table(stats_per_eco: list[dict]) -> None:
    table = Table(title="[bold]EOL coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    table.add_column("eol.csv", justify="center")
    table.add_column("EOL total", justify="right", style="red")
    table.add_column("EOL A+B", justify="right", style="red")

    tot_eol = 0
    tot_ab_eol = 0
    for s in stats_per_eco:
        table.add_row(
            s["ecosystem"],
            "[green]✓[/green]" if s["eol_covered"] else "[dim]–[/dim]",
            f"{s['eol_count']:,}",
            f"{s['ab_eol']:,}",
        )
        tot_eol += s["eol_count"]
        tot_ab_eol += s["ab_eol"]
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]", "",
        f"[bold]{tot_eol:,}[/bold]",
        f"[bold]{tot_ab_eol:,}[/bold]",
    )
    console.print(table)


def _print_class_table(stats_per_eco: list[dict]) -> None:
    table = Table(title="[bold]Value class distribution[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Ecosystem", style="bold")
    for cls in "ABCD":
        table.add_column(cls, justify="right")
    table.add_column("Total", justify="right")
    table.add_column("A+B GH", justify="right")

    totals = Counter()
    ab_total = 0
    ab_gh = 0
    for s in stats_per_eco:
        row = [s["ecosystem"]]
        eco_total = sum(s["classes"].values())
        for cls in "ABCD":
            n = s["classes"].get(cls, 0)
            totals[cls] += n
            row.append(f"{n:,}")
        row.append(f"{eco_total:,}")
        row.append(f"{s['ab_gh_pct']:.0f}%")
        ab_total += s["ab_total"]
        ab_gh += s["ab_with_gh"]
        table.add_row(*row)

    grand = sum(totals.values())
    grand_ab_gh_pct = (100.0 * ab_gh / ab_total) if ab_total else 0.0
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        *(f"[bold]{totals[cls]:,}[/bold]" for cls in "ABCD"),
        f"[bold]{grand:,}[/bold]",
        f"[bold]{grand_ab_gh_pct:.0f}%[/bold]",
    )
    console.print(table)


def main() -> None:
    console.print("[bold]Unifying value-pipeline results...[/bold]\n")

    all_rows: list[dict] = []
    stats_per_eco: list[dict] = []
    for ecosystem, pkg_col in ECOSYSTEMS:
        rows, stats = collect_ecosystem(ecosystem, pkg_col)
        all_rows.extend(rows)
        stats_per_eco.append(stats)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(all_rows)

    _print_funnel_table(stats_per_eco)
    console.print()
    _print_class_table(stats_per_eco)
    console.print()
    _print_eol_table(stats_per_eco)
    console.print()
    console.print(f"[dim]Written {len(all_rows):,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
