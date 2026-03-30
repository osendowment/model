"""
Build dependency graphs for PyPI, npm, and crates, then compute PageRank.

For each ecosystem:
  - Nodes  = all packages (top list + anything they depend on)
  - Edges  = A → B means "A depends on B" (B gets rank from dependents)
  - Node weight = avg_downloads from top-package-downloads.csv (0 if not in top list)

Two PageRank variants are computed:
  - pagerank       : standard PageRank (structural importance = how critical a node is)
  - pagerank_dl    : download-personalized PageRank (biases random walk toward
                     high-download packages, combining popularity with centrality)

Outputs (one per ecosystem):
  data/{ecosystem}/pagerank.csv
  Columns: package, rank, pagerank, pagerank_dl, avg_downloads, in_top_list

Run:
    uv run src/compute_pagerank.py
    uv run src/compute_pagerank.py --alpha 0.90
"""

import argparse
import csv
import time

import networkx as nx
from rich.console import Console
from rich.table import Table

ALPHA   = 0.85   # damping factor
YEARS   = [str(y) for y in range(2021, 2026)]

ECOSYSTEMS = {
    "pypi": {
        "downloads": "data/pypi/top-package-downloads.csv",
        "deps":      "data/pypi/package-dependencies.csv",
        "dep_col":   "dependency",
        "output":    "data/pypi/pagerank.csv",
    },
    "npm": {
        "downloads": "data/npm/top-package-downloads.csv",
        "deps":      "data/npm/package-dependencies.csv",
        "dep_col":   "dep_name",
        "output":    "data/npm/pagerank.csv",
    },
    "crates": {
        "downloads": "data/crates/top-package-downloads.csv",
        "deps":      "data/crates/package-dependencies.csv",
        "dep_col":   "dependency",
        "output":    "data/crates/pagerank.csv",
    },
}

console = Console()


def load_downloads(path: str) -> dict[str, int]:
    result = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            result[row["package"]] = int(row["avg_downloads"])
    return result


def load_edges(path: str, dep_col: str) -> list[tuple[str, str]]:
    edges = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pkg = row["package"].strip()
            dep = row[dep_col].strip()
            if pkg and dep and pkg != dep:
                edges.append((pkg, dep))
    return edges


def build_and_rank(name: str, cfg: dict, alpha: float) -> list[dict]:
    t = time.perf_counter()
    console.print(f"[bold]  {name}[/bold]")

    downloads = load_downloads(cfg["downloads"])
    edges     = load_edges(cfg["deps"], cfg["dep_col"])

    console.print(f"    {len(downloads):,} top packages  |  {len(edges):,} dependency edges")

    G = nx.DiGraph()
    G.add_nodes_from(downloads.keys())
    G.add_edges_from(edges)

    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()
    console.print(f"    Graph: {n_nodes:,} nodes  {n_edges:,} edges")

    # Standard PageRank
    pr = nx.pagerank(G, alpha=alpha)

    # Download-personalized PageRank
    total_dl = sum(downloads.values()) or 1
    personalization = {pkg: dl / total_dl for pkg, dl in downloads.items()}
    # Nodes not in top list get zero personalization weight
    for node in G.nodes:
        if node not in personalization:
            personalization[node] = 0.0
    pr_dl = nx.pagerank(G, alpha=alpha, personalization=personalization)

    # Build output rows (all nodes in graph)
    rows = []
    for node in G.nodes:
        rows.append({
            "package":      node,
            "pagerank":     pr[node],
            "pagerank_dl":  pr_dl[node],
            "avg_downloads": downloads.get(node, 0),
            "in_top_list":  node in downloads,
        })

    rows.sort(key=lambda r: r["pagerank"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    console.print(f"    Done  ({time.perf_counter()-t:.1f}s)")
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    fields = ["rank", "package", "pagerank", "pagerank_dl", "avg_downloads", "in_top_list"]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    import os; os.replace(tmp, path)


def print_top(name: str, rows: list[dict], n: int = 15) -> None:
    table = Table(title=f"{name} — top {n} by PageRank", show_header=True)
    table.add_column("Rank", justify="right")
    table.add_column("Package", style="bold")
    table.add_column("PageRank", justify="right")
    table.add_column("PR (dl)", justify="right")
    table.add_column("Avg DL", justify="right")

    for r in rows[:n]:
        table.add_row(
            str(r["rank"]),
            r["package"],
            f"{r['pagerank']:.6f}",
            f"{r['pagerank_dl']:.6f}",
            f"{r['avg_downloads']:,}" if r["avg_downloads"] else "—",
        )
    console.print(table)


# ── Main ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--alpha", type=float, default=ALPHA,
                    help=f"PageRank damping factor (default: {ALPHA})")
parser.add_argument("--ecosystem", choices=list(ECOSYSTEMS), default=None,
                    help="Run one ecosystem only (default: all)")
args = parser.parse_args()

console.rule("[bold]PageRank — dependency graphs")
console.print(f"Alpha (damping): {args.alpha}")
console.print()

t0 = time.perf_counter()
targets = {args.ecosystem: ECOSYSTEMS[args.ecosystem]} if args.ecosystem else ECOSYSTEMS

for name, cfg in targets.items():
    rows = build_and_rank(name, cfg, args.alpha)
    write_csv(rows, cfg["output"])
    console.print()
    print_top(name, rows)
    console.print(f"  [bold green]Written {len(rows):,} rows → {cfg['output']}[/bold green]\n")

console.print(f"[bold green]Done in {time.perf_counter()-t0:.1f}s[/bold green]")
