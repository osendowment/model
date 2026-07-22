"""
process_data.py — Build Homebrew C/C++ analysis outputs.

Pipeline:
  1. top-formulas.csv    — C/C++ formulas with avg installs >= threshold
  2. dependency-tree.csv — BFS over runtime deps from top formulas
  3. github-repos.csv    — formula → owner/repo (from homepage / source_url)
  4. results.csv         — pagerank with install signals + value classes

Inputs:
  data/sources/homebrew/raw/formulas.csv        — name, tap, desc, license, homepage,
                                          source_url, language
  data/sources/homebrew/raw/dependencies.csv    — formula, dep_name, dep_type, fetched_at
  data/sources/homebrew/raw/downloads.csv       — formula, year, downloads

Run:
    uv run src/sources/homebrew/process_data.py
"""

import argparse
import csv
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime

import networkx as nx
from rich.console import Console
from rich.table import Table

from src.common.tables import merge_preserved_columns
from src.common.params import (
    TOP_THRESHOLD_PCT, PAGERANK_ALPHA, YEARS,
    assign_value_class, ecosystem_avg_downloads,
)

console = Console()

RAW_FORMULAS = "data/sources/homebrew/raw/formulas.csv"
RAW_DEPS = "data/sources/homebrew/raw/dependencies.csv"
RAW_DOWNLOADS = "data/sources/homebrew/raw/downloads.csv"
OSSFUZZ_PROJECTS = "data/sources/ossfuzz/projects.csv"
OUT_TOP = "data/sources/homebrew/top-packages.csv"
OUT_DEP_TREE = "data/sources/homebrew/dependency-tree.csv"
OUT_GITHUB = "data/sources/homebrew/github-repos.csv"
OUT_RESULTS = "data/sources/homebrew/results.csv"

WIDE_FIELDS = ["package", "avg_downloads"] + [str(y) for y in YEARS]
GITHUB_RE = re.compile(r"github\.com/([^/\s#?]+)/([^/\s#?.]+)", re.IGNORECASE)


# ── helpers ────────────────────────────────────────────────────────────────────

def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def load_formulas() -> dict[str, dict[str, str]]:
    with open(RAW_FORMULAS, newline="", encoding="utf-8") as f:
        return {row["name"]: row for row in csv.DictReader(f)}


def load_ossfuzz_slugs() -> set[str]:
    """github_repo slugs of OSS-Fuzz C/C++ projects."""
    if not os.path.exists(OSSFUZZ_PROJECTS):
        return set()
    with open(OSSFUZZ_PROJECTS, newline="", encoding="utf-8") as f:
        return {
            row["github_repo"].lower() for row in csv.DictReader(f)
            if row["github_repo"] and row.get("language") in ("c", "c++")
        }


def load_downloads() -> dict[str, dict[int, int]]:
    data: dict[str, dict[int, int]] = defaultdict(dict)
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yr = int(row["year"])
            if yr in YEARS:
                data[row["formula"]][yr] = int(row["downloads"] or 0)
    return data


def load_runtime_deps() -> list[tuple[str, str]]:
    """Runtime-only edges. Build deps are useful for language heuristics
    but shouldn't flow through pagerank (they're scaffolding, not dependencies)."""
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str]] = []
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["dep_type"] != "runtime":
                continue
            dep = row["dep_name"]
            if not dep or dep == "__none__":
                continue
            key = (row["formula"], dep)
            if key not in seen:
                seen.add(key)
                edges.append(key)
    return edges


def compute_avg(year_vals: dict[int, int]) -> int:
    populated = [year_vals.get(yr, 0) for yr in YEARS if year_vals.get(yr, 0) > 0]
    return int(sum(populated) / len(populated)) if populated else 0


def wide_row(formula: str, year_vals: dict[int, int]) -> dict:
    return {"package": formula, "avg_downloads": compute_avg(year_vals),
            **{str(yr): year_vals.get(yr, 0) for yr in YEARS}}


def extract_github_slug(*urls: str) -> str:
    for url in urls:
        if not url:
            continue
        m = GITHUB_RE.search(url)
        if m:
            owner, repo = m.group(1), m.group(2)
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{owner}/{repo}"
    return ""


def build_dep_tree(top: set[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    adj: dict[str, list[str]] = defaultdict(list)
    for pkg, dep in edges:
        adj[pkg].append(dep)
    visited = set(top)
    # Seed the BFS from a SORTED list: `top` is a set, and Python's per-process
    # string hash randomizes set iteration, so an unsorted seed emitted the
    # same edges in a different order every run — rewriting the whole
    # dependency-tree.csv for no change in content.
    queue = deque(sorted(top))
    result: list[tuple[str, str]] = []
    while queue:
        pkg = queue.popleft()
        for dep in adj.get(pkg, []):
            result.append((pkg, dep))
            if dep not in visited:
                visited.add(dep)
                queue.append(dep)
    return result


# ── pipeline steps ─────────────────────────────────────────────────────────────

def step_top(raw: dict[str, dict[int, int]], formulas: dict[str, dict]) -> set[str]:
    """Take the formulas covering the top TOP_THRESHOLD_PCT% of cumulative
    installs, using the full Homebrew ecosystem avg dl as denominator
    (matches the npm/pypi pattern). The C/C++ filter applies orthogonally via
    the is_cpp column in results.csv — not at this selection step."""
    console.rule("[bold cyan]Step 1 — top-formulas.csv")
    t0 = time.perf_counter()

    all_rows = sorted(
        [wide_row(n, raw[n]) for n in formulas
         if n in raw and compute_avg(raw[n]) > 0],
        key=lambda r: r["avg_downloads"], reverse=True,
    )
    eco_total = ecosystem_avg_downloads("homebrew")
    target = eco_total * TOP_THRESHOLD_PCT / 100
    cum = 0
    cutoff = len(all_rows)
    for i, r in enumerate(all_rows):
        cum += r["avg_downloads"]
        if cum >= target:
            cutoff = i + 1
            break
    rows = all_rows[:cutoff]
    atomic_write(OUT_TOP, rows, WIDE_FIELDS)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Threshold",      f"top {TOP_THRESHOLD_PCT}% of ecosystem installs")
    tbl.add_row("Ecosystem avg",  f"{eco_total:,}")
    tbl.add_row("Top formulas",   f"{len(rows):,}")
    tbl.add_row("Min avg DL",     f"{rows[-1]['avg_downloads']:,}" if rows else "0")
    tbl.add_row("Elapsed",        f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 10 by avg installs", header_style="bold green")
    preview.add_column("Formula"); preview.add_column("Avg", justify="right")
    for r in rows[:10]:
        preview.add_row(r["package"], f"{r['avg_downloads']:,}")
    console.print(preview)
    return {r["package"] for r in rows}


def step_dep_tree(top: set[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    console.rule("[bold cyan]Step 2 — dependency-tree.csv")
    t0 = time.perf_counter()
    tree_edges = build_dep_tree(top, edges)
    rows = [{"package": p, "dependency": d, "type": "runtime"} for p, d in tree_edges]
    atomic_write(OUT_DEP_TREE, rows, ["package", "dependency", "type"])

    nodes = {n for e in tree_edges for n in e} | top
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes",    f"{len(nodes):,}")
    tbl.add_row("Elapsed",         f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return tree_edges


def step_github(nodes: set[str], formulas: dict[str, dict]) -> dict[str, str]:
    console.rule("[bold cyan]Step 3 — github-repos.csv")
    t0 = time.perf_counter()
    pkg_to_repo: dict[str, str] = {}
    for n in nodes:
        f = formulas.get(n, {})
        slug = extract_github_slug(f.get("homepage", ""), f.get("source_url", ""))
        if slug:
            pkg_to_repo[n] = slug
    rows = sorted(
        [{"package": n, "github_repo": s} for n, s in pkg_to_repo.items()],
        key=lambda r: r["package"],
    )
    atomic_write(OUT_GITHUB, rows, ["package", "github_repo"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Tree nodes",        f"{len(nodes):,}")
    tbl.add_row("With github repo",  f"{len(pkg_to_repo):,}")
    tbl.add_row("Coverage",          f"{len(pkg_to_repo) / max(len(nodes), 1) * 100:.1f}%")
    tbl.add_row("Elapsed",           f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return pkg_to_repo


def step_results(
    tree_edges: list[tuple[str, str]],
    top: set[str],
    raw: dict[str, dict[int, int]],
    github_repos: dict[str, str],
    formulas: dict[str, dict],
    ossfuzz_slugs: set[str],
) -> None:
    console.rule("[bold cyan]Step 4 — results.csv (pagerank)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep in tree_edges:
        G.add_edge(pkg, dep)
    for n in top:
        G.add_node(n)

    all_nodes = set(G.nodes())
    total_dl = sum(compute_avg(raw.get(n, {})) for n in all_nodes)
    personalization = (
        {n: compute_avg(raw.get(n, {})) / total_dl for n in all_nodes} if total_dl > 0 else None
    )
    pr = nx.pagerank(G, alpha=PAGERANK_ALPHA, personalization=personalization)

    rows: list[dict] = []
    for n in all_nodes:
        yv = raw.get(n, {})
        language = (formulas.get(n) or {}).get("language", "")
        slug = github_repos.get(n, "")
        rows.append({
            "package":       n,
            "github_repo":   slug,
            "language":      language,
            "avg_downloads": compute_avg(yv),
            **{str(yr): yv.get(yr, 0) for yr in YEARS},
            "top":           str(n in top),
            "is_cpp":        str(language == "c/c++"),
            "is_oss_fuzz":   str(slug.lower() in ossfuzz_slugs) if slug else "False",
            "pagerank":      f"{pr.get(n, 0.0):.8f}",
        })
    # Name tie-break — see npm/process_data.py for why.
    rows.sort(key=lambda r: (-float(r["pagerank"]), r["package"]))

    total_pr = sum(float(r["pagerank"]) for r in rows)
    cum = 0.0
    for r in rows:
        cum += float(r["pagerank"])
        share = cum / total_pr if total_pr > 0 else 1.0
        r["value_class"] = assign_value_class(share)

    fields = (
        ["package", "github_repo", "language", "avg_downloads"]
        + [str(y) for y in YEARS]
        + ["top", "is_cpp", "is_oss_fuzz", "pagerank", "value_class"]
    )
    # Keep the enrichment later value-stage steps add in place (repo_id,
    # canonical_url, license) — a rebuild must not drop them.
    rows, _cols = merge_preserved_columns(OUT_RESULTS, list(fields), rows)
    atomic_write(OUT_RESULTS, rows, _cols)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Nodes",         f"{G.number_of_nodes():,}")
    tbl.add_row("Edges",         f"{G.number_of_edges():,}")
    tbl.add_row("C/C++ nodes",   f"{sum(1 for r in rows if r['is_cpp'] == 'True'):,}")
    tbl.add_row("With github",   f"{sum(1 for r in rows if r['github_repo']):,}")
    tbl.add_row("OSS-Fuzz matches", f"{sum(1 for r in rows if r['is_oss_fuzz'] == 'True'):,}")
    tbl.add_row("Elapsed",       f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 by PageRank", header_style="bold green")
    preview.add_column("Formula")
    preview.add_column("Lang", style="dim")
    preview.add_column("GitHub")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg installs", justify="right")
    preview.add_column("Top?", justify="center")
    for r in rows[:15]:
        preview.add_row(
            r["package"], r["language"], r["github_repo"] or "[dim]—[/dim]",
            r["pagerank"], f"{r['avg_downloads']:,}",
            "[green]yes[/green]" if r["top"] == "True" else "[dim]no[/dim]",
        )
    console.print(preview)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    argparse.ArgumentParser().parse_args()

    console.rule("[bold white]homebrew — process_data.py")
    console.print(f"  Started        : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Top threshold  : [cyan]top {TOP_THRESHOLD_PCT}% of ecosystem install mass[/cyan]")
    console.print()

    t_total = time.perf_counter()

    formulas = load_formulas()
    raw = load_downloads()
    edges = load_runtime_deps()

    ossfuzz_slugs = load_ossfuzz_slugs()

    top = step_top(raw, formulas)
    tree_edges = step_dep_tree(top, edges)
    nodes = {n for e in tree_edges for n in e} | top
    gh = step_github(nodes, formulas)
    step_results(tree_edges, top, raw, gh, formulas, ossfuzz_slugs)

    console.rule()
    console.print(f"  [bold green]Done[/bold green] — {time.perf_counter() - t_total:.2f}s")


if __name__ == "__main__":
    main()
