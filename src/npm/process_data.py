"""
process_data.py — Reads raw npm data, produces analysis-ready derived files.

Inputs:
  data/npm/raw/downloads.csv       — long format: package, year, downloads
  data/npm/raw/dependencies.csv    — package, dep_name, dep_version, fetched_at
  data/npm/nice-registry/packages.csv — package, repo_url

Outputs (all to data/npm/):
  top-packages.csv       — wide format, packages with avg_downloads >= 1M
  dependency-tree.csv    — transitive dep edges (package, dependency, type)
  github-repos.csv       — package, github_repo (owner/repo slug)
  results.csv            — package, github_repo, downloads, top, pagerank, pagerank_dl
"""

import argparse
import csv
import os
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime

import networkx as nx
from rich.console import Console
from rich.table import Table

console = Console()

# ── constants ──────────────────────────────────────────────────────────────────
YEARS         = [2021, 2022, 2023, 2024, 2025]
ALPHA         = 0.85
TOP_MIN_AVG   = 1_000_000   # min avg annual downloads to be a "top" package

RAW_DOWNLOADS = "data/npm/raw/downloads.csv"
RAW_DEPS      = "data/npm/raw/dependencies.csv"
NICE_REGISTRY = "data/npm/nice-registry/packages.csv"

OUT_TOP       = "data/npm/top-packages.csv"
OUT_DEP_TREE  = "data/npm/dependency-tree.csv"
OUT_GITHUB    = "data/npm/github-repos.csv"
OUT_RESULTS   = "data/npm/results.csv"

WIDE_FIELDS   = ["package", "avg_downloads"] + [str(y) for y in YEARS]


# ── helpers ────────────────────────────────────────────────────────────────────

def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
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


def load_raw_deps() -> list[tuple[str, str, str]]:
    """Return list of (package, dep_name, dep_version)."""
    edges = []
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            edges.append((row["package"], row["dep_name"], row["dep_version"]))
    return edges


def compute_avg(year_vals: dict[int, int]) -> int:
    populated = [year_vals.get(yr, 0) for yr in YEARS if year_vals.get(yr, 0) > 0]
    return int(sum(populated) / len(populated)) if populated else 0


def wide_row(pkg: str, year_vals: dict[int, int]) -> dict:
    avg = compute_avg(year_vals)
    return {"package": pkg, "avg_downloads": avg, **{str(yr): year_vals.get(yr, 0) for yr in YEARS}}


def extract_github_slug(url: str) -> str:
    """Extract owner/repo from a GitHub URL."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com/" in url:
        slug = url.split("github.com/", 1)[1].strip("/")
        parts = slug.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return ""


# ── step 1: top-packages ───────────────────────────────────────────────────────

def step_top_packages(raw: dict[str, dict[int, int]]) -> set[str]:
    console.rule("[bold cyan]top-packages.csv")
    t0 = time.perf_counter()

    rows = [
        wide_row(pkg, yv) for pkg, yv in raw.items()
        if compute_avg(yv) >= TOP_MIN_AVG
    ]
    rows.sort(key=lambda r: r["avg_downloads"], reverse=True)
    atomic_write(OUT_TOP, rows, WIDE_FIELDS)

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Threshold", f"avg >= {TOP_MIN_AVG:,}")
    tbl.add_row("Top packages", f"{len(rows):,}")
    tbl.add_row("Output", OUT_TOP)
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    return {r["package"] for r in rows}


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

    rows = [{"package": p, "dependency": d, "type": "declared"} for p, d, _ in tree_edges]
    atomic_write(OUT_DEP_TREE, rows, ["package", "dependency", "type"])

    all_nodes = {n for p, d, _ in tree_edges for n in (p, d)}

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Total edges", f"{len(all_edges):,}")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes", f"{len(all_nodes):,}")
    tbl.add_row("Output", OUT_DEP_TREE)
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    return tree_edges


# ── step 4: github-repos ──────────────────────────────────────────────────────

def step_github_repos() -> dict[str, str]:
    """Return {package: owner/repo} and write github-repos.csv."""
    console.rule("[bold cyan]github-repos.csv")
    t0 = time.perf_counter()

    if not os.path.exists(NICE_REGISTRY):
        console.print(f"[yellow]WARNING:[/yellow] {NICE_REGISTRY} not found — skipping")
        return {}

    pkg_to_repo: dict[str, str] = {}
    with open(NICE_REGISTRY, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("repo_url", "").strip()
            slug = extract_github_slug(url)
            if slug:
                pkg_to_repo[row["package"]] = slug

    rows = [{"package": pkg, "github_repo": slug} for pkg, slug in pkg_to_repo.items()]
    atomic_write(OUT_GITHUB, rows, ["package", "github_repo"])

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Packages with github repo", f"{len(rows):,}")
    tbl.add_row("Output", OUT_GITHUB)
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    return pkg_to_repo


# ── step 5: results (pagerank) ────────────────────────────────────────────────

def step_results(
    tree_edges: list[tuple[str, str, str]],
    top_packages: set[str],
    raw: dict[str, dict[int, int]],
    github_repos: dict[str, str],
) -> None:
    console.rule("[bold cyan]results.csv (pagerank)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep, _ in tree_edges:
        G.add_edge(pkg, dep)

    pr = nx.pagerank(G, alpha=ALPHA)

    all_nodes = set(G.nodes())
    total_dl = sum(compute_avg(raw.get(n, {})) for n in all_nodes)
    personalization = (
        {n: compute_avg(raw.get(n, {})) / total_dl for n in all_nodes}
        if total_dl > 0 else None
    )
    pr_dl = nx.pagerank(G, alpha=ALPHA, personalization=personalization)

    rows = []
    for pkg in all_nodes:
        yv = raw.get(pkg, {})
        rows.append({
            "package":      pkg,
            "github_repo":  github_repos.get(pkg, ""),
            "avg_downloads": compute_avg(yv),
            **{str(yr): yv.get(yr, 0) for yr in YEARS},
            "top":          str(pkg in top_packages),
            "pagerank":     f"{pr.get(pkg, 0.0):.8f}",
            "pagerank_dl":  f"{pr_dl.get(pkg, 0.0):.8f}",
        })

    rows.sort(key=lambda r: float(r["pagerank"]), reverse=True)

    fields = ["package", "github_repo", "avg_downloads"] + [str(y) for y in YEARS] + ["top", "pagerank", "pagerank_dl"]
    atomic_write(OUT_RESULTS, rows, fields)

    tbl = Table(show_header=True, header_style="bold magenta")
    tbl.add_column("Metric", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Nodes in graph", f"{G.number_of_nodes():,}")
    tbl.add_row("Edges in graph", f"{G.number_of_edges():,}")
    tbl.add_row("Output", OUT_RESULTS)
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 10 by PageRank", show_header=True, header_style="bold green")
    preview.add_column("Package")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg DL", justify="right")
    preview.add_column("Top?", justify="center")
    for r in rows[:10]:
        preview.add_row(
            r["package"],
            r["pagerank"],
            f"{r['avg_downloads']:,}",
            "[green]yes[/green]" if r["top"] == "True" else "[dim]no[/dim]",
        )
    console.print(preview)


# ── completeness check ────────────────────────────────────────────────────────

def check_gaps(raw: dict[str, dict[int, int]], ignore_gaps: bool) -> None:
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
    tbl.add_row("Missing download data",
                f"[yellow]{len(missing):,}[/yellow]" if missing else "[green]0[/green]")
    tbl.add_row("Coverage", f"{(total - len(missing)) / max(total, 1) * 100:.1f}%")
    console.print(tbl)

    if missing and not ignore_gaps:
        console.print(
            f"\n[yellow]Data incomplete:[/yellow] {len(missing):,} dep packages have no download data. "
            f"Running fetch_npm_data.py …\n"
        )
        result = subprocess.run(
            ["uv", "run", "src/npm/fetch_npm_data.py"],
            check=False,
        )
        if result.returncode != 0:
            console.print("[red]fetch_npm_data.py failed — re-run manually or use --ignore-gaps[/red]")
            raise SystemExit(1)
    if missing and ignore_gaps:
        console.print(f"[dim]--ignore-gaps: skipping {len(missing):,} packages with no data[/dim]")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Process raw npm data into analysis-ready files.")
    parser.add_argument("--top",         action="store_true", help="Run top-packages step only")
    parser.add_argument("--deptree",     action="store_true", help="Run dependency-tree step only")
    parser.add_argument("--github",      action="store_true", help="Run github-repos step only")
    parser.add_argument("--pagerank",    action="store_true", help="Run results/pagerank step only")
    parser.add_argument("--ignore-gaps", action="store_true",
                        help="Skip completeness check and calculate on available data")
    args = parser.parse_args()

    run_all = not any([args.top, args.deptree, args.github, args.pagerank])

    console.rule("[bold white]npm process_data.py")
    console.print(f"  Started:     [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  YEARS:       [cyan]{YEARS}[/cyan]")
    console.print(f"  ALPHA:       [cyan]{ALPHA}[/cyan]")
    console.print(f"  TOP_MIN_AVG: [cyan]{TOP_MIN_AVG:,}[/cyan]")
    console.print(f"  ignore-gaps: [cyan]{args.ignore_gaps}[/cyan]")
    console.print()

    t_total = time.perf_counter()

    raw: dict[str, dict[int, int]] = {}
    top_packages: set[str] = set()
    tree_edges: list[tuple[str, str, str]] = []
    github_repos: dict[str, str] = {}

    if run_all or args.top or args.deptree or args.pagerank:
        raw = load_raw_downloads()

    console.rule("[bold]Data completeness")
    check_gaps(raw, args.ignore_gaps)
    console.print()

    if run_all or args.top:
        top_packages = step_top_packages(raw)

    if run_all or args.deptree or args.pagerank:
        if not top_packages:
            # Recompute from raw without writing
            top_packages = {
                pkg for pkg, yv in raw.items()
                if compute_avg(yv) >= TOP_MIN_AVG
            }

    if run_all or args.deptree:
        tree_edges = step_dependency_tree(top_packages)

    if run_all or args.pagerank:
        if not tree_edges and os.path.exists(OUT_DEP_TREE):
            with open(OUT_DEP_TREE, newline="", encoding="utf-8") as f:
                tree_edges = [
                    (r["package"], r["dependency"], r.get("type", "declared"))
                    for r in csv.DictReader(f)
                ]

    if run_all or args.github:
        github_repos = step_github_repos()

    if run_all or args.pagerank:
        if not github_repos and os.path.exists(OUT_GITHUB):
            with open(OUT_GITHUB, newline="", encoding="utf-8") as f:
                github_repos = {r["package"]: r["github_repo"] for r in csv.DictReader(f)}
        step_results(tree_edges, top_packages, raw, github_repos)

    console.rule()
    console.print(
        f"  [bold green]Done[/bold green] — total elapsed: "
        f"[cyan]{time.perf_counter() - t_total:.2f}s[/cyan]"
    )


if __name__ == "__main__":
    main()
