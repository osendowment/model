"""
process_data.py — Build npm analysis outputs from raw data.

Pipeline:
  1. top-packages.csv    — packages covering TOP_THRESHOLD_PCT% of total downloads
  2. Iteratively expand dep tree, fetching missing deps from npm registry
  3. Fetch missing downloads for all dep tree nodes
  4. dependency-tree.csv — transitive edges from top packages
  5. github-repos.csv    — package → owner/repo slug from nice-registry
  6. results.csv         — pagerank over dep graph with download signals

Inputs:
  data/sources/npm/raw/downloads.csv          — package, year, downloads
  data/sources/npm/raw/dependencies.csv       — package, dep_name, dep_version, fetched_at
  data/sources/npm/nice-registry/packages.csv — package, repo_url

Run:
    uv run src/npm/process_data.py
    uv run src/npm/process_data.py --ignore-gaps   # skip fetching, use what's available
    uv run src/npm/process_data.py --concurrency 20
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime

import networkx as nx
from rich.console import Console
from rich.table import Table

# fetch_npm_data is in the same directory
sys.path.insert(0, os.path.dirname(__file__))
from fetch_npm_data import fetch_and_save_deps, fetch_and_save_downloads, load_fetched_dep_packages  # noqa: E402

from src.pipeline.common.params import TOP_THRESHOLD_PCT, PAGERANK_ALPHA, YEARS, assign_value_class, ecosystem_avg_downloads

console = Console()

RAW_DOWNLOADS = "data/sources/npm/raw/downloads.csv"
RAW_DEPS      = "data/sources/npm/raw/dependencies.csv"
NICE_REGISTRY = "data/sources/npm/nice-registry/packages.csv"
OUT_TOP       = "data/sources/npm/top-packages.csv"
OUT_DEP_TREE  = "data/sources/npm/dependency-tree.csv"
OUT_GITHUB    = "data/sources/npm/github-repos.csv"
OUT_RESULTS   = "data/sources/npm/results.csv"
WIDE_FIELDS   = ["package", "avg_downloads", "avg_downloads_share"] + [str(y) for y in YEARS]


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
    data: dict[str, dict[int, int]] = defaultdict(dict)
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yr = int(row["year"])
            if yr in YEARS:
                data[row["package"]][yr] = int(row["downloads"]) if row["downloads"] else 0
    return data


def load_raw_deps() -> list[tuple[str, str, str]]:
    if not os.path.exists(RAW_DEPS):
        return []
    seen: set[tuple[str, str]] = set()
    edges = []
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["package"], row["dep_name"])
            if key not in seen:
                seen.add(key)
                edges.append((row["package"], row["dep_name"], row["dep_version"]))
    return edges


def compute_avg(year_vals: dict[int, int]) -> int:
    populated = [year_vals.get(yr, 0) for yr in YEARS if year_vals.get(yr, 0) > 0]
    return int(sum(populated) / len(populated)) if populated else 0


def wide_row(pkg: str, year_vals: dict[int, int]) -> dict:
    return {"package": pkg, "avg_downloads": compute_avg(year_vals),
            **{str(yr): year_vals.get(yr, 0) for yr in YEARS}}


def extract_github_slug(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com/" in url:
        parts = url.split("github.com/", 1)[1].strip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return ""


def build_dep_tree(
    top_packages: set[str], all_edges: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pkg, dep, ver in all_edges:
        adj[pkg].append((dep, ver))

    visited = set(top_packages)
    queue   = deque(top_packages)
    result  = []
    while queue:
        pkg = queue.popleft()
        for dep, ver in adj[pkg]:
            if dep and dep != "__none__":
                result.append((pkg, dep, ver))
                if dep not in visited:
                    visited.add(dep)
                    queue.append(dep)
    return result


# ── pipeline steps ─────────────────────────────────────────────────────────────

def step_top_packages(raw: dict[str, dict[int, int]]) -> set[str]:
    console.rule("[bold cyan]Step 1 — top-packages.csv")
    t0 = time.perf_counter()

    # Build rows for all packages with nonzero avg downloads, sorted descending
    all_rows = sorted(
        [wide_row(pkg, yv) for pkg, yv in raw.items() if compute_avg(yv) > 0],
        key=lambda r: r["avg_downloads"], reverse=True,
    )

    # Find cutoff where cumulative downloads reach TOP_THRESHOLD_PCT% of ecosystem total
    eco_total = ecosystem_avg_downloads("npm")
    target = eco_total * TOP_THRESHOLD_PCT / 100
    cum = 0
    cutoff_idx = len(all_rows)
    for i, r in enumerate(all_rows):
        cum += r["avg_downloads"]
        if cum >= target:
            cutoff_idx = i + 1
            break

    rows = all_rows[:cutoff_idx]

    # Add avg_downloads_share (fraction of ecosystem total)
    for r in rows:
        r["avg_downloads_share"] = f"{r['avg_downloads'] / eco_total:.8f}" if eco_total else "0"

    atomic_write(OUT_TOP, rows, WIDE_FIELDS)
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Threshold",    f"top {TOP_THRESHOLD_PCT}% of downloads")
    tbl.add_row("Top packages", f"{len(rows):,}")
    tbl.add_row("Min avg DL",   f"{rows[-1]['avg_downloads']:,}" if rows else "0")
    tbl.add_row("Elapsed",      f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return {r["package"] for r in rows}


def step_build_dep_tree(top_packages: set[str], concurrency: int, ignore_gaps: bool) -> list[tuple[str, str, str]]:
    console.rule("[bold cyan]Step 2 — build dep tree (fetch missing deps)")
    for iteration in range(1, 100):
        all_edges  = load_raw_deps()
        tree_edges = build_dep_tree(top_packages, all_edges)
        tree_nodes = {n for p, d, _ in tree_edges for n in (p, d)} | top_packages
        need_deps  = tree_nodes - load_fetched_dep_packages()

        tbl = Table(show_header=False, box=None, padding=(0, 2))
        tbl.add_column(style="dim")
        tbl.add_column(justify="right")
        tbl.add_row(f"Iteration {iteration} — tree nodes", f"{len(tree_nodes):,}")
        tbl.add_row("Missing dep fetch",
                    f"[yellow]{len(need_deps):,}[/yellow]" if need_deps else "[green]0[/green]")
        console.print(tbl)

        if not need_deps:
            console.print("  [bold green]Dep tree complete.[/bold green]")
            break
        if ignore_gaps:
            console.print(f"  [dim]--ignore-gaps: {len(need_deps):,} packages without deps[/dim]")
            break

        console.print(f"  Fetching deps for {len(need_deps):,} packages …")
        fetch_and_save_deps(sorted(need_deps), concurrency)

    return tree_edges


def step_fetch_downloads(tree_nodes: set[str], concurrency: int, ignore_gaps: bool) -> dict[str, dict[int, int]]:
    console.rule("[bold cyan]Step 3 — downloads for dep tree")
    raw        = load_raw_downloads()
    missing_dl = tree_nodes - set(raw.keys())

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Tree nodes",        f"{len(tree_nodes):,}")
    tbl.add_row("Missing downloads",
                f"[yellow]{len(missing_dl):,}[/yellow]" if missing_dl else "[green]0[/green]")
    tbl.add_row("Coverage",
                f"{(len(tree_nodes) - len(missing_dl)) / max(len(tree_nodes), 1) * 100:.1f}%")
    console.print(tbl)

    if missing_dl and not ignore_gaps:
        console.print(f"  Fetching downloads for {len(missing_dl):,} packages …")
        fetch_and_save_downloads(sorted(missing_dl), concurrency)
        raw = load_raw_downloads()
    elif missing_dl:
        console.print(f"  [dim]--ignore-gaps: skipping {len(missing_dl):,} missing downloads[/dim]")

    return raw


def step_write_dep_tree(top_packages: set[str]) -> list[tuple[str, str, str]]:
    console.rule("[bold cyan]Step 4 — dependency-tree.csv")
    t0         = time.perf_counter()
    all_edges  = load_raw_deps()
    tree_edges = build_dep_tree(top_packages, all_edges)
    rows       = [{"package": p, "dependency": d, "type": "declared"} for p, d, _ in tree_edges]
    atomic_write(OUT_DEP_TREE, rows, ["package", "dependency", "type"])
    all_nodes  = {n for p, d, _ in tree_edges for n in (p, d)}
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes",    f"{len(all_nodes):,}")
    tbl.add_row("Elapsed",         f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return tree_edges


def step_github_repos(tree_nodes: set[str]) -> dict[str, str]:
    console.rule("[bold cyan]Step 5 — github-repos.csv")
    t0 = time.perf_counter()
    if not os.path.exists(NICE_REGISTRY):
        console.print(f"[yellow]WARNING:[/yellow] {NICE_REGISTRY} not found — skipping")
        return {}
    pkg_to_repo: dict[str, str] = {}
    with open(NICE_REGISTRY, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["package"] not in tree_nodes:
                continue
            slug = extract_github_slug(row.get("repo_url", ""))
            if slug:
                pkg_to_repo[row["package"]] = slug
    atomic_write(OUT_GITHUB, [{"package": p, "github_repo": s} for p, s in pkg_to_repo.items()],
                 ["package", "github_repo"])
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Tree nodes",              f"{len(tree_nodes):,}")
    tbl.add_row("Packages with repo",      f"{len(pkg_to_repo):,}")
    tbl.add_row("Elapsed",                 f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return pkg_to_repo


def step_results(
    tree_edges: list[tuple[str, str, str]],
    top_packages: set[str],
    raw: dict[str, dict[int, int]],
    github_repos: dict[str, str],
) -> None:
    console.rule("[bold cyan]Step 6 — results.csv (pagerank)")
    t0 = time.perf_counter()
    G  = nx.DiGraph()
    # Seed with all top packages so orphans (no declared deps, no inbound deps)
    # still get a row in results.csv -- dropping them silently would shrink the
    # value-pipeline funnel below the top-packages count.
    G.add_nodes_from(top_packages)
    for pkg, dep, _ in tree_edges:
        G.add_edge(pkg, dep)

    all_nodes = set(G.nodes())
    total_dl  = sum(compute_avg(raw.get(n, {})) for n in all_nodes)
    personalization = (
        {n: compute_avg(raw.get(n, {})) / total_dl for n in all_nodes} if total_dl > 0 else None
    )
    pr = nx.pagerank(G, alpha=PAGERANK_ALPHA, personalization=personalization)

    rows = []
    for pkg in all_nodes:
        yv = raw.get(pkg, {})
        rows.append({
            "package":       pkg,
            "github_repo":   github_repos.get(pkg, ""),
            "avg_downloads": compute_avg(yv),
            **{str(yr): yv.get(yr, 0) for yr in YEARS},
            "top":           str(pkg in top_packages),
            "pagerank":      f"{pr.get(pkg, 0.0):.8f}",
        })
    rows.sort(key=lambda r: float(r["pagerank"]), reverse=True)

    # Assign value classes by cumulative pagerank share
    total_pr = sum(float(r["pagerank"]) for r in rows)
    cum = 0.0
    for r in rows:
        cum += float(r["pagerank"])
        share = cum / total_pr if total_pr > 0 else 1.0
        r["value_class"] = assign_value_class(share)

    fields = ["package", "github_repo", "avg_downloads"] + [str(y) for y in YEARS] + ["top", "pagerank", "value_class"]
    atomic_write(OUT_RESULTS, rows, fields)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column(justify="right")
    tbl.add_row("Nodes", f"{G.number_of_nodes():,}")
    tbl.add_row("Edges", f"{G.number_of_edges():,}")
    tbl.add_row("Elapsed", f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 10 by PageRank", header_style="bold green")
    preview.add_column("Package")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg DL",   justify="right")
    preview.add_column("Top?",     justify="center")
    for r in rows[:10]:
        preview.add_row(r["package"], r["pagerank"], f"{r['avg_downloads']:,}",
                        "[green]yes[/green]" if r["top"] == "True" else "[dim]no[/dim]")
    console.print(preview)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ignore-gaps",  action="store_true",
                        help="Skip fetching; calculate on available data")
    parser.add_argument("--concurrency",  type=int, default=5)
    args = parser.parse_args()

    console.rule("[bold white]npm — process_data.py")
    console.print(f"  Started     : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Top threshold: [cyan]{TOP_THRESHOLD_PCT}% of downloads[/cyan]")
    console.print(f"  ignore-gaps : [cyan]{args.ignore_gaps}[/cyan]")
    console.print()

    t_total      = time.perf_counter()
    raw          = load_raw_downloads()
    top_packages = step_top_packages(raw)
    tree_edges   = step_build_dep_tree(top_packages, args.concurrency, args.ignore_gaps)
    tree_nodes   = {n for p, d, _ in tree_edges for n in (p, d)} | top_packages
    raw          = step_fetch_downloads(tree_nodes, args.concurrency, args.ignore_gaps)
    tree_edges   = step_write_dep_tree(top_packages)
    github_repos = step_github_repos(tree_nodes)
    step_results(tree_edges, top_packages, raw, github_repos)

    console.rule()
    console.print(f"  [bold green]Done[/bold green] — {time.perf_counter() - t_total:.2f}s")


if __name__ == "__main__":
    main()
