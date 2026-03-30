"""
process_data.py — Reads raw npm data, produces analysis-ready derived files.

Outputs (all to data/npm/):
  packages-summary.csv   — wide format, one row per package, avg + yearly downloads
  top-packages.csv       — same format, only top packages
  dependency-tree.csv    — transitive dep edges rooted at top packages
  github-repos.csv       — package → github_repo from nice-registry/metadata.csv
  results.csv            — pagerank over dependency graph
"""

import argparse
import csv
import os
import time
from collections import defaultdict, deque
from datetime import datetime

import networkx as nx
from rich.console import Console
from rich.table import Table

console = Console()

# ── constants ──────────────────────────────────────────────────────────────────
YEARS = [2021, 2022, 2023, 2024, 2025]
ALPHA = 0.85

RAW_DOWNLOADS = "data/npm/raw/downloads.csv"
RAW_DEPS      = "data/npm/raw/dependencies.csv"
TOP_PACKAGES  = "data/npm/top-package-downloads.csv"
NICE_REGISTRY = "data/npm/nice-registry/metadata.csv"

OUT_SUMMARY   = "data/npm/packages-summary.csv"
OUT_TOP       = "data/npm/top-packages.csv"
OUT_DEP_TREE  = "data/npm/dependency-tree.csv"
OUT_GITHUB    = "data/npm/github-repos.csv"
OUT_RESULTS   = "data/npm/results.csv"


# ── helpers ────────────────────────────────────────────────────────────────────

def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    """Write CSV atomically via a .tmp file."""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def load_raw_downloads() -> dict[str, dict[int, int]]:
    """Return {package: {year: downloads}}."""
    data: dict[str, dict[int, int]] = defaultdict(dict)
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pkg = row["package"]
            yr = int(row["year"])
            dl = int(row["downloads"]) if row["downloads"] else 0
            if yr in YEARS:
                data[pkg][yr] = dl
    return data


def load_top_package_names() -> dict[str, dict]:
    """Return {package: {year: downloads, avg_downloads: int}} from top-package-downloads.csv."""
    top: dict[str, dict] = {}
    with open(TOP_PACKAGES, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pkg = row["package"]
            top[pkg] = {
                "avg_downloads": int(row["avg_downloads"]) if row["avg_downloads"] else 0,
                **{yr: int(row[str(yr)]) if row.get(str(yr)) else 0 for yr in YEARS},
            }
    return top


def load_raw_deps() -> list[tuple[str, str, str]]:
    """Return list of (package, dep_name, dep_version)."""
    edges = []
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            edges.append((row["package"], row["dep_name"], row["dep_version"]))
    return edges


def compute_avg(year_vals: dict[int, int]) -> int:
    vals = [year_vals.get(yr, 0) for yr in YEARS]
    populated = [v for v in vals if v > 0]
    if not populated:
        return 0
    return int(sum(populated) / len(populated))


def wide_row(pkg: str, year_vals: dict[int, int]) -> dict:
    avg = compute_avg(year_vals)
    return {"package": pkg, "avg_downloads": avg, **{str(yr): year_vals.get(yr, 0) for yr in YEARS}}


WIDE_FIELDS = ["package", "avg_downloads"] + [str(y) for y in YEARS]


# ── step 1: packages-summary ───────────────────────────────────────────────────

def step_packages_summary() -> dict[str, dict[int, int]]:
    console.rule("[bold cyan]packages-summary.csv")
    t0 = time.perf_counter()

    raw = load_raw_downloads()
    rows = sorted(
        [wide_row(pkg, yv) for pkg, yv in raw.items()],
        key=lambda r: r["avg_downloads"],
        reverse=True,
    )
    atomic_write(OUT_SUMMARY, rows, WIDE_FIELDS)

    elapsed = time.perf_counter() - t0
    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Packages", f"{len(rows):,}")
    tbl.add_row("Output", OUT_SUMMARY)
    tbl.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(tbl)

    return raw


# ── step 2: top-packages ───────────────────────────────────────────────────────

def step_top_packages(raw: dict[str, dict[int, int]]) -> set[str]:
    console.rule("[bold cyan]top-packages.csv")
    t0 = time.perf_counter()

    top_data = load_top_package_names()
    rows = []
    fallback_count = 0

    for pkg, top_vals in top_data.items():
        if pkg in raw:
            row = wide_row(pkg, raw[pkg])
        else:
            # fallback to top-package-downloads values
            row = {"package": pkg, "avg_downloads": top_vals["avg_downloads"],
                   **{str(yr): top_vals[yr] for yr in YEARS}}
            fallback_count += 1
        rows.append(row)

    rows.sort(key=lambda r: r["avg_downloads"], reverse=True)
    atomic_write(OUT_TOP, rows, WIDE_FIELDS)

    elapsed = time.perf_counter() - t0
    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Top packages", f"{len(rows):,}")
    tbl.add_row("From raw/downloads", f"{len(rows) - fallback_count:,}")
    tbl.add_row("Fallback (top-pkg-dl)", f"{fallback_count:,}")
    tbl.add_row("Output", OUT_TOP)
    tbl.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(tbl)

    return set(top_data.keys())


# ── step 3: dependency-tree (BFS) ─────────────────────────────────────────────

def build_dep_tree(
    top_packages: set[str], all_edges: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pkg, dep, ver in all_edges:
        adj[pkg].append((dep, ver))

    visited = set(top_packages)
    queue = deque(top_packages)
    result_edges = []

    while queue:
        pkg = queue.popleft()
        for dep, ver in adj[pkg]:
            if dep and dep != "__none__":
                result_edges.append((pkg, dep, ver))
                if dep not in visited:
                    visited.add(dep)
                    queue.append(dep)

    return result_edges


def step_dependency_tree(top_packages: set[str]) -> list[tuple[str, str, str]]:
    console.rule("[bold cyan]dependency-tree.csv")
    t0 = time.perf_counter()

    all_edges = load_raw_deps()
    tree_edges = build_dep_tree(top_packages, all_edges)

    rows = [{"package": p, "dep_name": d, "dep_version": v} for p, d, v in tree_edges]
    atomic_write(OUT_DEP_TREE, rows, ["package", "dep_name", "dep_version"])

    all_nodes = set()
    for p, d, _ in tree_edges:
        all_nodes.add(p)
        all_nodes.add(d)

    elapsed = time.perf_counter() - t0
    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Total edges", f"{len(all_edges):,}")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes", f"{len(all_nodes):,}")
    tbl.add_row("Output", OUT_DEP_TREE)
    tbl.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(tbl)

    return tree_edges


# ── step 4: github-repos ──────────────────────────────────────────────────────

def step_github_repos() -> None:
    console.rule("[bold cyan]github-repos.csv")
    t0 = time.perf_counter()

    if not os.path.exists(NICE_REGISTRY):
        console.print(
            f"[yellow]WARNING:[/yellow] {NICE_REGISTRY} not found — skipping github-repos.csv"
        )
        return

    rows = []
    with open(NICE_REGISTRY, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gr = row.get("github_repo", "").strip()
            if gr:
                rows.append({"package": row["package"], "github_repo": gr})

    atomic_write(OUT_GITHUB, rows, ["package", "github_repo"])

    elapsed = time.perf_counter() - t0
    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Packages with github_repo", f"{len(rows):,}")
    tbl.add_row("Output", OUT_GITHUB)
    tbl.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(tbl)


# ── step 5: pagerank ──────────────────────────────────────────────────────────

def step_pagerank(
    tree_edges: list[tuple[str, str, str]],
    top_packages: set[str],
    raw: dict[str, dict[int, int]],
) -> None:
    console.rule("[bold cyan]results.csv (pagerank)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep, _ in tree_edges:
        G.add_edge(pkg, dep)

    # Standard pagerank
    pr = nx.pagerank(G, alpha=ALPHA)

    # Personalized pagerank — seed on download weight
    all_nodes = set(G.nodes())
    total_dl = sum(
        compute_avg(raw.get(n, {})) for n in all_nodes
    )
    if total_dl > 0:
        personalization = {
            n: compute_avg(raw.get(n, {})) / total_dl for n in all_nodes
        }
    else:
        personalization = None

    pr_dl = nx.pagerank(G, alpha=ALPHA, personalization=personalization)

    rows = []
    for pkg in all_nodes:
        avg_dl = compute_avg(raw.get(pkg, {}))
        rows.append({
            "package": pkg,
            "pagerank": pr.get(pkg, 0.0),
            "pagerank_dl": pr_dl.get(pkg, 0.0),
            "avg_downloads": avg_dl,
            "in_top_list": int(pkg in top_packages),
        })

    rows.sort(key=lambda r: r["pagerank"], reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    fields = ["rank", "package", "pagerank", "pagerank_dl", "avg_downloads", "in_top_list"]
    atomic_write(OUT_RESULTS, rows, fields)

    elapsed = time.perf_counter() - t0
    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Nodes in graph", f"{G.number_of_nodes():,}")
    tbl.add_row("Edges in graph", f"{G.number_of_edges():,}")
    tbl.add_row("Output", OUT_RESULTS)
    tbl.add_row("Elapsed", f"{elapsed:.2f}s")
    console.print(tbl)

    # Preview top 10
    preview = Table(title="Top 10 by PageRank", show_header=True, header_style="bold green")
    preview.add_column("Rank", justify="right", style="dim")
    preview.add_column("Package")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg DL", justify="right")
    preview.add_column("Top?", justify="center")
    for r in rows[:10]:
        preview.add_row(
            str(r["rank"]),
            r["package"],
            f"{r['pagerank']:.6f}",
            f"{r['avg_downloads']:,}",
            "[green]yes[/green]" if r["in_top_list"] else "[dim]no[/dim]",
        )
    console.print(preview)


# ── completeness check ────────────────────────────────────────────────────────

def check_gaps(raw: dict[str, dict[int, int]], ignore_gaps: bool) -> None:
    """Warn (or exit) if dep packages are missing from raw/downloads."""
    if not os.path.exists(RAW_DEPS):
        return
    dep_names: set[str] = set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("dep_name", "")
            if d and d != "__none__":
                dep_names.add(d)

    missing = dep_names - set(raw.keys())
    total = len(dep_names)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Dep packages in graph", f"{total:,}")
    tbl.add_row("Missing download data", f"[yellow]{len(missing):,}[/yellow]" if missing else "[green]0[/green]")
    tbl.add_row("Coverage", f"{(total - len(missing)) / max(total, 1) * 100:.1f}%")
    console.print(tbl)

    if missing and not ignore_gaps:
        console.print(
            f"\n[red]Data incomplete:[/red] {len(missing):,} dep packages have no download data.\n"
            f"Run [cyan]uv run src/npm/fetch_npm_data.py[/cyan] to fill gaps, "
            f"or pass [cyan]--ignore-gaps[/cyan] to proceed anyway."
        )
        raise SystemExit(1)
    if missing and ignore_gaps:
        console.print(f"[dim]--ignore-gaps: skipping {len(missing):,} packages with no data[/dim]")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Process raw npm data into analysis-ready files.")
    parser.add_argument("--summary",      action="store_true", help="Run packages-summary step only")
    parser.add_argument("--top",          action="store_true", help="Run top-packages step only")
    parser.add_argument("--deptree",      action="store_true", help="Run dependency-tree step only")
    parser.add_argument("--github",       action="store_true", help="Run github-repos step only")
    parser.add_argument("--pagerank",     action="store_true", help="Run pagerank/results step only")
    parser.add_argument("--ignore-gaps",  action="store_true",
                        help="Skip completeness check and calculate results on available data")
    args = parser.parse_args()

    # If no step flags, run everything
    run_all = not any([args.summary, args.top, args.deptree, args.github, args.pagerank])

    console.rule("[bold white]npm process_data.py")
    console.print(f"  Started:     [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  YEARS:       [cyan]{YEARS}[/cyan]")
    console.print(f"  ALPHA:       [cyan]{ALPHA}[/cyan]")
    console.print(f"  ignore-gaps: [cyan]{args.ignore_gaps}[/cyan]")
    console.print()

    t_total = time.perf_counter()

    # Load shared data lazily
    raw: dict[str, dict[int, int]] = {}
    top_packages: set[str] = set()
    tree_edges: list[tuple[str, str, str]] = []

    if run_all or args.summary:
        raw = step_packages_summary()
    if run_all or args.top or args.deptree or args.pagerank:
        if not raw:
            raw = load_raw_downloads()

    console.rule("[bold]Data completeness")
    check_gaps(raw, args.ignore_gaps)
    console.print()

    if run_all or args.top:
        top_packages = step_top_packages(raw)
    if run_all or args.deptree or args.pagerank:
        if not top_packages:
            top_packages = set(load_top_package_names().keys())

    if run_all or args.deptree:
        tree_edges = step_dependency_tree(top_packages)
    if run_all or args.pagerank:
        if not tree_edges and os.path.exists(OUT_DEP_TREE):
            with open(OUT_DEP_TREE, newline="", encoding="utf-8") as f:
                tree_edges = [(r["package"], r["dep_name"], r["dep_version"]) for r in csv.DictReader(f)]

    if run_all or args.github:
        step_github_repos()

    if run_all or args.pagerank:
        step_pagerank(tree_edges, top_packages, raw)

    console.rule()
    console.print(
        f"  [bold green]Done[/bold green] — total elapsed: "
        f"[cyan]{time.perf_counter() - t_total:.2f}s[/cyan]"
    )


if __name__ == "__main__":
    main()
