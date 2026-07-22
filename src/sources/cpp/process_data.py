"""
process_data.py — Unified C/C++ pipeline across Debian + Homebrew, joined at
the Repology "project" level.

Repology's canonical project name collapses the structural noise in each
ecosystem:
  - Debian source-version fragmentation (boost1.74/1.81/1.83 → boost)
  - Homebrew per-version formulas (openssl@3/@3.0/@3.5 → openssl)
  - Naming drift across ecosystems (libpng1.6 ↔ libpng)

Outputs mirror the pypi/ and npm/ layouts so downstream analysis works the
same way for every ecosystem.

Inputs:
  data/sources/repology/packages.csv              project ↔ (repo, srcname, binname, visiblename)
  data/sources/debian/raw/dependencies.csv        binary, dep_name   (raw edges — results.csv
                                                              drops the unreachable ones)
  data/sources/debian/raw/package-metadata.csv    binary → source    (for t64 aliases)
  data/sources/debian/raw/aliases.csv             current ↔ old      (t64 rename map)
  data/sources/debian/results.csv                 per-source: avg, yearly, is_cpp, github, is_oss_fuzz
  data/sources/homebrew/raw/dependencies.csv      formula, dep_name, dep_type
  data/sources/homebrew/results.csv               per-formula: avg, yearly, is_cpp, github, is_oss_fuzz

Outputs:
  data/sources/cpp/raw/packages.csv               per-project join + aggregated signals
  data/sources/cpp/top-packages.csv               package, debian_avg_downloads, debian_share,
                                          homebrew_avg_downloads, homebrew_share
  data/sources/cpp/dependency-tree.csv            package, dependency, type
  data/sources/cpp/github-repos.csv               package, github_repo
  data/sources/cpp/results.csv                    package, github_repo, debian_avg_downloads,
                                          homebrew_avg_downloads, downloads_score,
                                          pagerank, value_class

Aggregation rules (per Repology project):
  downloads       — within ecosystem: MAX across constituent names (boost1.74 vs
                    1.81 vs 1.83 → same machines, don't double-count)
  downloads_score — A × debian_avg + B × homebrew_avg, weights from params.json
                    (default 1.39:1 balances ecosystem totals)
  deps            — union of project→project edges from both ecosystems, deduped,
                    self-loops dropped
  github          — first non-empty slug from Debian, else Homebrew

Run:
    uv run -m src.sources.cpp.process_data                    # default TOP_THRESHOLD_PCT
    uv run -m src.sources.cpp.process_data --top-share 50     # override per-ecosystem cum-dl cap
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
    TOP_THRESHOLD_PCT, PAGERANK_ALPHA,
    DOWNLOADS_SCORE_DEBIAN_WEIGHT, DOWNLOADS_SCORE_HOMEBREW_WEIGHT,
    assign_value_class,
)

console = Console()

# ── paths / config ────────────────────────────────────────────────────────────

REPOLOGY_CSV       = "data/sources/repology/packages.csv"

# Pre-aggregated per-ecosystem results (source for signals + percentile pools)
DEBIAN_RESULTS     = "data/sources/debian/results.csv"
HOMEBREW_RESULTS   = "data/sources/homebrew/results.csv"

# Raw files — only needed for raw-level dep edges (the per-ecosystem pipelines
# don't export unfiltered source/formula edges, only BFS-trimmed trees)
DEBIAN_DEPS        = "data/sources/debian/raw/dependencies.csv"
DEBIAN_METADATA    = "data/sources/debian/raw/package-metadata.csv"
DEBIAN_ALIASES     = "data/sources/debian/raw/aliases.csv"
HOMEBREW_DEPS      = "data/sources/homebrew/raw/dependencies.csv"

OSSFUZZ_PROJECTS   = "data/sources/ossfuzz/projects.csv"

OUT_RAW_PACKAGES   = "data/sources/cpp/raw/packages.csv"
OUT_TOP            = "data/sources/cpp/top-packages.csv"
OUT_DEP_TREE       = "data/sources/cpp/dependency-tree.csv"
OUT_GITHUB         = "data/sources/cpp/github-repos.csv"
OUT_RESULTS        = "data/sources/cpp/results.csv"

# A package is "top" if it is among those responsible for the top X% of
# cumulative download mass in Debian or Homebrew (not a count percentile).
# Debian's cum-dl is flatter than Homebrew's (Gini 0.69 vs 0.79), so using
# the "either ecosystem" union gives a Pareto-style seed set skewed toward
# actually-load-bearing libraries. The threshold (TOP_THRESHOLD_PCT) lands
# at ~20% of Debian C/C++ sources and ~15% of Homebrew C/C++ formulas —
# the classic Pareto cutoff.

GITHUB_RE = re.compile(r"github\.com/([^/\s#?]+)/([^/\s#?.]+)", re.IGNORECASE)

TOP_FIELDS = ["package",
              "debian_avg_downloads", "debian_share",
              "homebrew_avg_downloads", "homebrew_share"]


# ── io helpers ────────────────────────────────────────────────────────────────

def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


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


# ── Repology name maps ────────────────────────────────────────────────────────

def load_repology() -> tuple[dict[str, str], dict[str, str]]:
    """Return (debian_name → project, homebrew_name → project).

    Indexes srcname, binname, and visiblename — our upstream data (Debian
    binary deps, Homebrew formula names) might carry any of the three.
    """
    deb: dict[str, str] = {}
    hb: dict[str, str] = {}
    with open(REPOLOGY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            project = row["project"]
            target = deb if row["repo"] == "debian_13" else hb if row["repo"] == "homebrew" else None
            if target is None:
                continue
            for k in ("srcname", "binname", "visiblename"):
                v = (row.get(k) or "").strip()
                if v:
                    target.setdefault(v, project)
    return deb, hb


# ── ecosystem signal loaders ──────────────────────────────────────────────────

def _bool(v) -> bool:
    return str(v).lower() == "true"


def load_debian_aliases() -> dict[str, str]:
    if not os.path.exists(DEBIAN_ALIASES):
        return {}
    with open(DEBIAN_ALIASES, newline="", encoding="utf-8") as f:
        return {row["old"]: row["current"] for row in csv.DictReader(f)}


_T64_STRIP_RE = re.compile(r"^(lib[a-z0-9.+-]*?\d+)t64(\b.*)$")


def _synthesize_t64_aliases(bin_to_src: dict[str, str]) -> None:
    """For every `libXYZt64[-suffix]` binary in metadata, register the pre-t64
    twin `libXYZ[-suffix]` as an alias pointing at the same source. In-place.

    Debian 13's time_t transition renamed thousands of libs (`libssl3` →
    `libssl3t64`, `libcurl3-gnutls` → `libcurl3t64-gnutls`, …). Our
    aliases.csv catches some but not all — this fallback infers the rest
    from the canonical names we already have. Without it, dep edges that
    still reference old names (pinned in reverse-dependencies we scraped)
    become orphan `debian:libcurl3-gnutls`-style nodes whose PR mass
    doesn't flow into the real project.
    """
    added = 0
    for name, source in list(bin_to_src.items()):
        m = _T64_STRIP_RE.match(name)
        if not m:
            continue
        pre = m.group(1) + m.group(2)
        if pre not in bin_to_src:
            bin_to_src[pre] = source
            added += 1
    console.print(f"  [dim]synthesized {added:,} t64 aliases[/dim]")


def build_bin_to_source(aliases: dict[str, str]) -> dict[str, str]:
    """{binary_name: source_name} with alias backfill.

    Sources:
      1. Debian package-metadata.csv (authoritative, post-t64 names)
      2. aliases.csv explicit old→current map
      3. synthesized pre-t64 twins for any `libXYZt64...` in (1)
    """
    bin_to_src: dict[str, str] = {}
    with open(DEBIAN_METADATA, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("source"):
                bin_to_src[row["package"]] = row["source"]
    for old, current in aliases.items():
        if current in bin_to_src:
            bin_to_src.setdefault(old, bin_to_src[current])
    _synthesize_t64_aliases(bin_to_src)
    return bin_to_src


def _load_signals(path: str) -> dict[str, dict]:
    """Shared reader for the per-ecosystem results files. Both Debian and
    Homebrew results.csv use `package` as the identifier column (was
    historically `source` and `formula` respectively — unified for
    consistency with npm/pypi/crates)."""
    signals: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            signals[r["package"]] = {
                "avg":         _i(r["avg_downloads"]),
                "is_cpp":      _bool(r["is_cpp"]),
                "github":      r["github_repo"] or "",
                "is_oss_fuzz": _bool(r["is_oss_fuzz"]),
            }
    return signals


def load_debian_signals() -> dict[str, dict]:
    return _load_signals(DEBIAN_RESULTS)


def load_homebrew_signals() -> dict[str, dict]:
    return _load_signals(HOMEBREW_RESULTS)


# ── edges (from raw deps) ─────────────────────────────────────────────────────

def _debian_project(name: str, bin_to_src: dict[str, str], deb_map: dict[str, str]) -> str:
    """Binary → source → Repology project (with `debian:<source>` fallback)."""
    source = bin_to_src.get(name, name)
    return deb_map.get(source) or deb_map.get(name) or f"debian:{source}"


def _homebrew_project(name: str, hb_map: dict[str, str]) -> str:
    return hb_map.get(name) or f"homebrew:{name}"


def build_debian_edges(bin_to_src: dict[str, str], deb_map: dict[str, str]) -> list[tuple[str, str]]:
    """Binary→binary raw deps → project→project edges (deduped, no self-loops)."""
    edges: set[tuple[str, str]] = set()
    with open(DEBIAN_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dep = row["dep_name"]
            if not dep or dep == "__none__":
                continue
            p_from = _debian_project(row["package"], bin_to_src, deb_map)
            p_to   = _debian_project(dep,            bin_to_src, deb_map)
            if p_from != p_to:
                edges.add((p_from, p_to))
    return sorted(edges)


def build_homebrew_edges(hb_map: dict[str, str]) -> list[tuple[str, str]]:
    """Runtime-only formula→formula deps → project→project (build deps are
    scaffolding, not real runtime dependencies)."""
    edges: set[tuple[str, str]] = set()
    with open(HOMEBREW_DEPS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["dep_type"] != "runtime":
                continue
            dep = row["dep_name"]
            if not dep or dep == "__none__":
                continue
            p_from = _homebrew_project(row["formula"], hb_map)
            p_to   = _homebrew_project(dep,            hb_map)
            if p_from != p_to:
                edges.add((p_from, p_to))
    return sorted(edges)


# ── combine ──────────────────────────────────────────────────────────────────

def _first_github(names: list[str], signals: dict[str, dict]) -> str:
    for n in sorted(names):
        slug = signals[n]["github"]
        if slug:
            return slug
    return ""


def combine(
    deb_signals: dict[str, dict],
    hb_signals: dict[str, dict],
    deb_map: dict[str, str],
    hb_map: dict[str, str],
    deb_edges: list[tuple[str, str]],
    hb_edges: list[tuple[str, str]],
    ossfuzz_slugs: set[str],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Roll per-source (Debian) + per-formula (Homebrew) signals up to Repology
    projects. Within an ecosystem we take MAX across constituent names
    (boost1.74/1.81/1.83 → MAX avg — same machines, don't double-count).
    Each row carries everything downstream steps need: the two per-ecosystem
    avgs, the weighted `downloads_score`, provenance lists, and the two
    internal cumulative-share fields (`debian_dl_p` / `homebrew_dl_p`) that
    drive top-package selection.
    """
    project_deb_names: dict[str, set[str]] = defaultdict(set)
    project_hb_names:  dict[str, set[str]] = defaultdict(set)
    for source in deb_signals:
        project_deb_names[deb_map.get(source) or f"debian:{source}"].add(source)
    for formula in hb_signals:
        project_hb_names[hb_map.get(formula) or f"homebrew:{formula}"].add(formula)

    # Per-ecosystem C/C++ pool, rolled up to project level.
    # Denominator for cumulative_shares is the project-level MAX-rolled sum,
    # so sum(shares) = 100% across all projects and ~95% within the top-95%
    # cum-dl slice used by step_top.
    def _any_cpp(names: set[str], signals: dict[str, dict]) -> bool:
        return any(signals[n]["is_cpp"] for n in names)

    deb_project_avg = {
        p: max(deb_signals[n]["avg"] for n in names)
        for p, names in project_deb_names.items()
        if _any_cpp(names, deb_signals) and max(deb_signals[n]["avg"] for n in names) > 0
    }
    hb_project_avg = {
        p: max(hb_signals[n]["avg"] for n in names)
        for p, names in project_hb_names.items()
        if _any_cpp(names, hb_signals) and max(hb_signals[n]["avg"] for n in names) > 0
    }
    deb_project_pct = cumulative_shares(deb_project_avg)
    hb_project_pct  = cumulative_shares(hb_project_avg)
    deb_total = sum(deb_project_avg.values()) or 1
    hb_total  = sum(hb_project_avg.values())  or 1

    all_projects = set(project_deb_names) | set(project_hb_names)
    rows: list[dict] = []
    for project in all_projects:
        d_names = project_deb_names.get(project, set())
        h_names = project_hb_names.get(project, set())

        d_avg = max((deb_signals[n]["avg"] for n in d_names), default=0)
        h_avg = max((hb_signals[n]["avg"] for n in h_names), default=0)
        is_cpp = _any_cpp(d_names, deb_signals) or _any_cpp(h_names, hb_signals)
        # Skip administrative split-binaries with no Repology mapping and no
        # cpp signal -- e.g. `debian:libc-l10n`, `debian:lib32c-dev`. They
        # come in via raw deps, get a `debian:<name>` fallback in
        # _debian_project, and would otherwise sit in cpp/results.csv with
        # zero downloads but non-zero PR (inherited from being depended-on).
        if not is_cpp:
            continue
        github = _first_github(list(d_names), deb_signals) \
              or _first_github(list(h_names), hb_signals)
        is_oss_fuzz = (
            (github.lower() in ossfuzz_slugs if github else False)
            or any(deb_signals[n]["is_oss_fuzz"] for n in d_names)
            or any(hb_signals[n]["is_oss_fuzz"] for n in h_names)
        )

        rows.append({
            "project":                project,
            "github_repo":            github,
            "debian_sources":         "|".join(sorted(d_names)),
            "homebrew_formulas":      "|".join(sorted(h_names)),
            "debian_avg_downloads":   d_avg,
            "homebrew_avg_downloads": h_avg,
            # Weighted combined signal — drives both sorting and PageRank
            # personalization. Weights configurable via src/settings.json.
            "downloads_score":        int(
                DOWNLOADS_SCORE_DEBIAN_WEIGHT   * d_avg +
                DOWNLOADS_SCORE_HOMEBREW_WEIGHT * h_avg
            ),
            "is_cpp":                 str(is_cpp),
            "is_oss_fuzz":            str(is_oss_fuzz),
            # Internal fields (kept on rows but excluded from raw/packages.csv
            # via extrasaction="ignore" — they drive the top-package filter and
            # the share columns in top-packages.csv).
            "debian_dl_p":            f"{deb_project_pct[project]:.2f}"
                                      if project in deb_project_pct else "",
            "homebrew_dl_p":          f"{hb_project_pct[project]:.2f}"
                                      if project in hb_project_pct else "",
            "debian_share":           f"{100 * d_avg / deb_total:.4f}" if d_avg > 0 else "",
            "homebrew_share":         f"{100 * h_avg / hb_total:.4f}" if h_avg > 0 else "",
        })

    edges = sorted(set(deb_edges) | set(hb_edges))
    return rows, edges


def cumulative_shares(name_to_avg: dict[str, int]) -> dict[str, float]:
    """Return {name: cumulative download share from the top, as percent}.

    Sort by avg descending; each package's _p is the cumulative share of
    total downloads captured by it and everything above it. So:
      0 < _p <= 100
      _p = 5   → this package sits inside the top 5% of install mass
      _p = 50  → this package is among the set that accounts for the top
                 half of all C/C++ installs in the ecosystem

    Ties share the cum share at the *end* of the tie group, so a tied cluster
    qualifies as "in the top X%" only if the whole group fits under the cap.
    """
    with_dl = [(n, v) for n, v in name_to_avg.items() if v > 0]
    if not with_dl:
        return {}
    with_dl.sort(key=lambda kv: kv[1], reverse=True)
    total = sum(v for _, v in with_dl)
    out: dict[str, float] = {}
    i = 0
    running = 0
    n = len(with_dl)
    while i < n:
        j = i
        while j < n and with_dl[j][1] == with_dl[i][1]:
            running += with_dl[j][1]
            j += 1
        share = 100.0 * running / total if total > 0 else 100.0
        for k in range(i, j):
            out[with_dl[k][0]] = share
        i = j
    return out


# ── pipeline steps ────────────────────────────────────────────────────────────

RAW_FIELDS = [
    "project", "github_repo",
    "debian_sources", "homebrew_formulas",
    "debian_avg_downloads", "homebrew_avg_downloads", "downloads_score",
    "is_cpp", "is_oss_fuzz",
]


def step_packages(rows: list[dict]) -> None:
    """Bridge table: every Repology project with its unified per-ecosystem
    signals, pre-ranking. Used for debugging and for any downstream consumer
    that wants the full join, not just the top slice or the BFS-reachable set."""
    console.rule("[bold cyan]Step 0 — raw/packages.csv")
    t0 = time.perf_counter()
    # Name tie-break — see npm/process_data.py for why.
    rows.sort(key=lambda r: (-r["downloads_score"], r["project"]))
    atomic_write(OUT_RAW_PACKAGES, rows, RAW_FIELDS)

    n_both = sum(1 for r in rows if r["debian_sources"] and r["homebrew_formulas"])
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Total projects", f"{len(rows):,}")
    tbl.add_row("In both",        f"{n_both:,}")
    tbl.add_row("Elapsed",        f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)


def _qual(p: str, share_cap: float) -> bool:
    """True iff `p` is a cum-share value inside the top `share_cap`% slice."""
    return bool(p) and float(p) <= share_cap


def _min_or_inf(s: str) -> float:
    return float(s) if s else float("inf")


def step_top(rows: list[dict], top_share: float) -> set[str]:
    """A project qualifies if it lives inside the top-X% cumulative download
    mass of *either* ecosystem. Writes top-packages.csv in the 5-col schema
    (package + per-ecosystem avg + per-ecosystem share-of-total)."""
    console.rule("[bold cyan]Step 1 — top-packages.csv")
    t0 = time.perf_counter()
    candidates = [r for r in rows if r["is_cpp"] == "True"]
    qualifying = [
        r for r in candidates
        if _qual(r["debian_dl_p"], top_share) or _qual(r["homebrew_dl_p"], top_share)
    ]
    qualifying.sort(key=lambda r: min(_min_or_inf(r["debian_dl_p"]),
                                      _min_or_inf(r["homebrew_dl_p"])))
    top_rows = [
        {"package":                r["project"],
         "debian_avg_downloads":   r["debian_avg_downloads"],
         "debian_share":           r["debian_share"],
         "homebrew_avg_downloads": r["homebrew_avg_downloads"],
         "homebrew_share":         r["homebrew_share"]}
        for r in qualifying
    ]
    atomic_write(OUT_TOP, top_rows, TOP_FIELDS)

    n_both     = sum(1 for r in qualifying if _qual(r["debian_dl_p"], top_share)
                                          and _qual(r["homebrew_dl_p"], top_share))
    n_deb_only = sum(1 for r in qualifying if _qual(r["debian_dl_p"], top_share)
                                          and not _qual(r["homebrew_dl_p"], top_share))
    n_hb_only  = sum(1 for r in qualifying if _qual(r["homebrew_dl_p"], top_share)
                                          and not _qual(r["debian_dl_p"], top_share))

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Candidates",       f"{len(candidates):,} (C/C++ only)")
    tbl.add_row("Share cap",        f"top {top_share}% of cum dl")
    tbl.add_row("Top packages",     f"{len(top_rows):,}")
    tbl.add_row("  in both",        f"{n_both:,}")
    tbl.add_row("  Debian only",    f"{n_deb_only:,}")
    tbl.add_row("  Homebrew only",  f"{n_hb_only:,}")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 (by best cum-dl share across ecosystems)",
                    header_style="bold green")
    preview.add_column("Package")
    preview.add_column("Debian avg", justify="right")
    preview.add_column("Debian %",   justify="right")
    preview.add_column("Brew avg",   justify="right")
    preview.add_column("Brew %",     justify="right")
    for r in qualifying[:15]:
        preview.add_row(
            r["project"][:30],
            f"{r['debian_avg_downloads']:,}"   if r["debian_avg_downloads"]   else "[dim]—[/dim]",
            r["debian_dl_p"]   or "[dim]—[/dim]",
            f"{r['homebrew_avg_downloads']:,}" if r["homebrew_avg_downloads"] else "[dim]—[/dim]",
            r["homebrew_dl_p"] or "[dim]—[/dim]",
        )
    console.print(preview)
    return {r["package"] for r in top_rows}


def step_dep_tree(top: set[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    console.rule("[bold cyan]Step 2 — dependency-tree.csv")
    t0 = time.perf_counter()
    adj: dict[str, list[str]] = defaultdict(list)
    for pkg, dep in edges:
        adj[pkg].append(dep)
    visited = set(top)
    # Seed the BFS from a SORTED list: `top` is a set, and Python's per-process
    # string hash randomizes set iteration, so an unsorted seed emitted the
    # same edges in a different order every run — rewriting the whole
    # dependency-tree.csv for no change in content.
    queue = deque(sorted(top))
    tree_edges: list[tuple[str, str]] = []
    while queue:
        pkg = queue.popleft()
        for dep in adj.get(pkg, []):
            tree_edges.append((pkg, dep))
            if dep not in visited:
                visited.add(dep)
                queue.append(dep)
    atomic_write(
        OUT_DEP_TREE,
        [{"package": p, "dependency": d, "type": "declared"} for p, d in tree_edges],
        ["package", "dependency", "type"],
    )
    nodes = {n for e in tree_edges for n in e} | top

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Reachable edges", f"{len(tree_edges):,}")
    tbl.add_row("Unique nodes",    f"{len(nodes):,}")
    tbl.add_row("Elapsed",         f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return tree_edges


def step_github(nodes: set[str], rows: list[dict]) -> dict[str, str]:
    console.rule("[bold cyan]Step 3 — github-repos.csv")
    t0 = time.perf_counter()
    lookup = {r["project"]: r["github_repo"] for r in rows if r["github_repo"]}
    pkg_to_repo = {n: lookup[n] for n in nodes if n in lookup}
    atomic_write(
        OUT_GITHUB,
        sorted(
            [{"package": p, "github_repo": r} for p, r in pkg_to_repo.items()],
            key=lambda r: r["package"],
        ),
        ["package", "github_repo"],
    )

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Tree nodes",       f"{len(nodes):,}")
    tbl.add_row("With github repo", f"{len(pkg_to_repo):,}")
    tbl.add_row("Coverage",         f"{len(pkg_to_repo) / max(len(nodes), 1) * 100:.1f}%")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)
    return pkg_to_repo


RESULTS_FIELDS = ["package", "github_repo",
                  "debian_avg_downloads", "homebrew_avg_downloads",
                  "downloads_score", "pagerank", "value_class"]


def step_results(
    tree_edges: list[tuple[str, str]],
    top: set[str],
    rows: list[dict],
    github_repos: dict[str, str],
) -> None:
    """PageRank over the combined dep graph, with install-weighted
    personalization (downloads_score) anchoring scores to real-world usage.
    Assigns A/B/C/D value classes via cumulative PR share cutoffs from
    src/settings.json."""
    console.rule("[bold cyan]Step 4 — results.csv (pagerank)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep in tree_edges:
        G.add_edge(pkg, dep)
    for n in top:
        G.add_node(n)

    by_project = {r["project"]: r for r in rows}
    scores = {n: by_project.get(n, {}).get("downloads_score", 0) for n in G.nodes()}
    total_score = sum(scores.values())
    personalization = (
        {n: v / total_score for n, v in scores.items()} if total_score > 0 else None
    )
    pr = nx.pagerank(G, alpha=PAGERANK_ALPHA, personalization=personalization)

    out_rows: list[dict] = []
    for n in G.nodes():
        # Drop orphan transit nodes that arrived via raw deps but have no
        # cpp project metadata (e.g. non-cpp dependencies, or
        # `debian:<split-binary>` fallbacks filtered out in combine()).
        # Their PR mass stays in the graph for correct scoring of real
        # projects, but we don't emit them as rows.
        if n not in by_project:
            continue
        r = by_project[n]
        out_rows.append({
            "package":                n,
            "github_repo":            github_repos.get(n, ""),
            "debian_avg_downloads":   r.get("debian_avg_downloads",   0),
            "homebrew_avg_downloads": r.get("homebrew_avg_downloads", 0),
            "downloads_score":        scores.get(n, 0),
            "pagerank":               f"{pr.get(n, 0.0):.8f}",
        })
    # Name tie-break — see npm/process_data.py for why.
    out_rows.sort(key=lambda r: (-float(r["pagerank"]), r["package"]))

    total_pr = sum(float(r["pagerank"]) for r in out_rows)
    cum = 0.0
    for r in out_rows:
        cum += float(r["pagerank"])
        share = cum / total_pr if total_pr > 0 else 1.0
        r["value_class"] = assign_value_class(share)

    # Keep the enrichment later value-stage steps add in place (repo_id,
    # canonical_url, license) — a rebuild must not drop them.
    out_rows, _cols = merge_preserved_columns(OUT_RESULTS, list(RESULTS_FIELDS), out_rows)
    atomic_write(OUT_RESULTS, out_rows, _cols)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Nodes",            f"{G.number_of_nodes():,}")
    tbl.add_row("Edges",            f"{G.number_of_edges():,}")
    tbl.add_row("With github",      f"{sum(1 for r in out_rows if r['github_repo']):,}")
    tbl.add_row("Score weights",    f"A={DOWNLOADS_SCORE_DEBIAN_WEIGHT} "
                                    f"B={DOWNLOADS_SCORE_HOMEBREW_WEIGHT}")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 by PageRank (combined C/C++ graph)", header_style="bold green")
    preview.add_column("Package")
    preview.add_column("GitHub")
    preview.add_column("Deb avg",  justify="right")
    preview.add_column("Brew avg", justify="right")
    preview.add_column("Score",    justify="right")
    preview.add_column("PageRank", justify="right")
    preview.add_column("VC", justify="center")
    for r in out_rows[:15]:
        preview.add_row(
            r["package"][:30],
            (r["github_repo"] or "[dim]—[/dim]")[:28],
            f"{r['debian_avg_downloads']:,}"   if r["debian_avg_downloads"]   else "[dim]—[/dim]",
            f"{r['homebrew_avg_downloads']:,}" if r["homebrew_avg_downloads"] else "[dim]—[/dim]",
            f"{r['downloads_score']:,}",
            r["pagerank"],
            r["value_class"],
        )
    console.print(preview)


# ── main ─────────────────────────────────────────────────────────────────────

def load_ossfuzz_slugs() -> set[str]:
    if not os.path.exists(OSSFUZZ_PROJECTS):
        return set()
    with open(OSSFUZZ_PROJECTS, newline="", encoding="utf-8") as f:
        return {
            row["github_repo"].lower() for row in csv.DictReader(f)
            if row["github_repo"] and row.get("language") in ("c", "c++")
        }


def _enrich_deb_map(deb_map: dict[str, str], bin_to_src: dict[str, str]) -> int:
    """Add binary → project entries derived from (binary → source → project).

    Catches the case where `data/sources/debian/results.csv` has a 'source' that's
    really a binary name — happens when a binary's metadata row is missing
    or for pre-t64 aliases that leaked into raw deps. Without this,
    `libcurl3-gnutls`, `libxmlsec1-openssl`, etc. stay as orphan `debian:…`
    nodes instead of rolling up into `curl` / `xmlsec1`. Returns the number
    of aliases added."""
    added = 0
    for binary, source in bin_to_src.items():
        if binary not in deb_map and source in deb_map:
            deb_map[binary] = deb_map[source]
            added += 1
    return added


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top-share", type=float, default=TOP_THRESHOLD_PCT,
                   help="Cumulative-download share cap for top-packages. "
                        # argparse %-formats help strings, so a literal % is %%
                        f"Default {TOP_THRESHOLD_PCT}%% from src/settings.json.")
    args = p.parse_args()

    console.rule("[bold white]cpp — process_data.py")
    console.print(f"  Started   : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Top share : [cyan]top {args.top_share}% of cum dl[/cyan]")
    console.print(f"  Weights   : [cyan]A={DOWNLOADS_SCORE_DEBIAN_WEIGHT} "
                  f"B={DOWNLOADS_SCORE_HOMEBREW_WEIGHT}[/cyan]\n")
    t_total = time.perf_counter()

    deb_map, hb_map = load_repology()
    bin_to_src = build_bin_to_source(load_debian_aliases())
    n_enriched = _enrich_deb_map(deb_map, bin_to_src)
    console.print(f"  [dim]enriched deb_map with {n_enriched:,} binary→project aliases[/dim]")

    deb_signals   = load_debian_signals()
    hb_signals    = load_homebrew_signals()
    ossfuzz_slugs = load_ossfuzz_slugs()

    console.print(
        f"  [dim]repology: debian={len(deb_map):,}  homebrew={len(hb_map):,}  "
        f"signals: debian_sources={len(deb_signals):,}  "
        f"homebrew_formulas={len(hb_signals):,}  "
        f"binaries={len(bin_to_src):,}[/dim]\n"
    )

    deb_edges = build_debian_edges(bin_to_src, deb_map)
    hb_edges  = build_homebrew_edges(hb_map)
    rows, edges = combine(
        deb_signals, hb_signals, deb_map, hb_map, deb_edges, hb_edges, ossfuzz_slugs
    )

    step_packages(rows)
    top          = step_top(rows, args.top_share)
    tree_edges   = step_dep_tree(top, edges)
    nodes        = {n for e in tree_edges for n in e} | top
    github_repos = step_github(nodes, rows)
    step_results(tree_edges, top, rows, github_repos)

    console.rule()
    console.print(f"  [bold green]Done[/bold green] — {time.perf_counter() - t_total:.2f}s")


if __name__ == "__main__":
    main()
