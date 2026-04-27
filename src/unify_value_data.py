#!/usr/bin/env python3
"""Unify per-ecosystem value-pipeline results into a single per-repo CSV.

Reads `data/{ecosystem}/results.csv` for each ecosystem (npm, pypi, crates,
cpp), groups packages by GitHub repo (or treats packages without a
`github_repo` as their own one-package groups), and writes
`data/value-data.csv` with **one row per repo**:

    id, github_repo, ecosystems, packages,
    top_eco, top_eco_pkg, top_eco_pct, class,
    class_npm, class_pypi, class_crates, class_cpp

`github_repo` is normalised to lowercase `owner/repo`. cpp is the unified
C/C++ ecosystem (Debian + Homebrew, joined via Repology) -- see
`src/cpp/process_data.py`.

Per-ecosystem class is computed by summing each group's package PR within
the ecosystem, ranking groups by that sum desc, and applying the same
cumulative-share cutoffs as the package-level value pipeline (≤50% A,
≤75% B, ≤90% C, rest D). `class` is the strongest of the per-eco classes
(A < B < C < D). `top_eco_pct = 100 − cumulative_pr_share`, so higher
means closer to the top of the ecosystem; `top_eco` is the ecosystem with
the max percentile and `top_eco_pkg` is the highest-PR package in it.

Rows are sorted by `top_eco_pct` desc so the highest-importance repos
come first.

EOL is intentionally **not** stored here. It's a property of the
eligibility pipeline (license + EOL); see `src/eligibility.py`, which
joins per-ecosystem `data/{eco}/eol.csv` with `data/{eco}/results.csv`
to compute per-repo `is_eol` directly.

Usage:
    uv run -m src.unify_value_data
"""

import csv
from collections import Counter, defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.params import assign_value_class

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "value-data.csv"

ECOSYSTEMS: tuple[str, ...] = ("npm", "pypi", "crates", "cpp")
CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}

# Ecosystem name → name of the package-name column in its results.csv
_ECOSYSTEM_SPECS: list[tuple[str, str]] = [
    ("npm", "package"),
    ("pypi", "package"),
    ("crates", "package"),
    ("cpp", "package"),
]

FIELDS = (
    ["id", "github_repo", "git_url", "ecosystems", "packages",
     "top_eco", "top_eco_pkg", "top_eco_pct", "class"]
    + [f"class_{e}" for e in ECOSYSTEMS]
)

# Order in which to pick the canonical git URL from a per-eco git.csv row
GIT_HOST_PRIORITY = ("github", "gitlab", "codeberg", "sourcehut", "bitbucket", "custom")


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


def _read_git_index(path: Path) -> dict[str, str]:
    """Return {package: canonical_git_url} from a per-ecosystem git.csv.

    Picks the first non-empty URL in `GIT_HOST_PRIORITY` order, so a
    package with both github and gitlab entries resolves to github.
    """
    if not path.exists():
        return {}
    idx: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for host in GIT_HOST_PRIORITY:
                url = (r.get(host) or "").strip()
                if url:
                    idx[r["package"]] = url
                    break
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
    git_idx = _read_git_index(eco_dir / "git.csv")

    rows: list[dict] = []
    with open(results_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkg = r[pkg_col]
            rows.append({
                "package": pkg,
                "ecosystem": ecosystem,
                "github_repo": _normalise_repo(r.get("github_repo", "")),
                "git_url": git_idx.get(pkg, ""),
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


def _group_key(row: dict) -> str:
    """github_repo if non-empty; otherwise a synthetic per-package key."""
    return row["github_repo"] or f"__orphan__:{row['ecosystem']}:{row['package']}"


def aggregate_by_repo(all_rows: list[dict]) -> list[dict]:
    """Collapse per-package rows into one row per repo (or per orphan).

    Implements: per-ecosystem PR sum → cumulative-share ranking → A/B/C/D,
    plus top_eco / top_eco_pkg / top_eco_pct, the cross-ecosystem
    `class` (strongest), is_eol (True only if every constituent package
    is is_eol), and the comma-separated `ecosystems` list.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in all_rows:
        groups[_group_key(r)].append(r)

    aggs: list[dict] = []
    for key, members in groups.items():
        a: dict = {
            "group_key": key,
            "github_repo": members[0]["github_repo"],
            "git_url": next((m["git_url"] for m in members if m.get("git_url")), ""),
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
                top_pkg = max(
                    eco_rows, key=lambda m: float(m.get("pagerank") or 0),
                )
                a[f"_top_pkg_{eco}"] = top_pkg["package"]
        a["ecosystems"] = ",".join(present_ecos)
        aggs.append(a)

    # Per ecosystem: rank groups by PR sum desc, compute cumulative share, assign class.
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

    # top_eco / top_eco_pkg / top_eco_pct + cross-eco strongest `class`
    for a in aggs:
        pcts = {e: a[f"_pr_pct_{e}"] for e in ECOSYSTEMS if f"_pr_pct_{e}" in a}
        if pcts:
            top = max(pcts, key=pcts.get)
            a["top_eco"] = top
            a["top_eco_pkg"] = a.get(f"_top_pkg_{top}", "")
            a["top_eco_pct"] = round(pcts[top], 4)
        else:
            a["top_eco"] = ""
            a["top_eco_pkg"] = ""
            a["top_eco_pct"] = ""
        present_classes = [a[f"class_{e}"] for e in ECOSYSTEMS if a[f"class_{e}"]]
        a["class"] = (
            min(present_classes, key=lambda c: CLASS_RANK[c]) if present_classes else ""
        )

    # Sort by top_eco_pct desc; ties broken by repo name for stability
    def sort_key(a: dict) -> tuple:
        pct = a["top_eco_pct"] if isinstance(a["top_eco_pct"], (int, float)) else -1
        return (-pct, a["github_repo"] or a["group_key"])

    aggs.sort(key=sort_key)
    for i, a in enumerate(aggs, 1):
        a["id"] = i
    return aggs


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


def _print_repo_class_table(aggs: list[dict]) -> None:
    table = Table(title="[bold]Repo class distribution[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("class")
    for eco in ECOSYSTEMS:
        table.add_column(eco, justify="right")
    table.add_column("strongest", justify="right", style="bold")
    for cls in ("A", "B", "C", "D"):
        row = [cls]
        for eco in ECOSYSTEMS:
            n = sum(1 for a in aggs if a[f"class_{eco}"] == cls)
            row.append(f"{n:,}")
        n_strongest = sum(1 for a in aggs if a["class"] == cls)
        row.append(f"{n_strongest:,}")
        table.add_row(*row)
    console.print(table)


def main() -> None:
    console.print("[bold]Unifying value-pipeline results...[/bold]\n")

    all_rows: list[dict] = []
    stats_per_eco: list[dict] = []
    for ecosystem, pkg_col in _ECOSYSTEM_SPECS:
        rows, stats = collect_ecosystem(ecosystem, pkg_col)
        all_rows.extend(rows)
        stats_per_eco.append(stats)

    aggs = aggregate_by_repo(all_rows)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(aggs)

    _print_funnel_table(stats_per_eco)
    console.print()
    _print_class_table(stats_per_eco)
    console.print()
    _print_eol_table(stats_per_eco)
    console.print()
    _print_repo_class_table(aggs)
    console.print()
    n_grouped = sum(1 for a in aggs if a["github_repo"])
    n_orphan = len(aggs) - n_grouped
    console.print(
        f"[dim]Written {len(aggs):,} repo rows "
        f"({n_grouped:,} github groups + {n_orphan:,} orphan packages) "
        f"→ {OUTPUT_FILE}[/dim]"
    )


if __name__ == "__main__":
    main()
