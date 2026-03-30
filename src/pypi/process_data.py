"""
Process all PyPI data into final output CSVs.

Steps:
  1  Load package downloads from BigQuery export
  2  Load dependency graph from raw deps
  3  Build top-packages.csv       — packages with avg ≥ 1M downloads
  4  Build dependency-tree.csv    — full transitive dep tree from top packages
  5  Build github-repos.csv       — owner/repo for every package in dep tree
  6  Build results.csv            — all dep-tree packages with downloads + PageRank

Reads:
  data/pypi/bigquery/bq-package-downloads.csv
  data/pypi/raw/package-dependencies.csv
  data/pypi/raw/package-github-mapping.csv

Outputs:
  data/pypi/top-packages.csv
  data/pypi/dependency-tree.csv
  data/pypi/github-repos.csv
  data/pypi/results.csv

Run:
    uv run src/pypi/process_data.py
    uv run src/pypi/process_data.py --min-avg 500000
    uv run src/pypi/process_data.py --alpha 0.90
"""

import argparse
import csv
import os
import time
from collections import defaultdict
from urllib.parse import urlparse

import networkx as nx
from rich.console import Console
from rich.table import Table

YEARS   = list(range(2021, 2026))
MIN_AVG = 1_000_000
ALPHA   = 0.85

BQ_CSV      = "data/pypi/bigquery/bq-package-downloads.csv"
DEPS_CSV    = "data/pypi/raw/package-dependencies.csv"
GITHUB_CSV_RAW = "data/pypi/raw/package-github-mapping.csv"
DATA_DIR    = "data/pypi"

TOP_CSV     = f"{DATA_DIR}/top-packages.csv"
DEPTREE_CSV = f"{DATA_DIR}/dependency-tree.csv"
GITHUB_CSV  = f"{DATA_DIR}/github-repos.csv"
RESULTS_CSV = f"{DATA_DIR}/results.csv"

YEAR_COLS = [str(y) for y in YEARS]

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--min-avg", type=int,   default=MIN_AVG)
parser.add_argument("--alpha",   type=float, default=ALPHA)
args = parser.parse_args()

t_total = time.perf_counter()
console.rule("[bold]PyPI — process data")
console.print(f"BigQuery : [cyan]{BQ_CSV}[/cyan]")
console.print(f"Deps     : [cyan]{DEPS_CSV}[/cyan]")
console.print(f"GitHub   : [cyan]{GITHUB_CSV_RAW}[/cyan]")
console.print(f"Years    : {YEARS[0]}–{YEARS[-1]}  |  Min avg: {args.min_avg:,}  |  PR alpha: {args.alpha}")
console.print()


def write_csv(path: str, fieldnames: list[str], rows) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def write_csv_rows(path: str, header: list[str], rows) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


# ── Step 1: Load package downloads from BigQuery export ───────────────────────

console.rule("[bold]Step 1[/bold] — load BigQuery downloads")

t = time.perf_counter()
console.print(f"Loading {BQ_CSV} …")

# pkg_downloads: lowercase_name → {"package": display_name, "avg_downloads": int, year: int, ...}
pkg_downloads: dict[str, dict] = {}
with open(BQ_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        name = row["package"].strip().lower()
        pkg_downloads[name] = {
            "package":       row["package"].strip(),
            "avg_downloads": int(row.get("avg_downloads") or 0),
            **{y: int(row.get(y) or 0) for y in YEAR_COLS},
        }

console.print(f"  {len(pkg_downloads):,} packages  ({time.perf_counter()-t:.1f}s)")


# ── Step 2: Load dependency graph ─────────────────────────────────────────────

console.rule("[bold]Step 2[/bold] — load dependency graph")

t = time.perf_counter()
console.print(f"Loading {DEPS_CSV} …")

# pkg_deps: lowercase_name → [(dep_lowercase, dep_type), ...]
pkg_deps: dict[str, list[tuple[str, str]]] = defaultdict(list)
total_edges = 0
with open(DEPS_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        pkg      = row["package"].strip().lower()
        dep      = row["dependency"].strip().lower()
        dep_type = row.get("type", "").strip() or "declared"
        if pkg and dep:
            pkg_deps[pkg].append((dep, dep_type))
            total_edges += 1

console.print(f"  {total_edges:,} edges across {len(pkg_deps):,} packages  ({time.perf_counter()-t:.1f}s)")


# ── Step 3: top-packages.csv ──────────────────────────────────────────────────

console.rule("[bold]Step 3[/bold] — top-packages.csv")

t = time.perf_counter()
top_rows = []
# top_pkg_names_lower: seed set for transitive dep expansion in Step 4
top_pkg_names_lower: set[str] = set()

for name_lower, data in pkg_downloads.items():
    if data["avg_downloads"] >= args.min_avg:
        top_rows.append({
            "package":       data["package"],
            "avg_downloads": data["avg_downloads"],
            **{y: data[y] for y in YEAR_COLS},
        })
        top_pkg_names_lower.add(name_lower)

top_rows.sort(key=lambda r: r["avg_downloads"], reverse=True)

write_csv(TOP_CSV, ["package", "avg_downloads"] + YEAR_COLS, top_rows)

table = Table(title="Top 10 packages by avg downloads", show_header=True, header_style="bold dim")
table.add_column("Package", style="bold")
table.add_column("Avg", justify="right")
for y in YEARS:
    table.add_column(str(y), justify="right")
for row in top_rows[:10]:
    table.add_row(row["package"], f"{row['avg_downloads']:,}", *[f"{row[y]:,}" for y in YEAR_COLS])
console.print(table)
console.print(f"[green]✓[/green] {len(top_rows):,} packages → {TOP_CSV}  ({time.perf_counter()-t:.1f}s)")


# ── Step 4: dependency-tree.csv (transitive closure from top packages) ─────────

console.rule("[bold]Step 4[/bold] — dependency-tree.csv")

t = time.perf_counter()
seen_edges: set[tuple[str, str, str]] = set()
all_edges:  list[tuple[str, str, str]] = []
# universe: every package reachable from top packages (transitively) — grows each BFS round
universe: set[str] = set(top_pkg_names_lower)
frontier: set[str] = set(top_pkg_names_lower)

# display_name: lowercase → original case from BigQuery export, used for all output
display_name: dict[str, str] = {k: v["package"] for k, v in pkg_downloads.items()}

round_num = 0
while frontier:
    round_num += 1
    new_frontier: set[str] = set()
    for pkg_lower in frontier:
        pkg = display_name.get(pkg_lower, pkg_lower)
        for dep_lower, dep_type in pkg_deps.get(pkg_lower, []):
            # Skip deps not in PyPI download data (OS packages, GitHub Actions, etc.)
            if dep_lower not in pkg_downloads:
                continue
            dep = display_name.get(dep_lower, dep_lower)
            edge = (pkg, dep, dep_type)
            if edge not in seen_edges:
                seen_edges.add(edge)
                all_edges.append(edge)
            if dep_lower not in universe:
                universe.add(dep_lower)
                new_frontier.add(dep_lower)
    console.print(f"  Round {round_num}: {len(frontier):,} → {len(new_frontier):,} new  |  universe: {len(universe):,}")
    frontier = new_frontier

all_edges.sort()
write_csv_rows(DEPTREE_CSV, ["package", "dependency", "type"], all_edges)
console.print(f"[green]✓[/green] {len(all_edges):,} edges, {len(universe):,} packages → {DEPTREE_CSV}  ({time.perf_counter()-t:.1f}s)")


# ── Step 5: github-repos.csv ──────────────────────────────────────────────────

console.rule("[bold]Step 5[/bold] — github-repos.csv")


def parse_owner_repo(url: str) -> str | None:
    """Extract lowercase 'owner/repo' from a GitHub URL. Returns None for non-GitHub or malformed URLs."""
    if not url or "github.com" not in url.lower():
        return None
    try:
        path = urlparse(url.strip()).path.rstrip("/")
    except Exception:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    # Need at least owner + repo; deeper paths (e.g. monorepos) take only the first two segments
    return f"{parts[0].lower()}/{parts[1].lower()}" if len(parts) >= 2 else None


t = time.perf_counter()

# raw_github: package_name_lower → github_url
raw_github: dict[str, str] = {}
with open(GITHUB_CSV_RAW, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        raw_github[row["package"].strip().lower()] = row.get("github_url", "").strip()

gh_rows = []
github_map: dict[str, str] = {}
for pkg_lower in sorted(universe):
    url  = raw_github.get(pkg_lower, "")
    repo = parse_owner_repo(url)
    if repo:
        name = display_name.get(pkg_lower, pkg_lower)
        gh_rows.append((name, repo))
        github_map[name] = repo

write_csv_rows(GITHUB_CSV, ["package", "github_repo"], gh_rows)
console.print(f"  {len(universe):,} in tree | {len(gh_rows):,} with GitHub repo  ({time.perf_counter()-t:.1f}s)")
console.print(f"[green]✓[/green] {len(gh_rows):,} mappings → {GITHUB_CSV}")


# ── Step 6: results.csv ───────────────────────────────────────────────────────

console.rule("[bold]Step 6[/bold] — results.csv")

t = time.perf_counter()
console.print("Computing PageRank …")
# Directed graph: edge A→B means "A depends on B", so B accumulates rank from everything that depends on it
G = nx.DiGraph()
for pkg, dep, _ in all_edges:
    if pkg != dep:  # skip self-loops
        G.add_edge(pkg, dep)
# Ensure isolated nodes (packages in universe with no edges) still appear in PageRank output
for pkg_lower in universe:
    G.add_node(display_name.get(pkg_lower, pkg_lower))

total_dl = sum(pkg_downloads.get(n, {}).get("avg_downloads", 0) for n in universe) or 1
# Download-weighted PageRank: personalization biases the random-surfer restart toward highly-downloaded
# packages, so a package that is both widely depended upon AND widely downloaded ranks higher.
personalization = {
    node: pkg_downloads.get(node.lower(), {}).get("avg_downloads", 0) / total_dl
    for node in G.nodes
}
pr = nx.pagerank(G, alpha=args.alpha, personalization=personalization)
console.print(f"  {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges  ({time.perf_counter()-t:.1f}s)")

result_rows = []
for pkg_lower in universe:
    name = display_name.get(pkg_lower, pkg_lower)
    data = pkg_downloads.get(pkg_lower, {"avg_downloads": 0, **{y: 0 for y in YEAR_COLS}})
    result_rows.append({
        "package":       name,
        "github_repo":   github_map.get(name, ""),
        "avg_downloads": data["avg_downloads"],
        **{y: data.get(y, 0) for y in YEAR_COLS},
        # "top" flags whether this package crossed the min-avg threshold on its own (vs. pulled in as a dep)
        "top":           pkg_lower in top_pkg_names_lower,
        "pagerank":      round(pr.get(name, 0.0), 8),
    })
result_rows.sort(key=lambda r: r["pagerank"], reverse=True)

write_csv(RESULTS_CSV, ["package", "github_repo", "avg_downloads"] + YEAR_COLS + ["top", "pagerank"], result_rows)

table = Table(title="Top 15 by PageRank", show_header=True, header_style="bold dim")
table.add_column("Package", style="bold")
table.add_column("GitHub", style="dim")
table.add_column("PageRank", justify="right")
table.add_column("Avg DL", justify="right")
table.add_column("Top", justify="center")
for r in result_rows[:15]:
    table.add_row(
        r["package"], r["github_repo"] or "—",
        f"{r['pagerank']:.6f}",
        f"{r['avg_downloads']:,}" if r["avg_downloads"] else "—",
        "[green]✓[/green]" if r["top"] else "",
    )
console.print(table)
console.print(f"[green]✓[/green] {len(result_rows):,} packages → {RESULTS_CSV}")


# ── Done ──────────────────────────────────────────────────────────────────────

console.print()
console.rule(f"[bold green]Done in {time.perf_counter()-t_total:.1f}s[/bold green]")
