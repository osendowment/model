"""Unit and integration tests for crates pipeline (src/crates/process_data.py)."""

import csv
import re
from collections import defaultdict
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Inline reimplementation of pure helpers from process_data.py
# (importing the module directly would execute all top-level pipeline code,
# so we copy only the tiny pure functions / constants under test)
# ---------------------------------------------------------------------------

KIND_MAP = {"0": "normal", "1": "build", "2": "dev"}


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

TOP_CSV     = "data/crates/top-packages.csv"
DEPS_CSV    = "data/crates/dependency-tree.csv"
GITHUB_CSV  = "data/crates/github-repos.csv"
RESULTS_CSV = "data/crates/results.csv"

YEAR_COLS = ["2021", "2022", "2023", "2024", "2025"]


# ---------------------------------------------------------------------------
# 1. parse_owner_repo()
# ---------------------------------------------------------------------------

def test_parse_owner_repo_plain():
    assert parse_owner_repo("https://github.com/owner/repo") == "owner/repo"


def test_parse_owner_repo_git_suffix():
    assert parse_owner_repo("https://github.com/owner/repo.git") == "owner/repo"


def test_parse_owner_repo_git_plus_https():
    assert parse_owner_repo("git+https://github.com/Owner/Repo.git") == "owner/repo"


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


def test_parse_owner_repo_none_like_empty():
    # Callers pass "" when repo field is missing; must return None
    assert parse_owner_repo("") is None


# ---------------------------------------------------------------------------
# 2. KIND_MAP
# ---------------------------------------------------------------------------

def test_kind_map_values():
    assert KIND_MAP == {"0": "normal", "1": "build", "2": "dev"}


def test_kind_map_keys_are_strings():
    for k in KIND_MAP:
        assert isinstance(k, str), f"Key {k!r} should be a string"


# ---------------------------------------------------------------------------
# 3. Transitive dep-tree BFS expansion (logic copied from process_data.py)
# ---------------------------------------------------------------------------

def _run_bfs(top_crate_ids, crate_default_vid, vid_to_deps, crate_id_to_name):
    """Replicate the BFS loop from Step 4 of process_data.py."""
    seen_edges: set[tuple[str, str, str]] = set()
    all_edges:  list[tuple[str, str, str]] = []
    universe_cids: set[int] = set(top_crate_ids)
    frontier: set[int] = set(top_crate_ids)

    while frontier:
        new_frontier: set[int] = set()
        for cid in frontier:
            vid = crate_default_vid.get(cid)
            if vid is None:
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
        frontier = new_frontier

    return universe_cids, all_edges


def test_bfs_direct_deps_only():
    """Top package A depends on B; B has no deps. Universe = {A, B}, one edge."""
    crate_id_to_name = {1: "a", 2: "b"}
    crate_default_vid = {1: 101, 2: 202}
    vid_to_deps = {101: [(2, "normal")], 202: []}

    universe, edges = _run_bfs({1}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert universe == {1, 2}
    assert ("a", "b", "normal") in edges
    assert len(edges) == 1


def test_bfs_transitive_deps():
    """A → B → C (two hops). Universe = {A, B, C}, two edges."""
    crate_id_to_name = {1: "a", 2: "b", 3: "c"}
    crate_default_vid = {1: 101, 2: 202, 3: 303}
    vid_to_deps = {101: [(2, "normal")], 202: [(3, "dev")], 303: []}

    universe, edges = _run_bfs({1}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert universe == {1, 2, 3}
    assert ("a", "b", "normal") in edges
    assert ("b", "c", "dev") in edges
    assert len(edges) == 2


def test_bfs_deduplicated_edges():
    """Two top packages both depend on the same crate; edge appears only once."""
    crate_id_to_name = {1: "a", 2: "b", 3: "shared"}
    crate_default_vid = {1: 101, 2: 202, 3: 303}
    vid_to_deps = {101: [(3, "normal")], 202: [(3, "normal")], 303: []}

    universe, edges = _run_bfs({1, 2}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert universe == {1, 2, 3}
    # Both a→shared and b→shared edges should exist (different source nodes)
    assert ("a", "shared", "normal") in edges
    assert ("b", "shared", "normal") in edges
    assert len(edges) == 2


def test_bfs_no_duplicate_same_edge():
    """If the same (pkg, dep, kind) triple would be added twice, it appears once."""
    # Two versions of A both listed under top; same edge should not duplicate
    crate_id_to_name = {1: "a", 2: "b"}
    crate_default_vid = {1: 101, 2: 202}
    vid_to_deps = {101: [(2, "normal"), (2, "normal")], 202: []}

    universe, edges = _run_bfs({1}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert edges.count(("a", "b", "normal")) == 1


def test_bfs_crate_with_no_default_version():
    """Crate in top set but no default version → skip silently; no crash."""
    crate_id_to_name = {1: "a", 2: "b"}
    crate_default_vid = {2: 202}  # crate 1 has no default version
    vid_to_deps = {202: []}

    universe, edges = _run_bfs({1, 2}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert 1 in universe  # still in universe (it was a top crate)
    assert 2 in universe
    assert edges == []


def test_bfs_unknown_dep_name_skipped():
    """Dependency whose crate_id has no name entry is silently skipped."""
    crate_id_to_name = {1: "a"}  # crate 2 has no name
    crate_default_vid = {1: 101}
    vid_to_deps = {101: [(2, "normal")]}

    universe, edges = _run_bfs({1}, crate_default_vid, vid_to_deps, crate_id_to_name)

    assert universe == {1}
    assert edges == []


# ---------------------------------------------------------------------------
# 4. Output CSV schema & content invariants
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
    assert len(rows) > 0, "top-packages.csv should have at least one row"


def test_dependency_tree_columns():
    cols, _ = _read_csv(DEPS_CSV)
    assert cols == ["package", "dependency", "type"]


def test_dependency_tree_non_empty():
    _, rows = _read_csv(DEPS_CSV)
    assert len(rows) > 0, "dependency-tree.csv should have at least one row"


def test_dependency_tree_type_values():
    _, rows = _read_csv(DEPS_CSV)
    valid_types = {"normal", "build", "dev"}
    bad = [r for r in rows if r["type"] not in valid_types]
    assert not bad, f"Unexpected type values: {[r['type'] for r in bad[:5]]}"


def test_github_repos_columns():
    cols, _ = _read_csv(GITHUB_CSV)
    assert cols == ["package", "github_repo"]


def test_github_repos_non_empty():
    _, rows = _read_csv(GITHUB_CSV)
    assert len(rows) > 0, "github-repos.csv should have at least one row"


def test_results_columns():
    cols, _ = _read_csv(RESULTS_CSV)
    expected = ["package", "github_repo", "avg_downloads"] + YEAR_COLS + ["top", "pagerank", "value_class"]
    assert cols == expected


def test_results_non_empty():
    _, rows = _read_csv(RESULTS_CSV)
    assert len(rows) > 0, "results.csv should have at least one row"


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
        float(r["pagerank"])      # should not raise


def test_results_value_class_valid():
    _, rows = _read_csv(RESULTS_CSV)
    valid = {"A", "B", "C", "D"}
    bad = [r for r in rows if r["value_class"] not in valid]
    assert not bad, f"Invalid value_class: {[r['value_class'] for r in bad[:5]]}"


def test_top_packages_avg_downloads_numeric():
    _, rows = _read_csv(TOP_CSV)
    for r in rows:
        assert int(r["avg_downloads"]) >= 0
