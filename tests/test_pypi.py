"""Unit and integration tests for PyPI pipeline (src/pypi/process_data.py)."""

import csv
import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Inline reimplementation of pure helpers from process_data.py
# (importing the module directly would execute all top-level pipeline code,
# so we copy only the tiny pure functions under test)
# ---------------------------------------------------------------------------


def parse_owner_repo(url: str) -> "str | None":
    """Copy of parse_owner_repo from process_data.py."""
    if not url or "github.com" not in url.lower():
        return None
    try:
        path = urlparse(url.strip()).path.rstrip("/")
    except Exception:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    return f"{parts[0].lower()}/{parts[1].lower()}" if len(parts) >= 2 else None


# ---------------------------------------------------------------------------
# Paths to generated output files
# ---------------------------------------------------------------------------

TOP_CSV     = "data/pypi/top-packages.csv"
DEPTREE_CSV = "data/pypi/dependency-tree.csv"
GITHUB_CSV  = "data/pypi/github-repos.csv"
RESULTS_CSV = "data/pypi/results.csv"

YEAR_COLS = ["2021", "2022", "2023", "2024", "2025"]


# ---------------------------------------------------------------------------
# 1. parse_owner_repo()
# ---------------------------------------------------------------------------

def test_parse_owner_repo_plain():
    assert parse_owner_repo("https://github.com/owner/repo") == "owner/repo"


def test_parse_owner_repo_git_suffix():
    assert parse_owner_repo("https://github.com/owner/repo.git") == "owner/repo"


def test_parse_owner_repo_lowercases():
    assert parse_owner_repo("https://github.com/MyOrg/MyRepo") == "myorg/myrepo"


def test_parse_owner_repo_trailing_slash():
    assert parse_owner_repo("https://github.com/owner/repo/") == "owner/repo"


def test_parse_owner_repo_non_github():
    assert parse_owner_repo("https://gitlab.com/owner/repo") is None


def test_parse_owner_repo_empty_string():
    assert parse_owner_repo("") is None


def test_parse_owner_repo_only_one_path_component():
    assert parse_owner_repo("https://github.com/owner") is None


# ---------------------------------------------------------------------------
# 2. Transitive dep-tree BFS expansion (logic copied from process_data.py)
# ---------------------------------------------------------------------------

def _run_bfs(top_pkgs: set[str], pkg_deps: dict, display_name: dict) -> tuple[set[str], list]:
    """Replicate the BFS loop from Step 4 of process_data.py."""
    seen_edges: set[tuple[str, str, str]] = set()
    all_edges:  list[tuple[str, str, str]] = []
    universe: set[str] = set(top_pkgs)
    frontier: set[str] = set(top_pkgs)

    while frontier:
        new_frontier: set[str] = set()
        for pkg_lower in frontier:
            pkg = display_name.get(pkg_lower, pkg_lower)
            for dep_lower, dep_type in pkg_deps.get(pkg_lower, []):
                dep = display_name.get(dep_lower, dep_lower)
                edge = (pkg, dep, dep_type)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    all_edges.append(edge)
                if dep_lower not in universe:
                    universe.add(dep_lower)
                    new_frontier.add(dep_lower)
        frontier = new_frontier

    return universe, all_edges


def test_bfs_direct_deps_only():
    """Top package a depends on b; b has no deps. Universe = {a, b}, one edge."""
    display = {"a": "a", "b": "b"}
    deps = {"a": [("b", "declared")], "b": []}

    universe, edges = _run_bfs({"a"}, deps, display)

    assert universe == {"a", "b"}
    assert ("a", "b", "declared") in edges
    assert len(edges) == 1


def test_bfs_transitive_deps():
    """a → b → c (two hops). Universe = {a, b, c}, two edges."""
    display = {"a": "a", "b": "b", "c": "c"}
    deps = {"a": [("b", "declared")], "b": [("c", "discovered")], "c": []}

    universe, edges = _run_bfs({"a"}, deps, display)

    assert universe == {"a", "b", "c"}
    assert ("a", "b", "declared") in edges
    assert ("b", "c", "discovered") in edges
    assert len(edges) == 2


def test_bfs_deduplicated_edges():
    """Two top packages both depend on the same package; each edge is distinct."""
    display = {"a": "a", "b": "b", "shared": "shared"}
    deps = {"a": [("shared", "declared")], "b": [("shared", "declared")], "shared": []}

    universe, edges = _run_bfs({"a", "b"}, deps, display)

    assert universe == {"a", "b", "shared"}
    assert ("a", "shared", "declared") in edges
    assert ("b", "shared", "declared") in edges
    assert len(edges) == 2


def test_bfs_no_duplicate_same_edge():
    """If the same (pkg, dep, type) triple appears twice in the source, it appears once in output."""
    display = {"a": "a", "b": "b"}
    deps = {"a": [("b", "declared"), ("b", "declared")], "b": []}

    _, edges = _run_bfs({"a"}, deps, display)

    assert edges.count(("a", "b", "declared")) == 1


def test_bfs_package_not_in_deps():
    """Package in top set with no dep entries — universe still contains it, no crash."""
    display = {"a": "a"}
    deps: dict = {}

    universe, edges = _run_bfs({"a"}, deps, display)

    assert "a" in universe
    assert edges == []


# ---------------------------------------------------------------------------
# 3. Output CSV schema & content invariants
# ---------------------------------------------------------------------------

def _read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def test_top_packages_columns():
    cols, _ = _read_csv(TOP_CSV)
    assert cols == ["package", "avg_downloads"] + YEAR_COLS


def test_top_packages_non_empty():
    _, rows = _read_csv(TOP_CSV)
    assert len(rows) > 0


def test_top_packages_avg_downloads_numeric():
    _, rows = _read_csv(TOP_CSV)
    for r in rows:
        assert int(r["avg_downloads"]) >= 0


def test_dependency_tree_columns():
    cols, _ = _read_csv(DEPTREE_CSV)
    assert cols == ["package", "dependency", "type"]


def test_dependency_tree_non_empty():
    _, rows = _read_csv(DEPTREE_CSV)
    assert len(rows) > 0


def test_dependency_tree_type_values():
    _, rows = _read_csv(DEPTREE_CSV)
    valid_types = {"declared", "discovered"}
    bad = [r for r in rows if r["type"] not in valid_types]
    assert not bad, f"Unexpected type values: {[r['type'] for r in bad[:5]]}"


def test_github_repos_columns():
    cols, _ = _read_csv(GITHUB_CSV)
    assert cols == ["package", "github_repo"]


def test_github_repos_non_empty():
    _, rows = _read_csv(GITHUB_CSV)
    assert len(rows) > 0


def test_results_columns():
    cols, _ = _read_csv(RESULTS_CSV)
    expected = ["package", "github_repo", "avg_downloads"] + YEAR_COLS + ["top", "pagerank", "pagerank_dl"]
    assert cols == expected


def test_results_non_empty():
    _, rows = _read_csv(RESULTS_CSV)
    assert len(rows) > 0


def test_results_top_values():
    _, rows = _read_csv(RESULTS_CSV)
    valid_top = {"True", "False"}
    bad = [r for r in rows if r["top"] not in valid_top]
    assert not bad, f"Unexpected top values: {[r['top'] for r in bad[:5]]}"


_OWNER_REPO_RE = re.compile(r"^[a-z0-9_.\-]+/[a-z0-9_.\-]+$")


def test_results_github_repo_format():
    """Non-empty github_repo entries must match owner/repo (lowercase)."""
    _, rows = _read_csv(RESULTS_CSV)
    bad = [
        r["github_repo"]
        for r in rows
        if r["github_repo"] and not _OWNER_REPO_RE.match(r["github_repo"])
    ]
    assert not bad, f"Malformed github_repo values: {bad[:5]}"


def test_results_pagerank_numeric():
    _, rows = _read_csv(RESULTS_CSV)
    for r in rows:
        float(r["pagerank"])     # should not raise
        float(r["pagerank_dl"])  # should not raise


def test_results_sorted_by_pagerank_descending():
    _, rows = _read_csv(RESULTS_CSV)
    prs = [float(r["pagerank"]) for r in rows]
    assert prs == sorted(prs, reverse=True), "results.csv should be sorted by pagerank desc"


def test_top_packages_in_results():
    """Every package in top-packages.csv should appear in results.csv with top=True."""
    _, top_rows = _read_csv(TOP_CSV)
    _, result_rows = _read_csv(RESULTS_CSV)
    results_top = {r["package"] for r in result_rows if r["top"] == "True"}
    top_names = {r["package"] for r in top_rows}
    missing = top_names - results_top
    assert not missing, f"Top packages missing from results or not flagged top=True: {list(missing)[:5]}"
