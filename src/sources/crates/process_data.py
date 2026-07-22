"""
Process all crates.io data into final output CSVs.

Steps:
  1  Load mappings from db-dump (crates, versions, default_versions, dependencies)
  2  Aggregate monthly version-downloads into per-year totals per crate
  3  Build top-packages.csv       — crates with avg ≥ 1M downloads
  4  Build dependency-tree.csv    — full transitive dep tree from top packages
  5  Build github-repos.csv       — owner/repo for every package in dep tree
  6  Build results.csv            — all dep-tree packages with downloads + PageRank

Reads:
  data/sources/crates/db-dump/{crates,versions,default_versions,dependencies}.csv
  data/sources/crates/version-downloads/YYYY-MM.csv

Outputs:
  data/sources/crates/top-packages.csv
  data/sources/crates/dependency-tree.csv
  data/sources/crates/github-repos.csv
  data/sources/crates/results.csv

Run:
    uv run src/sources/crates/process_data.py
    uv run src/sources/crates/process_data.py --min-avg 500000
    uv run src/sources/crates/process_data.py --alpha 0.90
"""

import argparse
import csv
import glob
import os
import time
from collections import defaultdict
from urllib.parse import urlparse

import networkx as nx
import pandas as pd
import polars as pl
from rich.console import Console
from rich.table import Table

from src.common.lfs import is_lfs_pointer
from src.common.tables import merge_preserved_columns
from src.common.params import TOP_THRESHOLD_PCT, PAGERANK_ALPHA, YEARS, assign_value_class, ecosystem_avg_downloads

DUMP_DIR    = "data/sources/crates/db-dump"  # slim pipeline extracts (gitignored, regenerable) — see fetch_db_dump
MONTHLY_DIR = "data/sources/crates/version-downloads"
DATA_DIR    = "data/sources/crates"

TOP_CSV     = f"{DATA_DIR}/top-packages.csv"
DEPS_CSV    = f"{DATA_DIR}/dependency-tree.csv"
GITHUB_CSV  = f"{DATA_DIR}/github-repos.csv"
RESULTS_CSV = f"{DATA_DIR}/results.csv"

# Per-crate annual download totals, cached. Step 2 re-reads 549 MB of monthly
# CSVs on every run to produce this, ~8s of a ~12s step, yet the inputs are
# immutable: a closed month's file never changes, and the version→crate map
# comes from the db-dump extracts. The cache is therefore keyed on the mtime of
# both, and recomputed the moment either moves. Regenerable, so gitignored.
ANNUAL_CACHE = f"{DATA_DIR}/download-cache/crate-annual.csv"

KIND_MAP  = {"0": "normal", "1": "build", "2": "dev"}
YEAR_COLS = [str(y) for y in YEARS]

console = Console()

parser = argparse.ArgumentParser()
parser.add_argument("--min-avg", type=int,   default=None)
parser.add_argument("--alpha",   type=float, default=PAGERANK_ALPHA)
parser.add_argument("--refresh", action="store_true",
                    help="Recompute the annual download aggregate, ignoring its cache")
args = parser.parse_args()

t_total = time.perf_counter()
console.rule("[bold]crates.io — process data")
console.print(f"DB dump  : [cyan]{DUMP_DIR}/[/cyan]")
console.print(f"Monthly  : [cyan]{MONTHLY_DIR}/[/cyan]")
if args.min_avg is not None:
    console.print(f"Years    : {YEARS[0]}–{YEARS[-1]}  |  Min avg: {args.min_avg:,}  |  PR alpha: {args.alpha}")
else:
    console.print(f"Years    : {YEARS[0]}–{YEARS[-1]}  |  Top {TOP_THRESHOLD_PCT}% cumulative DL  |  PR alpha: {args.alpha}")
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


# ── Step 1: Load db-dump mappings ─────────────────────────────────────────────

console.rule("[bold]Step 1[/bold] — load db-dump mappings")

t = time.perf_counter()
if is_lfs_pointer(f"{DUMP_DIR}/crates.csv"):
    raise SystemExit(
        f"{DUMP_DIR}/crates.csv is a Git LFS pointer — the crates db-dump isn't "
        "materialised. Run `uv run python -m src.sources.crates.fetch_db_dump` "
        "(or `git lfs pull`) to fetch it, then re-run."
    )
console.print("Loading crates.csv …")
df = pl.read_csv(f"{DUMP_DIR}/crates.csv", columns=["id", "name", "repository"],
                 schema_overrides={"id": pl.Int64}, truncate_ragged_lines=True, ignore_errors=True)
# crate_id_to_name: int → crate name (e.g. 1 → "rand")
crate_id_to_name: dict[int, str] = dict(zip(df["id"].to_list(), df["name"].to_list()))
# crate_id_to_repo: int → raw repository URL string (empty string if null in dump)
crate_id_to_repo: dict[int, str] = {cid: (repo or "") for cid, repo in zip(df["id"].to_list(), df["repository"].to_list())}
console.print(f"  {len(crate_id_to_name):,} crates  ({time.perf_counter()-t:.1f}s)")

t = time.perf_counter()
console.print("Loading versions.csv …")
df = pl.read_csv(f"{DUMP_DIR}/versions.csv", columns=["id", "crate_id"],
                 schema_overrides={"id": pl.Int64, "crate_id": pl.Int64},
                 truncate_ragged_lines=True, ignore_errors=True)
# version_to_crate: version_id → crate_id (needed to map download records back to crates)
version_to_crate: dict[int, int] = dict(zip(df["id"].to_list(), df["crate_id"].to_list()))
# crate_to_versions: crate_id → [version_id, ...] (inverse index; used if per-crate version enumeration is needed)
crate_to_versions: dict[int, list[int]] = defaultdict(list)
for vid, cid in version_to_crate.items():
    crate_to_versions[cid].append(vid)
console.print(f"  {len(version_to_crate):,} versions  ({time.perf_counter()-t:.1f}s)")

t = time.perf_counter()
console.print("Loading default_versions.csv …")
df = pl.read_csv(f"{DUMP_DIR}/default_versions.csv", columns=["version_id"],
                 schema_overrides={"version_id": pl.Int64})
# default_vids: the set of version_ids that crates.io considers the "current" version of each crate.
# Invariant: exactly one entry per crate (the latest non-yanked version).
default_vids: set[int] = set(df["version_id"].to_list())
# crate_default_vid: crate_id → version_id of its default version (used to resolve deps in Step 4)
crate_default_vid: dict[int, int] = {
    version_to_crate[vid]: vid for vid in default_vids if vid in version_to_crate
}
console.print(f"  {len(default_vids):,} default versions → {len(crate_default_vid):,} crates  ({time.perf_counter()-t:.1f}s)")

t = time.perf_counter()
console.print("Loading dependencies.csv …")
df = pl.read_csv(f"{DUMP_DIR}/dependencies.csv", columns=["version_id", "crate_id", "kind"],
                 schema_overrides={"version_id": pl.Int64, "crate_id": pl.Int64, "kind": pl.String},
                 truncate_ragged_lines=True, ignore_errors=True)
# Only load deps for default versions — this keeps the dep graph representative of current usage
# and avoids inflating edges from yanked or historical versions.
df = df.filter(pl.col("version_id").is_in(list(default_vids)))
# vid_to_deps: version_id → [(dep_crate_id, kind), ...] where kind is "normal"/"build"/"dev"
vid_to_deps: dict[int, list[tuple[int, str]]] = defaultdict(list)
for vid, dep_cid, kind_raw in zip(df["version_id"].to_list(), df["crate_id"].to_list(), df["kind"].to_list()):
    vid_to_deps[vid].append((dep_cid, KIND_MAP.get(str(kind_raw), str(kind_raw))))
console.print(f"  {len(vid_to_deps):,} default versions with deps  ({time.perf_counter()-t:.1f}s)")


# ── Step 2: Aggregate monthly downloads → per-crate annual totals ─────────────

console.rule("[bold]Step 2[/bold] — aggregate downloads")

# crate_annual[crate_id][year] = total downloads for that crate in that year
# Initialized to 0 for all years so every crate has a complete record even with no downloads.
crate_annual: dict[int, dict[int, int]] = defaultdict(lambda: {y: 0 for y in YEARS})


def _newest(*globs: str) -> float:
    """Newest mtime across every file matching the globs (0.0 if none)."""
    newest = 0.0
    for g in globs:
        for f in glob.glob(g):
            newest = max(newest, os.path.getmtime(f))
    return newest


def _load_annual_cache() -> bool:
    """Populate `crate_annual` from the cache. False when it is absent or stale.

    Stale means any monthly download file or any db-dump extract is newer than
    the cache — those are the only two inputs, so a cache newer than both is
    byte-identical to what a recompute would produce.
    """
    if args.refresh or not os.path.exists(ANNUAL_CACHE):
        return False
    if os.path.getmtime(ANNUAL_CACHE) <= _newest(f"{MONTHLY_DIR}/*.csv", f"{DUMP_DIR}/*.csv"):
        return False
    with open(ANNUAL_CACHE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            crate_annual[int(row["crate_id"])][int(row["year"])] = int(row["downloads"])
    return True


def _write_annual_cache() -> None:
    os.makedirs(os.path.dirname(ANNUAL_CACHE), exist_ok=True)
    tmp = ANNUAL_CACHE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["crate_id", "year", "downloads"])
        for cid, per_year in crate_annual.items():
            for year, dl in per_year.items():
                if dl:                      # zeros are the default; storing them
                    w.writerow([cid, year, dl])   # would triple the file for nothing
    os.replace(tmp, ANNUAL_CACHE)


_cached = _load_annual_cache()
if _cached:
    console.print(f"  [dim]annual totals loaded from {ANNUAL_CACHE} "
                  f"(inputs unchanged; --refresh to recompute)[/dim]")

for year in ([] if _cached else YEARS):
    t = time.perf_counter()
    monthly_files = sorted(glob.glob(f"{MONTHLY_DIR}/{year}-*.csv"))
    if not monthly_files:
        console.print(f"  [red]{year}: no files[/red]")
        continue
    if len(monthly_files) != 12:
        console.print(f"  [yellow]{year}: {len(monthly_files)}/12 months[/yellow]")
    dfs = [pd.read_csv(f, dtype={"version_id": "int32", "downloads": "int64"}) for f in monthly_files]
    # Aggregate all monthly files for this year at once via concat+groupby — faster than iterating months
    year_totals = pd.concat(dfs, ignore_index=True).groupby("version_id", sort=False)["downloads"].sum().to_dict()
    for vid, dl in year_totals.items():
        cid = version_to_crate.get(vid)
        if cid is not None:
            crate_annual[cid][year] += dl
    console.print(f"  {year}: {len(monthly_files)} months, {len(year_totals):,} versions  ({time.perf_counter()-t:.1f}s)")

if not _cached:
    _write_annual_cache()


# ── Step 3: top-packages.csv ──────────────────────────────────────────────────

console.rule("[bold]Step 3[/bold] — top-packages.csv")

t = time.perf_counter()

# Compute avg downloads for every crate, sorted descending
all_avgs: list[tuple[int, str, int]] = []  # (crate_id, name, avg_downloads)
for cid, name in crate_id_to_name.items():
    annual = crate_annual.get(cid, {y: 0 for y in YEARS})
    avg = sum(annual.values()) // len(YEARS)
    if avg > 0:
        all_avgs.append((cid, name, avg))
all_avgs.sort(key=lambda x: x[2], reverse=True)

if args.min_avg is not None:
    # CLI override: use hard min-avg cutoff
    min_avg_cutoff = args.min_avg
else:
    # Find cutoff at TOP_THRESHOLD_PCT of ecosystem total downloads
    eco_total = ecosystem_avg_downloads("crates")
    target = eco_total * TOP_THRESHOLD_PCT / 100
    cumulative = 0
    min_avg_cutoff = 0
    for _, _, avg in all_avgs:
        cumulative += avg
        if cumulative >= target:
            min_avg_cutoff = avg
            break

top_rows = []
# top_crate_ids: seed set for the transitive dependency expansion in Step 4
top_crate_ids: set[int] = set()
for cid, name, avg in all_avgs:
    if avg >= min_avg_cutoff:
        annual = crate_annual.get(cid, {y: 0 for y in YEARS})
        top_rows.append({"package": name, "avg_downloads": avg, **{str(y): annual[y] for y in YEARS}})
        top_crate_ids.add(cid)
# top_package_names: used in Step 6 to flag whether a dep-tree crate is itself a top-downloaded package
top_package_names: set[str] = {r["package"] for r in top_rows}

# Add avg_downloads_share (fraction of ecosystem total)
eco_total = ecosystem_avg_downloads("crates")
for r in top_rows:
    r["avg_downloads_share"] = f"{r['avg_downloads'] / eco_total:.8f}" if eco_total else "0"

write_csv(TOP_CSV, ["package", "avg_downloads", "avg_downloads_share"] + YEAR_COLS, top_rows)

table = Table(title="Top 10 crates by avg downloads", show_header=True, header_style="bold dim")
table.add_column("Package", style="bold")
table.add_column("Avg", justify="right")
for y in YEARS:
    table.add_column(str(y), justify="right")
for row in top_rows[:10]:
    table.add_row(row["package"], f"{row['avg_downloads']:,}", *[f"{row[str(y)]:,}" for y in YEARS])
console.print(table)
console.print(f"[green]✓[/green] {len(top_rows):,} packages → {TOP_CSV}  ({time.perf_counter()-t:.1f}s)")


# ── Step 4: dependency-tree.csv (transitive closure from top packages) ─────────

console.rule("[bold]Step 4[/bold] — dependency-tree.csv")

t = time.perf_counter()
seen_edges: set[tuple[str, str, str]] = set()
all_edges:  list[tuple[str, str, str]] = []
# universe_cids: every crate reachable from top packages (transitively) — grows each BFS round
universe_cids: set[int] = set(top_crate_ids)
frontier: set[int] = set(top_crate_ids)

# BFS over the dependency graph: each round expands one hop from the current frontier.
# We walk the default version's deps so the graph reflects current published state.
round_num = 0
while frontier:
    round_num += 1
    new_frontier: set[int] = set()
    for cid in frontier:
        vid = crate_default_vid.get(cid)
        if vid is None:
            # Crate has no resolvable default version (e.g. all versions yanked); skip it
            continue
        pkg = crate_id_to_name.get(cid, "")
        for dep_cid, kind in vid_to_deps.get(vid, []):
            dep = crate_id_to_name.get(dep_cid, "")
            if not dep:
                continue
            edge = (pkg, dep, kind)
            if edge not in seen_edges:
                seen_edges.add(edge)
                all_edges.append(edge)
            if dep_cid not in universe_cids:
                universe_cids.add(dep_cid)
                new_frontier.add(dep_cid)
    console.print(f"  Round {round_num}: {len(frontier):,} → {len(new_frontier):,} new  |  universe: {len(universe_cids):,}")
    frontier = new_frontier

all_edges.sort()
write_csv_rows(DEPS_CSV, ["package", "dependency", "type"], all_edges)
console.print(f"[green]✓[/green] {len(all_edges):,} edges, {len(universe_cids):,} packages → {DEPS_CSV}  ({time.perf_counter()-t:.1f}s)")


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
    # Need at least owner + repo; deeper paths (e.g. monorepos) are fine — we take only the first two segments
    return f"{parts[0].lower()}/{parts[1].lower()}" if len(parts) >= 2 else None


t = time.perf_counter()
gh_rows = sorted(
    ((crate_id_to_name[cid], parse_owner_repo(crate_id_to_repo.get(cid, "")))
     for cid in universe_cids if cid in crate_id_to_name),
    key=lambda x: x[0],
)
gh_rows = [(name, repo) for name, repo in gh_rows if repo]
github_map: dict[str, str] = dict(gh_rows)

write_csv_rows(GITHUB_CSV, ["package", "github_repo"], gh_rows)
console.print(f"  {len(universe_cids):,} in tree | {len(gh_rows):,} with GitHub repo  ({time.perf_counter()-t:.1f}s)")
console.print(f"[green]✓[/green] {len(gh_rows):,} mappings → {GITHUB_CSV}")


# ── Step 6: results.csv ───────────────────────────────────────────────────────

console.rule("[bold]Step 6[/bold] — results.csv")

t = time.perf_counter()
console.print("Computing PageRank …")
# Directed graph: edge A→B means "A depends on B", so B accumulates rank from everything that depends on it
G = nx.DiGraph()
for pkg, dep, _ in all_edges:
    if pkg != dep:  # skip self-loops (rare but present in the dump)
        G.add_edge(pkg, dep)
# Ensure isolated nodes (crates in the universe with no edges) still appear in PageRank output
for cid in universe_cids:
    if name := crate_id_to_name.get(cid):
        G.add_node(name)

total_dl = sum(sum(crate_annual.get(cid, {}).values()) // len(YEARS) for cid in universe_cids) or 1
# Download-weighted PageRank: personalization biases the random-surfer restart toward highly-downloaded
# crates, so a package that is both widely depended upon AND widely downloaded ranks higher.
# name_to_cid is built here to avoid a linear scan per node when constructing personalization weights.
name_to_cid = {v: k for k, v in crate_id_to_name.items()}
personalization = {
    node: sum(crate_annual.get(name_to_cid.get(node, -1), {}).values()) // len(YEARS) / total_dl
    for node in G.nodes
}
pr = nx.pagerank(G, alpha=args.alpha, personalization=personalization)
console.print(f"  {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges  ({time.perf_counter()-t:.1f}s)")

result_rows = []
for cid in universe_cids:
    name = crate_id_to_name.get(cid)
    if not name:
        continue
    annual = crate_annual.get(cid, {y: 0 for y in YEARS})
    avg = sum(annual.values()) // len(YEARS)
    result_rows.append({
        "package":       name,
        "github_repo":   github_map.get(name, ""),
        "avg_downloads": avg,
        **{str(y): annual.get(y, 0) for y in YEARS},
        # "top" flags whether this crate crossed the min-avg threshold on its own (vs. pulled in as a dep)
        "top":           name in top_package_names,
        "pagerank":      round(pr.get(name, 0.0), 8),
    })
result_rows.sort(key=lambda r: r["pagerank"], reverse=True)

# Assign value classes by cumulative pagerank share
total_pr = sum(r["pagerank"] for r in result_rows)
cum = 0.0
for r in result_rows:
    cum += r["pagerank"]
    share = cum / total_pr if total_pr > 0 else 1.0
    r["value_class"] = assign_value_class(share)

_res_cols = ["package", "github_repo", "avg_downloads"] + YEAR_COLS + ["top", "pagerank", "value_class"]
# Keep the enrichment later value-stage steps add in place (git, eco_guess,
# repo_id, canonical_url, license) — a rebuild must not drop them.
result_rows, _res_cols = merge_preserved_columns(RESULTS_CSV, _res_cols, result_rows)
write_csv(RESULTS_CSV, _res_cols, result_rows)

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
