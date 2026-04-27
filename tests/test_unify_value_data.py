"""Tests for src/unify_value_data.py — covers helper readers, per-ecosystem
collection, repo-level aggregation, sort/grouping invariants, and the
end-to-end CSV writer.

Synthetic data is materialised under `tmp_path` so tests don't depend on
the real `data/` tree. Every non-display function in unify_value_data is
exercised; display helpers are tagged `# pragma: no cover` in the source.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.pipeline.unify_value_data import (
    CLASS_RANK,
    ECOSYSTEMS,
    FIELDS,
    _group_key,
    _normalise_repo,
    _read_dep_tree_nodes,
    _read_eol_index,
    _read_top_packages,
    _strip_internals,
    aggregate_by_repo,
    collect_ecosystem,
    write_value_data,
)


# ── small unit helpers ───────────────────────────────────────────────────────

def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)


def _eco_dir(root: Path, ecosystem: str) -> Path:
    d = root / ecosystem
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── _normalise_repo ──────────────────────────────────────────────────────────

class TestNormaliseRepo:
    def test_lowercases(self):
        assert _normalise_repo("Babel/Babel") == "babel/babel"

    def test_strips_whitespace(self):
        assert _normalise_repo("  facebook/react  ") == "facebook/react"

    def test_empty(self):
        assert _normalise_repo("") == ""

    def test_already_normal(self):
        assert _normalise_repo("rust-lang/rust") == "rust-lang/rust"


# ── _read_top_packages / _read_dep_tree_nodes / _read_eol_index ─────────────

class TestReadHelpers:
    def test_top_packages_missing_file_returns_empty(self, tmp_path):
        assert _read_top_packages(tmp_path / "absent.csv") == set()

    def test_top_packages_reads_package_col(self, tmp_path):
        p = tmp_path / "top-packages.csv"
        _write_csv(p, ["package", "avg_downloads"], [["a", "1"], ["b", "2"], ["a", "3"]])
        assert _read_top_packages(p) == {"a", "b"}

    def test_dep_tree_missing_file_returns_empty(self, tmp_path):
        assert _read_dep_tree_nodes(tmp_path / "absent.csv") == set()

    def test_dep_tree_reads_both_endpoints(self, tmp_path):
        p = tmp_path / "dependency-tree.csv"
        _write_csv(p, ["package", "dependency", "type"],
                   [["a", "b", "runtime"], ["b", "c", "runtime"]])
        assert _read_dep_tree_nodes(p) == {"a", "b", "c"}

    def test_eol_missing_file_returns_empty(self, tmp_path):
        assert _read_eol_index(tmp_path / "absent.csv") == {}

    def test_eol_parses_truthy_string(self, tmp_path):
        p = tmp_path / "eol.csv"
        _write_csv(p, ["package", "is_eol"],
                   [["dead", "True"], ["alive", "False"], ["other", "true"]])
        idx = _read_eol_index(p)
        assert idx["dead"] is True
        assert idx["alive"] is False
        # "true" (lowercase) is not "True" → False; matches existing strictness
        assert idx["other"] is False


# ── _group_key ───────────────────────────────────────────────────────────────

class TestGroupKey:
    def test_uses_github_repo_when_present(self):
        assert _group_key({"github_repo": "babel/babel", "ecosystem": "npm",
                           "package": "@babel/core"}) == "babel/babel"

    def test_synthetic_orphan_key_when_missing(self):
        key = _group_key({"github_repo": "", "ecosystem": "cpp", "package": "glibc"})
        assert key == "__orphan__:cpp:glibc"

    def test_orphans_are_unique_per_ecosystem(self):
        a = _group_key({"github_repo": "", "ecosystem": "npm", "package": "x"})
        b = _group_key({"github_repo": "", "ecosystem": "pypi", "package": "x"})
        assert a != b


# ── collect_ecosystem ───────────────────────────────────────────────────────

class TestCollectEcosystem:
    def test_results_only_no_eol_no_top(self, tmp_path):
        eco = _eco_dir(tmp_path, "npm")
        _write_csv(eco / "results.csv",
                   ["package", "github_repo", "git", "pagerank", "value_class"],
                   [
                       ["a", "Owner/Repo", "https://github.com/Owner/Repo.git", "0.5", "A"],
                       ["b", "", "", "", "D"],
                   ])
        rows, stats = collect_ecosystem("npm", data_dir=tmp_path)
        assert len(rows) == 2

        a, b = rows
        assert a["package"] == "a"
        assert a["github_repo"] == "owner/repo"  # lowercased
        assert a["git_url"] == "https://github.com/owner/repo.git"  # lowercased
        assert a["is_eol"] is False  # eol.csv missing → default False

        assert b["github_repo"] == ""
        assert b["git_url"] == ""

        assert stats["ecosystem"] == "npm"
        assert stats["results"] == 2
        assert stats["with_gh"] == 1
        assert stats["with_git"] == 1
        assert stats["gh_pct"] == 50.0
        assert stats["git_pct"] == 50.0
        assert stats["classes"]["A"] == 1
        assert stats["classes"]["D"] == 1
        assert stats["ab_total"] == 1  # only the A row
        assert stats["ab_with_gh"] == 1
        assert stats["ab_with_git"] == 1
        assert stats["ab_gh_pct"] == 100.0
        assert stats["ab_git_pct"] == 100.0
        assert stats["eol_covered"] is False
        assert stats["eol_count"] == 0
        assert stats["top"] == 0
        assert stats["deps_unique"] == 0

    def test_eol_index_propagates(self, tmp_path):
        eco = _eco_dir(tmp_path, "pypi")
        _write_csv(eco / "results.csv",
                   ["package", "github_repo", "git", "pagerank", "value_class"],
                   [["dead", "x/y", "", "0.1", "B"], ["alive", "y/z", "", "0.2", "A"]])
        _write_csv(eco / "eol.csv",
                   ["package", "is_eol"],
                   [["dead", "True"], ["alive", "False"]])
        rows, stats = collect_ecosystem("pypi", data_dir=tmp_path)
        eol_by_pkg = {r["package"]: r["is_eol"] for r in rows}
        assert eol_by_pkg == {"dead": True, "alive": False}
        assert stats["eol_covered"] is True
        assert stats["eol_count"] == 1
        assert stats["ab_eol"] == 1  # both A+B; the dead one was B

    def test_top_packages_and_deps_count_separately(self, tmp_path):
        eco = _eco_dir(tmp_path, "crates")
        _write_csv(eco / "results.csv",
                   ["package", "github_repo", "git", "pagerank", "value_class"],
                   [["serde", "", "", "0.1", "A"]])
        _write_csv(eco / "top-packages.csv",
                   ["package", "avg_downloads"],
                   [["serde", "100"], ["other", "50"]])
        _write_csv(eco / "dependency-tree.csv",
                   ["package", "dependency", "type"],
                   [["serde", "serde_derive", "runtime"]])
        _, stats = collect_ecosystem("crates", data_dir=tmp_path)
        assert stats["top"] == 2
        # union of {serde, other} ∪ {serde, serde_derive} = 3
        assert stats["deps_unique"] == 3

    def test_results_missing_returns_empty_rows(self, tmp_path):
        # No results.csv at all — collect_ecosystem must not crash
        rows, stats = collect_ecosystem("npm", data_dir=tmp_path)
        assert rows == []
        assert stats["results"] == 0
        assert stats["gh_pct"] == 0.0


# ── aggregate_by_repo ────────────────────────────────────────────────────────

def _pkg_row(package, ecosystem, github_repo="", git_url="",
             pagerank="0.0", value_class="D", is_eol=False) -> dict:
    return {
        "package": package, "ecosystem": ecosystem,
        "github_repo": github_repo, "git_url": git_url,
        "pagerank": pagerank, "value_class": value_class, "is_eol": is_eol,
    }


class TestAggregateByRepo:
    def test_empty_input(self):
        assert aggregate_by_repo([]) == []

    def test_single_package_single_ecosystem(self):
        aggs = aggregate_by_repo([
            _pkg_row("a", "npm", github_repo="x/y",
                     git_url="https://github.com/x/y.git", pagerank="1.0"),
        ])
        assert len(aggs) == 1
        a = aggs[0]
        assert a["id"] == 1
        assert a["github_repo"] == "x/y"
        assert a["git_url"] == "https://github.com/x/y.git"
        assert a["packages"] == 1
        assert a["ecosystems"] == "npm"
        assert a["top_eco"] == "npm"
        assert a["top_eco_pkg"] == "a"
        # Single-package universe: cum_share=100% → assign_value_class → D
        # top_eco_pct = 100 − 100 = 0 (it is also the *last* entry in the ranking).
        assert a["top_eco_pct"] == 0.0
        assert a["class"] == "D"
        assert a["class_npm"] == "D"
        assert a["class_pypi"] == ""

    def test_monorepo_groups_packages_by_github_repo(self):
        aggs = aggregate_by_repo([
            _pkg_row("@babel/core", "npm", github_repo="babel/babel",
                     pagerank="2.0", value_class="A"),
            _pkg_row("@babel/parser", "npm", github_repo="babel/babel",
                     pagerank="3.0", value_class="A"),
            _pkg_row("react", "npm", github_repo="facebook/react",
                     pagerank="1.0", value_class="A"),
        ])
        assert len(aggs) == 2
        babel = next(a for a in aggs if a["github_repo"] == "babel/babel")
        assert babel["packages"] == 2
        # Highest PR within babel monorepo is @babel/parser
        assert babel["top_eco_pkg"] == "@babel/parser"

    def test_orphans_kept_as_separate_groups(self):
        aggs = aggregate_by_repo([
            _pkg_row("glibc", "cpp", github_repo="", git_url="https://sourceware.org/git/glibc.git",
                     pagerank="5.0", value_class="A"),
            _pkg_row("gcc", "cpp", github_repo="", git_url="https://gcc.gnu.org/git/gcc.git",
                     pagerank="4.0", value_class="A"),
        ])
        assert len(aggs) == 2
        glibc = next(a for a in aggs if a["top_eco_pkg"] == "glibc")
        assert glibc["github_repo"] == ""
        assert glibc["git_url"] == "https://sourceware.org/git/glibc.git"

    def test_cross_ecosystem_strongest_class_wins(self):
        # Repo x/x lives in both npm and pypi. We arrange:
        #  - in pypi: x/x is the top entry with cum_share ≤ 50% → class A
        #  - in npm:  x/x is the bottom entry with cum_share = 100% → class D
        # Strongest of {A, D} is A, so `class` should be A and `top_eco` pypi.
        rows = [
            # pypi: x/x at top with 40% share, two fillers at 30% each
            _pkg_row("xpypi", "pypi", github_repo="x/x", pagerank="4.0"),
            _pkg_row("p1", "pypi", github_repo="p/1", pagerank="3.0"),
            _pkg_row("p2", "pypi", github_repo="p/2", pagerank="3.0"),
            # npm: dom dominates; x/x is a tail package
            _pkg_row("dom", "npm", github_repo="dom/dom", pagerank="100.0"),
            _pkg_row("xnpm", "npm", github_repo="x/x", pagerank="0.001"),
        ]
        aggs = aggregate_by_repo(rows)
        x = next(a for a in aggs if a["github_repo"] == "x/x")
        assert set(x["ecosystems"].split(",")) == {"npm", "pypi"}
        assert x["class_pypi"] == "A"
        assert x["class_npm"] == "D"
        # Strongest: A (pypi)
        assert x["class"] == "A"
        # top_eco is the ecosystem where x/x ranks best (pypi here)
        assert x["top_eco"] == "pypi"

    def test_class_assignment_follows_cumulative_share(self):
        # 4 single-eco repos with PRs picked so the cum-share cutoffs (50/75/90)
        # land on each: A=top 50%, B=next 25%, C=next 15%, D=last 10%.
        rows = [
            _pkg_row("p1", "npm", github_repo="r/1", pagerank="50"),  # A
            _pkg_row("p2", "npm", github_repo="r/2", pagerank="25"),  # B
            _pkg_row("p3", "npm", github_repo="r/3", pagerank="15"),  # C
            _pkg_row("p4", "npm", github_repo="r/4", pagerank="10"),  # D
        ]
        aggs = aggregate_by_repo(rows)
        by_repo = {a["github_repo"]: a["class_npm"] for a in aggs}
        assert by_repo == {"r/1": "A", "r/2": "B", "r/3": "C", "r/4": "D"}

    def test_sort_orders_by_top_eco_pct_desc(self):
        rows = [
            _pkg_row("low", "npm", github_repo="z/z", pagerank="1.0"),
            _pkg_row("hi", "npm", github_repo="a/a", pagerank="100.0"),
            _pkg_row("mid", "npm", github_repo="m/m", pagerank="10.0"),
        ]
        aggs = aggregate_by_repo(rows)
        # `hi` should be id=1, `low` last.
        assert [a["github_repo"] for a in aggs] == ["a/a", "m/m", "z/z"]
        assert [a["id"] for a in aggs] == [1, 2, 3]

    def test_sort_pushes_no_pr_groups_to_end(self):
        rows = [
            _pkg_row("noprrowdata", "npm", github_repo="b/b", pagerank=""),  # no PR
            _pkg_row("withpr", "npm", github_repo="a/a", pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows)
        # The PR-bearing row must come first; the PR-less row sinks
        first, second = aggs
        assert first["github_repo"] == "a/a"
        assert first["top_eco_pct"] == 0.0  # only entry → cum_share=100% → pct=0
        # The no-PR group has total=0 path; it still gets a class (D) but is sorted after.
        assert second["github_repo"] == "b/b"

    def test_id_is_sequential_starting_at_1(self):
        rows = [_pkg_row(f"p{i}", "npm", github_repo=f"r/{i}", pagerank=str(10 - i))
                for i in range(5)]
        aggs = aggregate_by_repo(rows)
        assert [a["id"] for a in aggs] == [1, 2, 3, 4, 5]

    def test_internals_are_stripped_from_output(self):
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows)
        for a in aggs:
            for k in a:
                assert not k.startswith("_pkgs_")
                assert not k.startswith("_pr_sum_")
                assert not k.startswith("_pr_pct_")
                assert not k.startswith("_top_pkg_")
                assert k != "group_key"

    def test_git_url_preferred_from_first_member_with_url(self):
        rows = [
            _pkg_row("a", "npm", github_repo="x/y", git_url=""),
            _pkg_row("b", "npm", github_repo="x/y",
                     git_url="https://github.com/x/y.git"),
        ]
        aggs = aggregate_by_repo(rows)
        assert aggs[0]["git_url"] == "https://github.com/x/y.git"

    def test_packages_count_matches_membership(self):
        rows = [_pkg_row(f"p{i}", "npm", github_repo="x/y", pagerank=str(i))
                for i in range(7)]
        aggs = aggregate_by_repo(rows)
        assert aggs[0]["packages"] == 7

    def test_ecosystems_field_is_csv_in_canonical_order(self):
        # Force one repo to appear in pypi + crates + cpp; ECOSYSTEMS order
        # should drive the csv list (npm,pypi,crates,cpp); the result is
        # comma-joined of *present* ecos, in that order.
        rows = [
            _pkg_row("a", "crates", github_repo="x/y", pagerank="1"),
            _pkg_row("b", "cpp", github_repo="x/y", pagerank="1"),
            _pkg_row("c", "pypi", github_repo="x/y", pagerank="1"),
        ]
        aggs = aggregate_by_repo(rows)
        assert aggs[0]["ecosystems"] == "pypi,crates,cpp"


# ── _strip_internals direct test ─────────────────────────────────────────────

class TestStripInternals:
    def test_drops_scratch_keys(self):
        d = {
            "id": 1, "github_repo": "x/y", "_pkgs_npm": 5,
            "_pr_sum_npm": 1.0, "_pr_pct_npm": 99.0, "_top_pkg_npm": "x",
            "group_key": "x/y", "class": "A",
        }
        clean = _strip_internals(d)
        assert clean == {"id": 1, "github_repo": "x/y", "class": "A"}


# ── write_value_data ─────────────────────────────────────────────────────────

class TestWriteValueData:
    def test_round_trip(self, tmp_path):
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows)
        out = tmp_path / "value-data.csv"
        write_value_data(aggs, path=out)
        with open(out, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            written = list(reader)
            assert reader.fieldnames == FIELDS
        assert len(written) == 1
        assert written[0]["github_repo"] == "x/y"
        # Single-package universe → cum_share=100% → class D (see
        # test_single_package_single_ecosystem for the reasoning).
        assert written[0]["class"] == "D"
        assert written[0]["class_pypi"] == ""

    def test_creates_parent_directory(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "value-data.csv"
        assert not out.parent.exists()
        write_value_data([], path=out)
        assert out.exists()


# ── end-to-end via collect + aggregate + write ───────────────────────────────

class TestEndToEnd:
    def test_full_pipeline_two_ecosystems(self, tmp_path):
        # npm: babel monorepo (2 packages, 1 repo) + lodash (1 pkg, 1 repo)
        npm = _eco_dir(tmp_path, "npm")
        _write_csv(npm / "results.csv",
                   ["package", "github_repo", "git", "pagerank", "value_class"],
                   [
                       ["@babel/core", "babel/babel",
                        "https://github.com/babel/babel.git", "5.0", "A"],
                       ["@babel/parser", "babel/babel",
                        "https://github.com/babel/babel.git", "3.0", "A"],
                       ["lodash", "lodash/lodash",
                        "https://github.com/lodash/lodash.git", "2.0", "A"],
                   ])
        # cpp: glibc orphan (no github_repo, sourceware URL)
        cpp = _eco_dir(tmp_path, "cpp")
        _write_csv(cpp / "results.csv",
                   ["package", "github_repo", "git", "pagerank", "value_class"],
                   [
                       ["glibc", "", "https://sourceware.org/git/glibc.git", "10.0", "A"],
                   ])

        all_rows: list[dict] = []
        for eco in ECOSYSTEMS:
            r, _ = collect_ecosystem(eco, data_dir=tmp_path)
            all_rows.extend(r)

        aggs = aggregate_by_repo(all_rows)
        assert len(aggs) == 3  # babel/babel, lodash/lodash, glibc orphan

        out = tmp_path / "value-data.csv"
        write_value_data(aggs, path=out)

        with open(out, encoding="utf-8") as f:
            written = list(csv.DictReader(f))

        # Sorted by top_eco_pct desc; ids sequential
        assert [int(r["id"]) for r in written] == [1, 2, 3]
        # All written rows have exactly the canonical FIELDS, in order
        with open(out, encoding="utf-8") as f:
            assert csv.DictReader(f).fieldnames == FIELDS

        # Babel monorepo collapsed
        babel = next(r for r in written if r["github_repo"] == "babel/babel")
        assert babel["packages"] == "2"
        assert babel["top_eco_pkg"] == "@babel/core"  # higher pagerank than parser
        assert babel["ecosystems"] == "npm"

        # Glibc orphan keeps its non-GitHub git_url
        glibc = next(r for r in written if r["top_eco_pkg"] == "glibc")
        assert glibc["github_repo"] == ""
        assert glibc["git_url"] == "https://sourceware.org/git/glibc.git"
        assert glibc["ecosystems"] == "cpp"


# ── invariants ───────────────────────────────────────────────────────────────

class TestInvariants:
    def test_class_rank_covers_abcd_in_strict_order(self):
        assert CLASS_RANK == {"A": 0, "B": 1, "C": 2, "D": 3}

    def test_fields_contains_required_columns(self):
        for col in ("id", "github_repo", "git_url", "ecosystems", "packages",
                    "top_eco", "top_eco_pkg", "top_eco_pct", "class"):
            assert col in FIELDS
        for eco in ECOSYSTEMS:
            assert f"class_{eco}" in FIELDS
        # is_eol is intentionally NOT in FIELDS — owned by eligibility-data.csv
        assert "is_eol" not in FIELDS

    def test_ecosystems_tuple_canonical(self):
        assert ECOSYSTEMS == ("npm", "pypi", "crates", "cpp")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
