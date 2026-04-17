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
  data/repology/packages.csv              project ↔ (repo, srcname, binname, visiblename)
  data/debian/raw/downloads.csv           binary, year, downloads
  data/debian/raw/dependencies.csv        binary, dep_name
  data/debian/raw/package-metadata.csv    binary → source, homepage, vcs_browser
  data/debian/raw/aliases.csv             t64 renames: current ↔ old
  data/debian/raw/cpp-packages.csv        debtags-identified C/C++ binaries
  data/homebrew/raw/downloads.csv         formula, year, downloads
  data/homebrew/raw/dependencies.csv      formula, dep_name, dep_type
  data/homebrew/raw/formulas.csv          formula → language, homepage, source_url
  data/ossfuzz/projects.csv               OSS-Fuzz C/C++ whitelist (github slugs)

Outputs:
  data/cpp/raw/packages.csv               per-project join + aggregated signals
  data/cpp/top-packages.csv               project, avg_downloads, 2021..2025
  data/cpp/dependency-tree.csv            project, dependency, type
  data/cpp/github-repos.csv               project, github_repo
  data/cpp/results.csv                    project, github_repo, avg, yearly, top, pagerank, value_class

Aggregation rules (per Repology project):
  downloads  — within ecosystem: MAX across constituent names (e.g. boost1.74 vs
               1.81 vs 1.83 → same machines, don't double-count). Across
               ecosystems: SUM (Debian + Homebrew are disjoint populations).
  deps       — union of project→project edges from both ecosystems (deduped,
               self-loops dropped)
  is_cpp     — True if any Debian binary or Homebrew formula rolling up to the
               project is flagged C/C++
  github     — first non-empty github slug across all contributing rows
  oss_fuzz   — github slug matches the OSS-Fuzz whitelist

Run:
    uv run src/cpp/process_data.py
    uv run src/cpp/process_data.py --top-min 10000
    uv run src/cpp/process_data.py --include-non-cpp
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

from src.params import TOP_THRESHOLD_PCT, PAGERANK_ALPHA, YEARS, assign_value_class

console = Console()

# ── paths / config ────────────────────────────────────────────────────────────

REPOLOGY_CSV       = "data/repology/packages.csv"

# Pre-aggregated per-ecosystem results (source for signals + percentile pools)
DEBIAN_RESULTS     = "data/debian/results.csv"
HOMEBREW_RESULTS   = "data/homebrew/results.csv"

# Raw files — only needed for raw-level dep edges (the per-ecosystem pipelines
# don't export unfiltered source/formula edges, only BFS-trimmed trees)
DEBIAN_DEPS        = "data/debian/raw/dependencies.csv"
DEBIAN_METADATA    = "data/debian/raw/package-metadata.csv"
DEBIAN_ALIASES     = "data/debian/raw/aliases.csv"
HOMEBREW_DEPS      = "data/homebrew/raw/dependencies.csv"

OSSFUZZ_PROJECTS   = "data/ossfuzz/projects.csv"

OUT_RAW_PACKAGES   = "data/cpp/raw/packages.csv"
OUT_TOP            = "data/cpp/top-packages.csv"
OUT_DEP_TREE       = "data/cpp/dependency-tree.csv"
OUT_GITHUB         = "data/cpp/github-repos.csv"
OUT_RESULTS        = "data/cpp/results.csv"

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

def load_repology() -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    """Return (debian_name_to_project, homebrew_name_to_project, project_meta).

    `debian_name_to_project` indexes srcname/binname/visiblename — any of which
    our upstream data might carry.  Same for homebrew.
    """
    deb: dict[str, str] = {}
    hb: dict[str, str] = {}
    meta: dict[str, dict] = defaultdict(lambda: {"categories": set(), "licenses": set()})

    with open(REPOLOGY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            project = row["project"]
            repo = row["repo"]
            target = deb if repo == "debian_13" else hb if repo == "homebrew" else None
            if target is None:
                continue
            for k in ("srcname", "binname", "visiblename"):
                v = (row.get(k) or "").strip()
                if v:
                    target.setdefault(v, project)
            for cat in (row.get("categories") or "").split("|"):
                if cat:
                    meta[project]["categories"].add(cat)
            for lic in (row.get("licenses") or "").split("|"):
                if lic:
                    meta[project]["licenses"].add(lic)
    return deb, hb, meta


# ── ecosystem signal loaders ──────────────────────────────────────────────────

def _bool(v) -> bool:
    return str(v).lower() == "true"


def load_debian_aliases() -> dict[str, str]:
    if not os.path.exists(DEBIAN_ALIASES):
        return {}
    with open(DEBIAN_ALIASES, newline="", encoding="utf-8") as f:
        return {row["old"]: row["current"] for row in csv.DictReader(f)}


def build_bin_to_source(aliases: dict[str, str]) -> dict[str, str]:
    """{binary_name: source_name} + alias backfill for pre-t64 historical rows.

    Read from Debian package-metadata.csv (binaries → sources)."""
    bin_to_src: dict[str, str] = {}
    with open(DEBIAN_METADATA, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("source"):
                bin_to_src[row["package"]] = row["source"]
    for old, current in aliases.items():
        if current in bin_to_src:
            bin_to_src.setdefault(old, bin_to_src[current])
    return bin_to_src


def load_debian_signals() -> dict[str, dict]:
    """Per-source signals from the already-aggregated debian pipeline output.

    Each row is one Debian source package with avg + yearly installs, C/C++
    flag, and github slug — exactly the pool we want to rank within.
    """
    signals: dict[str, dict] = {}
    with open(DEBIAN_RESULTS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            signals[r["source"]] = {
                "avg":         _i(r["avg_downloads"]),
                "yearly":      {str(y): _i(r[str(y)]) for y in YEARS},
                "is_cpp":      _bool(r["is_cpp"]),
                "github":      r["github_repo"] or "",
                "is_oss_fuzz": _bool(r["is_oss_fuzz"]),
            }
    return signals


def load_homebrew_signals() -> dict[str, dict]:
    """Per-formula signals from the already-aggregated homebrew pipeline output."""
    signals: dict[str, dict] = {}
    with open(HOMEBREW_RESULTS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            signals[r["formula"]] = {
                "avg":         _i(r["avg_downloads"]),
                "yearly":      {str(y): _i(r[str(y)]) for y in YEARS},
                "is_cpp":      _bool(r["is_cpp"]),
                "github":      r["github_repo"] or "",
                "is_oss_fuzz": _bool(r["is_oss_fuzz"]),
            }
    return signals


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

def combine(
    deb_signals: dict[str, dict],
    hb_signals: dict[str, dict],
    deb_map: dict[str, str],
    hb_map: dict[str, str],
    deb_edges: list[tuple[str, str]],
    hb_edges: list[tuple[str, str]],
    meta: dict[str, dict],
    ossfuzz_slugs: set[str],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Roll per-source (Debian) and per-formula (Homebrew) signals up to
    Repology projects, then compute within-ecosystem percentiles.

    Within an ecosystem, the rollup is MAX across constituent names
    (boost1.74/1.81/1.83 → MAX; same machines, don't double-count).
    Across ecosystems, per-year totals are summed (Debian + Homebrew are
    disjoint user populations).
    """
    # 1. Group sources/formulas by the project they roll up to.
    project_deb_names: dict[str, set[str]] = defaultdict(set)
    project_hb_names:  dict[str, set[str]] = defaultdict(set)
    for source in deb_signals:
        project_deb_names[deb_map.get(source) or f"debian:{source}"].add(source)
    for formula in hb_signals:
        project_hb_names[hb_map.get(formula) or f"homebrew:{formula}"].add(formula)

    # 2. Collapse to project-level MAX avg within each ecosystem (so versioned
    #    duplicates like boost1.74/1.81/1.83 → boost count once), restricted
    #    to C/C++ projects with avg > 0. This is the pool we rank and share over
    #    — doing it at project level means shares sum to ~100% across all
    #    projects and ~95% within the top-95%-cum-dl slice.
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

    # 3. Build per-project rows: MAX-aggregated signals within each ecosystem,
    #    SUM-combined yearly totals across ecosystems.
    all_projects = set(project_deb_names) | set(project_hb_names)
    rows: list[dict] = []
    for project in all_projects:
        d_names = project_deb_names.get(project, set())
        h_names = project_hb_names.get(project, set())

        d_avg = max((deb_signals[n]["avg"] for n in d_names), default=0)
        h_avg = max((hb_signals[n]["avg"] for n in h_names), default=0)
        d_pct = deb_project_pct.get(project)
        h_pct = hb_project_pct.get(project)

        yearly = {
            str(y): (
                max((deb_signals[n]["yearly"][str(y)] for n in d_names), default=0)
                + max((hb_signals[n]["yearly"][str(y)] for n in h_names), default=0)
            )
            for y in YEARS
        }
        is_cpp = any(deb_signals[n]["is_cpp"] for n in d_names) or \
                 any(hb_signals[n]["is_cpp"] for n in h_names)
        github = ""
        for n in sorted(d_names):
            if deb_signals[n]["github"]:
                github = deb_signals[n]["github"]
                break
        if not github:
            for n in sorted(h_names):
                if hb_signals[n]["github"]:
                    github = hb_signals[n]["github"]
                    break
        is_oss_fuzz = (
            (github.lower() in ossfuzz_slugs if github else False)
            or any(deb_signals[n]["is_oss_fuzz"] for n in d_names)
            or any(hb_signals[n]["is_oss_fuzz"] for n in h_names)
        )

        m = meta.get(project, {"categories": set(), "licenses": set()})
        rows.append({
            "project":           project,
            "github_repo":       github,
            "debian_sources":    "|".join(sorted(d_names)),
            "homebrew_formulas": "|".join(sorted(h_names)),
            "in_debian":         str(bool(d_names)),
            "in_homebrew":       str(bool(h_names)),
            "is_cpp":            str(is_cpp),
            "is_oss_fuzz":       str(is_oss_fuzz),
            "debian_dl_avg":     d_avg,
            "homebrew_dl_avg":   h_avg,
            "debian_dl_p":       f"{d_pct:.2f}" if d_pct is not None else "",
            "homebrew_dl_p":     f"{h_pct:.2f}" if h_pct is not None else "",
            "debian_share":      f"{100 * d_avg / deb_total:.4f}" if d_avg > 0 else "",
            "homebrew_share":    f"{100 * h_avg / hb_total:.4f}" if h_avg > 0 else "",
            "categories":        "|".join(sorted(m["categories"])),
            "licenses":          "|".join(sorted(m["licenses"])),
            **yearly,
        })

    edges = sorted(set(deb_edges) | set(hb_edges))
    return rows, edges


def compute_avg(year_vals: dict[str, int]) -> int:
    pop = [year_vals[str(y)] for y in YEARS if year_vals.get(str(y), 0) > 0]
    return int(sum(pop) / len(pop)) if pop else 0


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

def step_packages(rows: list[dict]) -> None:
    """Write the bridge table — raw inputs joined and aggregated, no ranking."""
    console.rule("[bold cyan]Step 0 — raw/packages.csv")
    t0 = time.perf_counter()
    fields = (
        ["project", "github_repo", "debian_sources", "homebrew_formulas",
         "in_debian", "in_homebrew", "is_cpp", "is_oss_fuzz",
         "debian_dl_avg", "homebrew_dl_avg",
         "debian_dl_p", "homebrew_dl_p",
         "debian_share", "homebrew_share",
         "categories", "licenses"]
        + [str(y) for y in YEARS]
    )
    for r in rows:
        r["_avg"] = compute_avg({str(y): r[str(y)] for y in YEARS})
    rows.sort(key=lambda r: r["_avg"], reverse=True)
    atomic_write(OUT_RAW_PACKAGES, rows, fields)

    n_both = sum(1 for r in rows if r["in_debian"] == "True" and r["in_homebrew"] == "True")
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Total projects", f"{len(rows):,}")
    tbl.add_row("In both",        f"{n_both:,}")
    tbl.add_row("Elapsed",        f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)


def _is_top(r: dict, share_cap: float) -> bool:
    """True if the project lives inside the top-X% cumulative download mass
    of either ecosystem (smaller _p = higher in the cum-dl distribution)."""
    for col in ("debian_dl_p", "homebrew_dl_p"):
        v = r.get(col, "")
        if v and float(v) <= share_cap:
            return True
    return False


def _min_or_inf(s: str) -> float:
    return float(s) if s else float("inf")


def step_top(rows: list[dict], top_share: float, include_non_cpp: bool) -> set[str]:
    console.rule("[bold cyan]Step 1 — top-packages.csv")
    t0 = time.perf_counter()
    candidates = rows if include_non_cpp else [r for r in rows if r["is_cpp"] == "True"]
    qualifying = [r for r in candidates if _is_top(r, top_share)]
    qualifying.sort(key=lambda r: min(_min_or_inf(r["debian_dl_p"]),
                                      _min_or_inf(r["homebrew_dl_p"])))

    def _qual(v: str) -> bool:
        return bool(v) and float(v) <= top_share

    # Populate each _share column only when the package qualifies in that
    # ecosystem's top-X%. Otherwise the sum of _share would include the
    # other ecosystem's long tail and overshoot the target threshold.
    top_rows = [
        {"package":                r["project"],
         "debian_avg_downloads":   r["debian_dl_avg"],
         "debian_share":           r["debian_share"]   if _qual(r["debian_dl_p"])   else "",
         "homebrew_avg_downloads": r["homebrew_dl_avg"],
         "homebrew_share":         r["homebrew_share"] if _qual(r["homebrew_dl_p"]) else ""}
        for r in qualifying
    ]
    atomic_write(OUT_TOP, top_rows, TOP_FIELDS)
    n_both     = sum(1 for r in qualifying if _qual(r["debian_dl_p"]) and _qual(r["homebrew_dl_p"]))
    n_deb_only = sum(1 for r in qualifying if _qual(r["debian_dl_p"]) and not _qual(r["homebrew_dl_p"]))
    n_hb_only  = sum(1 for r in qualifying if _qual(r["homebrew_dl_p"]) and not _qual(r["debian_dl_p"]))

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Candidates",       f"{len(candidates):,} "
                                    f"({'all' if include_non_cpp else 'C/C++ only'})")
    tbl.add_row("Share cap",        f"top {top_share}% of cum dl (_p <= {top_share:.1f})")
    tbl.add_row("Top packages",     f"{len(top_rows):,}")
    tbl.add_row("  in both",        f"{n_both:,}")
    tbl.add_row("  Debian only",    f"{n_deb_only:,}")
    tbl.add_row("  Homebrew only",  f"{n_hb_only:,}")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 (by best cum-dl share across ecosystems)", header_style="bold green")
    preview.add_column("Package")
    preview.add_column("Debian avg", justify="right")
    preview.add_column("Debian %",   justify="right")
    preview.add_column("Brew avg",   justify="right")
    preview.add_column("Brew %",     justify="right")
    for r in qualifying[:15]:
        preview.add_row(
            r["project"][:30],
            f"{r['debian_dl_avg']:,}"   if r["debian_dl_avg"]   else "[dim]—[/dim]",
            r["debian_dl_p"]   or "[dim]—[/dim]",
            f"{r['homebrew_dl_avg']:,}" if r["homebrew_dl_avg"] else "[dim]—[/dim]",
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
    queue = deque(top)
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


def step_results(
    tree_edges: list[tuple[str, str]],
    top: set[str],
    rows: list[dict],
    github_repos: dict[str, str],
    ossfuzz_slugs: set[str],
) -> None:
    console.rule("[bold cyan]Step 4 — results.csv (pagerank)")
    t0 = time.perf_counter()

    G = nx.DiGraph()
    for pkg, dep in tree_edges:
        G.add_edge(pkg, dep)
    for n in top:
        G.add_node(n)

    by_project = {r["project"]: r for r in rows}

    all_nodes = set(G.nodes())
    total_dl = sum(by_project.get(n, {}).get("_avg", 0) for n in all_nodes)
    personalization = (
        {n: by_project.get(n, {}).get("_avg", 0) / total_dl for n in all_nodes}
        if total_dl > 0 else None
    )
    pr = nx.pagerank(G, alpha=PAGERANK_ALPHA, personalization=personalization)

    out_rows: list[dict] = []
    for n in all_nodes:
        r = by_project.get(n, {})
        slug = github_repos.get(n, "")
        out_rows.append({
            "package":       n,
            "github_repo":   slug,
            "avg_downloads": r.get("_avg", 0),
            **{str(y): r.get(str(y), 0) for y in YEARS},
            "top":           str(n in top),
            "is_cpp":        r.get("is_cpp", "False"),
            "is_oss_fuzz":   str(slug.lower() in ossfuzz_slugs) if slug else "False",
            "pagerank":      f"{pr.get(n, 0.0):.8f}",
        })
    out_rows.sort(key=lambda r: float(r["pagerank"]), reverse=True)

    total_pr = sum(float(r["pagerank"]) for r in out_rows)
    cum = 0.0
    for r in out_rows:
        cum += float(r["pagerank"])
        share = cum / total_pr if total_pr > 0 else 1.0
        r["value_class"] = assign_value_class(share)

    fields = (
        ["package", "github_repo", "avg_downloads"]
        + [str(y) for y in YEARS]
        + ["top", "is_cpp", "is_oss_fuzz", "pagerank", "value_class"]
    )
    atomic_write(OUT_RESULTS, out_rows, fields)

    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row("Nodes",            f"{G.number_of_nodes():,}")
    tbl.add_row("Edges",            f"{G.number_of_edges():,}")
    tbl.add_row("C/C++ nodes",      f"{sum(1 for r in out_rows if r['is_cpp'] == 'True'):,}")
    tbl.add_row("With github",      f"{sum(1 for r in out_rows if r['github_repo']):,}")
    tbl.add_row("OSS-Fuzz matches", f"{sum(1 for r in out_rows if r['is_oss_fuzz'] == 'True'):,}")
    tbl.add_row("Elapsed",          f"{time.perf_counter() - t0:.2f}s")
    console.print(tbl)

    preview = Table(title="Top 15 by PageRank (combined C/C++ graph)", header_style="bold green")
    preview.add_column("Package")
    preview.add_column("GitHub")
    preview.add_column("PageRank", justify="right")
    preview.add_column("Avg installs", justify="right")
    preview.add_column("C/C++", justify="center")
    preview.add_column("Top?",  justify="center")
    for r in out_rows[:15]:
        preview.add_row(
            r["package"][:30],
            (r["github_repo"] or "[dim]—[/dim]")[:28],
            r["pagerank"],
            f"{r['avg_downloads']:,}",
            "[green]yes[/green]" if r["is_cpp"] == "True" else "[dim]no[/dim]",
            "[green]yes[/green]" if r["top"]    == "True" else "[dim]no[/dim]",
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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top-share", type=float, default=TOP_THRESHOLD_PCT,
                   help="Cumulative-download share cap. A project qualifies for "
                        "top-packages if its cum-dl share _p <= X in either "
                        "Debian or Homebrew (default 50 → top half of install mass)")
    p.add_argument("--include-non-cpp", action="store_true",
                   help="Don't filter to C/C++ projects (default: C/C++ only)")
    args = p.parse_args()

    console.rule("[bold white]cpp — process_data.py")
    console.print(f"  Started         : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  TOP_SHARE       : [cyan]top {args.top_share}% of cum dl (default: {TOP_THRESHOLD_PCT}%)[/cyan]")
    console.print(f"  Include non-C++ : [cyan]{args.include_non_cpp}[/cyan]\n")
    t_total = time.perf_counter()

    deb_map, hb_map, meta = load_repology()
    aliases = load_debian_aliases()
    bin_to_src = build_bin_to_source(aliases)

    deb_signals = load_debian_signals()
    hb_signals  = load_homebrew_signals()

    console.print(
        f"  [dim]repology keys: debian={len(deb_map):,}  homebrew={len(hb_map):,}  "
        f"projects={len(meta):,}[/dim]"
    )
    console.print(
        f"  [dim]debian: sources={len(deb_signals):,}  "
        f"homebrew: formulas={len(hb_signals):,}  "
        f"binaries={len(bin_to_src):,}[/dim]\n"
    )

    ossfuzz_slugs = load_ossfuzz_slugs()

    deb_edges = build_debian_edges(bin_to_src, deb_map)
    hb_edges  = build_homebrew_edges(hb_map)
    rows, edges = combine(
        deb_signals, hb_signals, deb_map, hb_map, deb_edges, hb_edges, meta, ossfuzz_slugs
    )

    step_packages(rows)
    top = step_top(rows, args.top_share, args.include_non_cpp)
    tree_edges = step_dep_tree(top, edges)
    nodes = {n for e in tree_edges for n in e} | top
    github_repos = step_github(nodes, rows)
    step_results(tree_edges, top, rows, github_repos, ossfuzz_slugs)

    console.rule()
    console.print(f"  [bold green]Done[/bold green] — {time.perf_counter() - t_total:.2f}s")


if __name__ == "__main__":
    main()
