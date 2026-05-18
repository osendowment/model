"""
process_data.py — Build Debian C/C++ analysis outputs, aggregated at the
SOURCE-package level.

Why source-level: one Debian source package (e.g. `ffmpeg`, `glib2.0`, `openssl`)
produces many binary packages (`libavcodec61`, `libavformat61`, `libglib2.0-0t64`,
`libssl3t64`, …). Binaries get renamed across SONAME bumps and ABI transitions;
the *source* is the stable "project" unit and the right granularity for
funding/importance signals.

Aggregation rules:
  downloads (per year)  → MAX across binaries of the source
                          (SUM would double-count machines that have both
                           libfoo.so.5 and libfoo.so.6 installed side-by-side)
  deps                  → translate binary→binary edges to source→source,
                          drop self-loops and dedupe
  is_cpp                → any binary C/C++ → source C/C++
  github                → first non-empty github.com URL across binaries
                          (Homepage / Vcs-Browser)

Inputs:
  data/debian/raw/cpp-packages.csv       — package, tag, via
  data/debian/raw/downloads.csv          — package, year, downloads
  data/debian/raw/dependencies.csv       — package, dep_name, dep_version, fetched_at
  data/debian/raw/package-metadata.csv   — package, source, homepage, vcs_browser, section
  data/debian/raw/aliases.csv            — current, old  (t64 rename map)

Outputs (now per source):
  data/debian/top-packages.csv           — source, avg_downloads, per-year
  data/debian/dependency-tree.csv        — source, dependency, type
  data/debian/github-repos.csv           — source, github_repo
  data/debian/results.csv                — source, github_repo, per-year, top, is_cpp, pagerank, value_class

Run:
    uv run src/debian/process_data.py
    uv run src/debian/process_data.py --top-min 500
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

from src.pipeline.common.params import (
    TOP_THRESHOLD_PCT, PAGERANK_ALPHA, YEARS,
    assign_value_class, ecosystem_avg_downloads,
)

console = Console()

RAW_PACKAGES = "data/debian/raw/cpp-packages.csv"
RAW_DOWNLOADS = "data/debian/raw/downloads.csv"
RAW_DEPS = "data/debian/raw/dependencies.csv"
RAW_METADATA = "data/debian/raw/package-metadata.csv"
RAW_ALIASES = "data/debian/raw/aliases.csv"
OSSFUZZ_PROJECTS = "data/ossfuzz/projects.csv"
OUT_TOP = "data/debian/top-packages.csv"
OUT_DEP_TREE = "data/debian/dependency-tree.csv"
OUT_GITHUB = "data/debian/github-repos.csv"
OUT_RESULTS = "data/debian/results.csv"

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


def load_aliases() -> dict[str, str]:
    """{old_binary_name: current_binary_name} for t64 renames."""
    if not os.path.exists(RAW_ALIASES):
        return {}
    with open(RAW_ALIASES, newline="", encoding="utf-8") as f:
        return {row["old"]: row["current"] for row in csv.DictReader(f)}


def load_ossfuzz_slugs() -> set[str]:
    """GitHub slugs of OSS-Fuzz projects (C/C++/C only). Used as a critical-
    project whitelist — any match adds is_oss_fuzz=True to the row."""
    if not os.path.exists(OSSFUZZ_PROJECTS):
        return set()
    with open(OSSFUZZ_PROJECTS, newline="", encoding="utf-8") as f:
        return {
            row["github_repo"].lower() for row in csv.DictReader(f)
            if row["github_repo"] and row.get("language") in ("c", "c++")
        }


def load_metadata_rows() -> list[dict]:
    with open(RAW_METADATA, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_cpp_binaries() -> set[str]:
    with open(RAW_PACKAGES, newline="", encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f)}


def compute_avg(year_vals: dict[int, int]) -> int:
    populated = [year_vals.get(yr, 0) for yr in YEARS if year_vals.get(yr, 0) > 0]
    return int(sum(populated) / len(populated)) if populated else 0


def wide_row(source: str, year_vals: dict[int, int]) -> dict:
    return {"package": source, "avg_downloads": compute_avg(year_vals),
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


# ── binary → source aggregation ───────────────────────────────────────────────

def build_bin_to_source(meta: list[dict], aliases: dict[str, str]) -> dict[str, str]:
    """Return {binary_name: source_name}.

    For each metadata row, binary → its source. Plus, for each alias
    (old_name → current_name), old_name → source(current_name) so historical
    popcon rows using pre-t64 names map to the right source.
    """
    bin_to_src = {row["package"]: row["source"] for row in meta if row.get("source")}
    for old, current in aliases.items():
        if current in bin_to_src:
            bin_to_src.setdefault(old, bin_to_src[current])
    return bin_to_src


def load_downloads_by_source(bin_to_src: dict[str, str]) -> dict[str, dict[int, int]]:
    """Per-source yearly installs, MAX across binaries.

    Rationale: a single submitter machine typically has libfoo.so.5 AND
    libfoo.so.6 installed during a transition year — summing double-counts.
    MAX across binaries of the same source = ~"machines with this project
    installed in any form".
    """
    by_src: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    with open(RAW_DOWNLOADS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yr = int(row["year"])
            if yr not in YEARS:
                continue
            binary = row["package"]
            source = bin_to_src.get(binary, binary)  # fallback: binary name is the source
            count = int(row["downloads"] or 0)
            if count > by_src[source][yr]:
                by_src[source][yr] = count
    return by_src


def load_source_deps(bin_to_src: dict[str, str]) -> list[tuple[str, str]]:
    """Translate binary deps to source edges. Drops self-loops and dedupes."""
    edges: set[tuple[str, str]] = set()
    with open(RAW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dep = row["dep_name"]
            if not dep or dep == "__none__":
                continue
            src_from = bin_to_src.get(row["package"], row["package"])
            src_to   = bin_to_src.get(dep, dep)
            if src_from != src_to:
                edges.add((src_from, src_to))
    return sorted(edges)


def source_is_cpp(cpp_binaries: set[str], bin_to_src: dict[str, str]) -> set[str]:
    """A source is C/C++ if any of its binaries is."""
    return {bin_to_src.get(b, b) for b in cpp_binaries}


def source_github_map(meta: list[dict], bin_to_src: dict[str, str]) -> dict[str, str]:
    """First non-empty github slug per source (across its binaries)."""
    result: dict[str, str] = {}
    for row in meta:
        src = row.get("source") or row["package"]
        if src in result:
            continue
        slug = extract_github_slug(row.get("homepage", ""), row.get("vcs_browser", ""))
        if slug:
            result[src] = slug
    # Also apply bin_to_src transitively for binaries whose metadata name
    # differs (shouldn't usually happen, but defensive).
    return result


# ── BFS over source graph ──────────────────────────────────────────────────────

def build_dep_tree(top_sources: set[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    adj: dict[str, list[str]] = defaultdict(list)
    for pkg, dep in edges:
        adj[pkg].append(dep)
    visited = set(top_sources)
    queue = deque(top_sources)
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

def step_top(raw: dict[str, dict[int, int]], cpp_srcs: set[str]) -> set[str]:
    """Take the sources covering the top TOP_THRESHOLD_PCT% of cumulative
    installs, using the full Debian ecosystem avg dl as denominator (matches
    the npm/pypi pattern). The C/C++ filter applies orthogonally via the
    is_cpp column in results.csv — not at this selection step."""
    console.rule("[bold cyan]Step 1 — top-packages.csv (per source)")
    t0 = time.perf_counter()
    all_rows = sorted(
        [wide_row(src, yv) for src, yv in raw.items() if compute_avg(yv) > 0],
        key=lambda r: r["avg_downloads"], reverse=True,
    )
    eco_total = ecosystem_avg_downloads("debian")
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
    tbl.add_row("Threshold",       f"top {TOP_THRESHOLD_PCT}% of ecosystem installs")
    tbl.add_row("Ecosystem avg",   f"{eco_total:,}")
    tbl.add_row("Top sources",     f"{len(rows):,}")
    tbl.add_row("Min avg DL",      f"{rows[-1]['avg_downloads']:,}" if rows else "0")
    tbl.add_row("Elapsed",         f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 10 sources by avg installs", header_style="bold green")
    preview.add_column("Source"); preview.add_column("Avg", justify="right")
    for r in rows[:10]:
        preview.add_row(r["package"], f"{r['avg_downloads']:,}")
    console.print(preview)
    return {r["package"] for r in rows}


def step_dep_tree(top_sources: set[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    console.rule("[bold cyan]Step 2 — dependency-tree.csv (per source)")
    t0 = time.perf_counter()
    tree_edges = build_dep_tree(top_sources, edges)
    rows = [{"package": p, "dependency": d, "type": "declared"} for p, d in tree_edges]
    atomic_write(OUT_DEP_TREE, rows, ["package", "dependency", "type"])

    nodes = {n for e in tree_edges for n in e} | top_sources
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes",    f"{len(nodes):,}")
    tbl.add_row("Elapsed",         f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return tree_edges


def step_github(nodes: set[str], src_gh: dict[str, str]) -> dict[str, str]:
    console.rule("[bold cyan]Step 3 — github-repos.csv (per source)")
    t0 = time.perf_counter()
    pkg_to_repo = {s: src_gh[s] for s in nodes if s in src_gh}
    rows = sorted(
        [{"package": s, "github_repo": r} for s, r in pkg_to_repo.items()],
        key=lambda r: r["package"],
    )
    atomic_write(OUT_GITHUB, rows, ["package", "github_repo"])

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Tree nodes",            f"{len(nodes):,}")
    tbl.add_row("Sources with repo",     f"{len(pkg_to_repo):,}")
    tbl.add_row("Coverage",              f"{len(pkg_to_repo) / max(len(nodes), 1) * 100:.1f}%")
    tbl.add_row("Elapsed",               f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return pkg_to_repo


def step_results(
    tree_edges: list[tuple[str, str]],
    top_sources: set[str],
    raw: dict[str, dict[int, int]],
    github_repos: dict[str, str],
    cpp_srcs: set[str],
    ossfuzz_slugs: set[str],
) -> None:
    console.rule("[bold cyan]Step 4 — results.csv (pagerank per source)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep in tree_edges:
        G.add_edge(pkg, dep)
    for src in top_sources:
        G.add_node(src)

    all_nodes = set(G.nodes())
    total_dl = sum(compute_avg(raw.get(n, {})) for n in all_nodes)
    personalization = (
        {n: compute_avg(raw.get(n, {})) / total_dl for n in all_nodes} if total_dl > 0 else None
    )
    pr = nx.pagerank(G, alpha=PAGERANK_ALPHA, personalization=personalization)

    rows: list[dict] = []
    for src in all_nodes:
        yv = raw.get(src, {})
        slug = github_repos.get(src, "")
        rows.append({
            "package":       src,
            "github_repo":   slug,
            "avg_downloads": compute_avg(yv),
            **{str(yr): yv.get(yr, 0) for yr in YEARS},
            "top":           str(src in top_sources),
            "is_cpp":        str(src in cpp_srcs),
            "is_oss_fuzz":   str(slug.lower() in ossfuzz_slugs) if slug else "False",
            "pagerank":      f"{pr.get(src, 0.0):.8f}",
        })
    rows.sort(key=lambda r: float(r["pagerank"]), reverse=True)

    total_pr = sum(float(r["pagerank"]) for r in rows)
    cum = 0.0
    for r in rows:
        cum += float(r["pagerank"])
        share = cum / total_pr if total_pr > 0 else 1.0
        r["value_class"] = assign_value_class(share)

    fields = (
        ["package", "github_repo", "avg_downloads"]
        + [str(y) for y in YEARS]
        + ["top", "is_cpp", "is_oss_fuzz", "pagerank", "value_class"]
    )
    atomic_write(OUT_RESULTS, rows, fields)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Source nodes",     f"{G.number_of_nodes():,}")
    tbl.add_row("Source edges",     f"{G.number_of_edges():,}")
    tbl.add_row("C/C++ sources",    f"{sum(1 for r in rows if r['is_cpp'] == 'True'):,}")
    tbl.add_row("OSS-Fuzz matches", f"{sum(1 for r in rows if r['is_oss_fuzz'] == 'True'):,}")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 sources by PageRank", header_style="bold green")
    preview.add_column("Source")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg installs", justify="right")
    preview.add_column("C/C++",  justify="center")
    preview.add_column("Top?",   justify="center")
    for r in rows[:15]:
        preview.add_row(
            r["package"], r["pagerank"], f"{r['avg_downloads']:,}",
            "[green]yes[/green]" if r["is_cpp"] == "True" else "[dim]no[/dim]",
            "[green]yes[/green]" if r["top"]    == "True" else "[dim]no[/dim]",
        )
    console.print(preview)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    argparse.ArgumentParser().parse_args()

    console.rule("[bold white]debian — process_data.py (source-level)")
    console.print(f"  Started        : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Top threshold  : [cyan]top {TOP_THRESHOLD_PCT}% of C/C++ install mass[/cyan]")
    console.print()

    t_total = time.perf_counter()

    aliases = load_aliases()
    meta = load_metadata_rows()
    cpp_binaries = load_cpp_binaries()

    bin_to_src = build_bin_to_source(meta, aliases)
    cpp_srcs = source_is_cpp(cpp_binaries, bin_to_src)
    src_downloads = load_downloads_by_source(bin_to_src)
    src_edges = load_source_deps(bin_to_src)
    src_gh = source_github_map(meta, bin_to_src)

    console.print(
        f"  [dim]binaries={len(bin_to_src):,}  "
        f"sources={len({v for v in bin_to_src.values()}):,}  "
        f"C/C++ sources={len(cpp_srcs):,}[/dim]\n"
    )

    ossfuzz_slugs = load_ossfuzz_slugs()

    top = step_top(src_downloads, cpp_srcs)
    tree_edges = step_dep_tree(top, src_edges)
    nodes = {n for e in tree_edges for n in e} | top
    gh = step_github(nodes, src_gh)
    step_results(tree_edges, top, src_downloads, gh, cpp_srcs, ossfuzz_slugs)

    console.rule()
    console.print(f"  [bold green]Done[/bold green] — {time.perf_counter() - t_total:.2f}s")


if __name__ == "__main__":
    main()
