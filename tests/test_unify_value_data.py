"""Tests for src/value.py — covers helper readers, per-ecosystem
collection, repo-level aggregation, sort/grouping invariants, and the
end-to-end CSV writer.

Synthetic data is materialised under `tmp_path` so tests don't depend on
the real `data/` tree. Every non-display function in value.py is
exercised; display helpers are tagged `# pragma: no cover` in the source.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.value.unify_value_data import (
    CLASS_RANK,
    ECOSYSTEMS,
    FIELDS,
    _github_repo_from_url,
    _group_key,
    _identity,
    _normalise_repo,
    _read_dep_tree_nodes,
    _read_eol_index,
    _read_top_packages,
    _strip_internals,
    aggregate_by_repo,
    apply_repo_overrides,
    collect_ecosystem,
    load_repo_overrides,
    write_value_data,
)


# ── _github_repo_from_url ────────────────────────────────────────────────────

class TestGithubRepoFromUrl:
    def test_extracts_owner_repo(self):
        assert _github_repo_from_url("https://github.com/psf/requests") == "psf/requests"

    def test_strips_dot_git_and_trailing_slash(self):
        assert _github_repo_from_url("https://github.com/psf/requests.git") == "psf/requests"
        assert _github_repo_from_url("https://github.com/psf/requests/") == "psf/requests"

    def test_non_github_or_empty_returns_blank(self):
        assert _github_repo_from_url("") == ""
        assert _github_repo_from_url("https://gitlab.com/x/y") == ""

    def test_reserved_namespaces_are_not_repos(self):
        # owner/repo-shaped but not repositories — must not become a slug.
        assert _github_repo_from_url("https://github.com/sponsors/hynek") == ""
        assert _github_repo_from_url("https://github.com/orgs/scikit-build") == ""
        assert _github_repo_from_url("https://github.com/topics/python") == ""
        # a real repo whose owner merely resembles a reserved word is fine.
        assert _github_repo_from_url("https://github.com/orgsync/react-list") == "orgsync/react-list"


# ── _identity ────────────────────────────────────────────────────────────────

class TestIdentity:
    def test_github_slug_wins(self):
        # A GitHub slug is present → (platform=github, repo=slug), and the
        # git_url is ignored for the identity (verify_git_urls reconciles it).
        assert _identity("owner/repo", "https://github.com/owner/repo.git") == (
            "github", "owner/repo")

    def test_github_slug_wins_over_nongithub_url(self):
        assert _identity("owner/repo", "https://gitlab.com/x/y.git") == (
            "github", "owner/repo")

    def test_derives_platform_and_repo_from_gitlab_url(self):
        # No slug → read (platform, repo) straight off the non-GitHub git_url.
        assert _identity("", "https://gitlab.com/gnome/glib.git") == (
            "gitlab", "gnome/glib")

    def test_derives_custom_host_from_url(self):
        assert _identity("", "https://sourceware.org/git/glibc.git") == (
            "custom", "git/glibc")

    def test_orphan_no_slug_no_url(self):
        assert _identity("", "") == ("", "")


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
    def test_uses_repo_id_when_present(self):
        # repo_id wins over git_url — it's the stable GitHub numeric id.
        assert _group_key({"github_repo": "react/react",
                           "git_url": "https://github.com/react/react.git",
                           "repo_id": "gh/10270250",
                           "ecosystem": "npm", "package": "react"}) == "gh/10270250"

    def test_uses_git_url_when_no_repo_id(self):
        # No repo_id → fall back to git_url.
        assert _group_key({"github_repo": "babel/babel",
                           "git_url": "https://github.com/babel/babel.git",
                           "repo_id": "",
                           "ecosystem": "npm",
                           "package": "@babel/core"}) == "https://github.com/babel/babel.git"

    def test_synthetic_orphan_key_when_no_repo_id_and_no_git_url(self):
        key = _group_key({"github_repo": "", "git_url": "", "repo_id": "",
                          "ecosystem": "cpp", "package": "glibc"})
        assert key == "__orphan__:cpp:glibc"

    def test_orphans_are_unique_per_ecosystem(self):
        a = _group_key({"github_repo": "", "git_url": "", "repo_id": "",
                        "ecosystem": "npm", "package": "x"})
        b = _group_key({"github_repo": "", "git_url": "", "repo_id": "",
                        "ecosystem": "pypi", "package": "x"})
        assert a != b

    def test_missing_repo_id_and_git_url_keys_are_safe(self):
        # Defensive: dicts without `repo_id`/`git_url` (e.g. ad-hoc test data).
        assert _group_key({"github_repo": "x/y", "ecosystem": "npm",
                           "package": "p"}) == "__orphan__:npm:p"


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

    def test_default_data_dir_is_the_sources_root(self):
        # Regression: per-ecosystem inputs live under data/sources/<eco>/
        # since the data-layout refactor. The default data_dir must point
        # there (not data/), or unify silently reads nothing and writes an
        # empty value.csv. Guard the default + the shipped results.csv path.
        import src.value.unify_value_data as mod
        assert mod.SOURCES_DIR == mod.DATA_DIR / "sources"
        assert (mod.SOURCES_DIR / "npm" / "results.csv").exists()


# ── aggregate_by_repo ────────────────────────────────────────────────────────

_AUTO_GIT = object()


def _pkg_row(package, ecosystem, github_repo="", git_url=_AUTO_GIT,
             pagerank="0.0", value_class="D", is_eol=False,
             repo_id="", mirror_url="") -> dict:
    """Test fixture for one per-package row.

    Grouping uses `repo_id` first, then `git_url`. When only `github_repo` is
    supplied and `repo_id` is "" (default), the auto-derived git_url serves as
    the group key — tests intending a monorepo should pass the same `github_repo`
    so they get the same auto-derived git_url. Pass `git_url=""` explicitly to
    override and force the orphan path.
    """
    if git_url is _AUTO_GIT:
        git_url = f"https://github.com/{github_repo}.git" if github_repo else ""
    return {
        "package": package, "ecosystem": ecosystem,
        "github_repo": github_repo, "git_url": git_url,
        "repo_id": repo_id, "mirror_url": mirror_url,
        "pagerank": pagerank, "value_class": value_class, "is_eol": is_eol,
    }


class TestAggregateByRepo:
    @pytest.fixture(autouse=True)
    def _isolate_overrides(self, monkeypatch):
        """aggregate_by_repo applies the curated overrides.csv as its last step;
        isolate these pure-aggregation tests from it, since fixtures use real
        package/repo names (glibc, gcc, …) that may appear in overrides.csv."""
        monkeypatch.setattr(
            "src.value.unify_value_data.load_repo_overrides", lambda *a, **k: {}
        )

    def test_empty_input(self):
        assert aggregate_by_repo([], drop_d_class=False) == []

    def test_single_package_single_ecosystem(self):
        aggs = aggregate_by_repo([
            _pkg_row("a", "npm", github_repo="x/y",
                     git_url="https://github.com/x/y.git", pagerank="1.0"),
        ], drop_d_class=False)
        assert len(aggs) == 1
        a = aggs[0]
        assert a["repo"] == "x/y"
        assert a["platform"] == "github"
        assert a["git_url"] == "https://github.com/x/y.git"
        assert a["packages"] == 1
        assert a["ecosystems"] == "npm"
        assert a["top_eco"] == "npm"
        assert a["top_eco_pkg"] == "a"
        # Single-package universe: cum_share=100% → assign_value_class → C
        # top_eco_pct = 100 − 100 = 0 (it is also the *last* entry in the ranking).
        assert a["top_eco_pct"] == 0.0
        assert a["class"] == "C"
        assert a["class_npm"] == "C"
        assert a["class_pypi"] == ""

    def test_monorepo_groups_packages_by_github_repo(self):
        # Two @babel/* packages share github_repo=babel/babel — they auto-derive
        # the same git_url and end up in one group. react is a separate group.
        aggs = aggregate_by_repo([
            _pkg_row("@babel/core", "npm", github_repo="babel/babel",
                     pagerank="2.0", value_class="A"),
            _pkg_row("@babel/parser", "npm", github_repo="babel/babel",
                     pagerank="3.0", value_class="A"),
            _pkg_row("react", "npm", github_repo="facebook/react",
                     pagerank="1.0", value_class="A"),
        ], drop_d_class=False)
        assert len(aggs) == 2
        babel = next(a for a in aggs if a["repo"] == "babel/babel")
        assert babel["platform"] == "github"
        assert babel["packages"] == 2
        # Highest PR within babel monorepo is @babel/parser
        assert babel["top_eco_pkg"] == "@babel/parser"

    def test_orphans_kept_as_separate_groups(self):
        aggs = aggregate_by_repo([
            _pkg_row("glibc", "cpp", github_repo="", git_url="https://sourceware.org/git/glibc.git",
                     pagerank="5.0", value_class="A"),
            _pkg_row("gcc", "cpp", github_repo="", git_url="https://gcc.gnu.org/git/gcc.git",
                     pagerank="4.0", value_class="A"),
        ], drop_d_class=False)
        assert len(aggs) == 2
        glibc = next(a for a in aggs if a["top_eco_pkg"] == "glibc")
        # Non-GitHub upstream: identity is derived from the git_url via
        # platform_and_slug (sourceware cgit → custom host, `git/glibc` path).
        assert glibc["platform"] == "custom"
        assert glibc["repo"] == "git/glibc"
        assert glibc["git_url"] == "https://sourceware.org/git/glibc.git"

    def test_cross_ecosystem_strongest_class_wins(self):
        # Repo x/x lives in both npm and pypi. We arrange:
        #  - in pypi: x/x is the top entry with cum_share ≤ 75% → class A
        #  - in npm:  x/x is the bottom entry with cum_share = 100% → class C
        # Strongest of {A, C} is A, so `class` should be A and `top_eco` pypi.
        rows = [
            # pypi: x/x at top with 40% share, two fillers at 30% each
            _pkg_row("xpypi", "pypi", github_repo="x/x", pagerank="4.0"),
            _pkg_row("p1", "pypi", github_repo="p/1", pagerank="3.0"),
            _pkg_row("p2", "pypi", github_repo="p/2", pagerank="3.0"),
            # npm: dom dominates; x/x is a tail package
            _pkg_row("dom", "npm", github_repo="dom/dom", pagerank="100.0"),
            _pkg_row("xnpm", "npm", github_repo="x/x", pagerank="0.001"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        x = next(a for a in aggs if a["repo"] == "x/x")
        assert set(x["ecosystems"].split(",")) == {"npm", "pypi"}
        assert x["class_pypi"] == "A"
        assert x["class_npm"] == "C"
        # Strongest: A (pypi)
        assert x["class"] == "A"
        # top_eco is the ecosystem where x/x ranks best (pypi here)
        assert x["top_eco"] == "pypi"

    def test_class_assignment_follows_cumulative_share(self):
        # 3 single-eco repos with PRs picked so the cum-share cutoffs (75/95)
        # land on each: A=top 75% (cum 75%), B=next 17% (cum 92%),
        # C=last 8% (cum 100%). The A boundary (cum == 75%) is inclusive.
        rows = [
            _pkg_row("p1", "npm", github_repo="r/1", pagerank="75"),  # A
            _pkg_row("p2", "npm", github_repo="r/2", pagerank="17"),  # B
            _pkg_row("p3", "npm", github_repo="r/3", pagerank="8"),   # C
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        by_repo = {a["repo"]: a["class_npm"] for a in aggs}
        assert by_repo == {"r/1": "A", "r/2": "B", "r/3": "C"}

    def test_sort_orders_by_top_eco_pct_desc(self):
        rows = [
            _pkg_row("low", "npm", github_repo="z/z", pagerank="1.0"),
            _pkg_row("hi", "npm", github_repo="a/a", pagerank="100.0"),
            _pkg_row("mid", "npm", github_repo="m/m", pagerank="10.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        # `hi` (highest PR) sorts first, `low` last.
        assert [a["repo"] for a in aggs] == ["a/a", "m/m", "z/z"]

    def test_sort_pushes_no_pr_groups_to_end(self):
        rows = [
            _pkg_row("noprrowdata", "npm", github_repo="b/b", pagerank=""),  # no PR
            _pkg_row("withpr", "npm", github_repo="a/a", pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        # The PR-bearing row must come first; the PR-less row sinks
        first, second = aggs
        assert first["repo"] == "a/a"
        assert first["top_eco_pct"] == 0.0  # only entry → cum_share=100% → pct=0
        # The no-PR group has total=0 path; it still gets a class (C) but is sorted after.
        assert second["repo"] == "b/b"


    def test_internals_are_stripped_from_output(self):
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        for a in aggs:
            for k in a:
                assert not k.startswith("_pkgs_")
                assert not k.startswith("_pr_sum_")
                assert not k.startswith("_pr_pct_")
                assert not k.startswith("_top_pkg_")
                assert k != "group_key"

    def test_github_repo_first_nonempty_member_wins(self):
        # Within a group, packages may carry github_repo or not (e.g. cpp's
        # results.csv often has empty github_repo while a sibling ecosystem
        # has it populated). The aggregate must pick the first non-empty value
        # — not just members[0]'s — so the slug isn't silently dropped.
        rows = [
            _pkg_row("a", "cpp",   github_repo="",
                     git_url="https://github.com/x/y.git", pagerank="1.0"),
            _pkg_row("b", "npm",   github_repo="x/y",
                     git_url="https://github.com/x/y.git", pagerank="2.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["repo"] == "x/y"
        assert aggs[0]["platform"] == "github"

    def test_mismatched_github_repo_does_not_collide_with_sibling_repo(self):
        # Regression: when two repos under the same owner share an
        # upstream packages naming convention but live in separate git
        # repos (e.g. `org/main` and `org/main-contrib`), some packages'
        # github_repo column points to the wrong sibling. With the URL
        # fixed for grouping, both groups would otherwise pick the same
        # `github_repo`, producing duplicate rows in value-data.csv.
        rows = [
            # Main repo: 1 package, internally consistent.
            _pkg_row("api", "pypi", github_repo="org/main",
                     git_url="https://github.com/org/main.git",
                     pagerank="1.0"),
            # Contrib repo: 2 packages. The first is mis-labelled with
            # the main repo's slug; the second is correct. Without the
            # url-derived tie-break, "first non-empty wins" picks the
            # wrong slug for the contrib group.
            _pkg_row("instrumentation", "pypi", github_repo="org/main",
                     git_url="https://github.com/org/main-contrib.git",
                     pagerank="2.0"),
            _pkg_row("contrib-other", "pypi", github_repo="org/main-contrib",
                     git_url="https://github.com/org/main-contrib.git",
                     pagerank="3.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 2
        slugs = sorted(a["repo"] for a in aggs)
        assert slugs == ["org/main", "org/main-contrib"]

    def test_keeps_member_github_repo_when_git_column_is_wrong(self):
        # Inverse of the previous case: the per-package `git` column is
        # wrong (points at a sponsorship page or unrelated mirror) but
        # the `github_repo` column is correct. The url-derived slug
        # would corrupt the row; falling back to the member's slug keeps
        # the legitimate value. Modelled on real `attrs` data.
        rows = [
            _pkg_row("attrs", "pypi", github_repo="python-attrs/attrs",
                     git_url="https://github.com/sponsors/hynek.git",
                     pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["repo"] == "python-attrs/attrs"
        assert aggs[0]["platform"] == "github"

    def test_sponsors_url_with_two_members_does_not_pick_sponsor_slug(self):
        # Real pypi case: two unrelated packages both carry a wrong
        # `git` column pointing at github.com/sponsors/<user>. One of
        # them ALSO has its `github_repo` set to the matching nonsense
        # slug (sponsors/hynek). Without filtering /sponsors/ from the
        # url-derived slug, the group's github_repo would flip to the
        # nonsense slug because it 'agrees' with the URL.
        rows = [
            _pkg_row("attrs", "pypi", github_repo="python-attrs/attrs",
                     git_url="https://github.com/sponsors/hynek.git",
                     pagerank="2.0"),
            _pkg_row("service-identity", "pypi", github_repo="sponsors/hynek",
                     git_url="https://github.com/sponsors/hynek.git",
                     pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        # Tied 1-1; URL is filtered (sponsors/); alphabetic tiebreak.
        assert aggs[0]["repo"] == "python-attrs/attrs"

    def test_git_url_slug_is_authoritative_over_member_field(self):
        # The git URL (ecosyste.ms-sourced) wins over the github_repo field.
        # Real case: the `influxdb` package's github_repo field is wrong
        # (`simplejson/simplejson`) but its git URL correctly names
        # influxdb/influxdb-python.
        rows = [
            _pkg_row("influxdb", "pypi", github_repo="simplejson/simplejson",
                     git_url="https://github.com/influxdb/influxdb-python.git",
                     pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["repo"] == "influxdb/influxdb-python"

    def test_git_url_slug_beats_member_field_majority(self):
        # Even a unanimous github_repo field loses to a usable git URL slug.
        # (typeshed's stub packages name python/typeshed but their git URL
        # is the stub_uploader repo; the URL wins here — value-repo-
        # overrides.csv is what restores python/typeshed in production.)
        rows = [_pkg_row(f"types-{i}", "pypi", github_repo="python/typeshed",
                         git_url="https://github.com/typeshed-internal/stub_uploader.git",
                         pagerank="1.0")
                for i in range(5)]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert len(aggs) == 1
        assert aggs[0]["repo"] == "typeshed-internal/stub_uploader"

    def test_same_repo_id_merges_rename_twins(self):
        """Two packages with the same repo_id (but different git_urls / slugs)
        collapse into one group with combined PageRank — the stable numeric id
        unifies rename-twins natively without a separate canonicalisation map."""
        rows = [
            _pkg_row("jest-old", "npm", github_repo="facebook/jest",
                     git_url="https://github.com/facebook/jest.git",
                     repo_id="gh/10270250", pagerank="0.6"),
            _pkg_row("@jest/core", "npm", github_repo="jestjs/jest",
                     git_url="https://github.com/jestjs/jest.git",
                     repo_id="gh/10270250", pagerank="0.4"),
        ]
        aggs = aggregate_by_repo(rows)
        assert len(aggs) == 1                               # one repo, not two
        assert aggs[0]["repo_id"] == "gh/10270250"
        assert aggs[0]["packages"] == 2                     # both pkgs, PR combined
        assert aggs[0]["platform"] == "github"

    def test_blank_repo_id_splits_by_git_url(self):
        """Without repo_id, packages with different git_urls stay separate groups
        (documents that the merge depends on repo_id being resolved first)."""
        rows = [
            _pkg_row("jest-old", "npm", github_repo="facebook/jest",
                     git_url="https://github.com/facebook/jest.git",
                     repo_id="", pagerank="0.6"),
            _pkg_row("@jest/core", "npm", github_repo="jestjs/jest",
                     git_url="https://github.com/jestjs/jest.git",
                     repo_id="", pagerank="0.4"),
        ]
        aggs = aggregate_by_repo(rows)
        assert len(aggs) == 2

    def test_packages_count_matches_membership(self):
        rows = [_pkg_row(f"p{i}", "npm", github_repo="x/y", pagerank=str(i))
                for i in range(7)]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
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
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert aggs[0]["ecosystems"] == "pypi,crates,cpp"


# ── repo overrides (curated wrong-repo correction) ───────────────────────────

class TestRepoOverrides:
    """The curated `overrides.csv` layer forces the correct identity for
    packages whose upstream registry metadata names the wrong repo (e.g.
    `@sinclair/typebox`, whose latest npm version points at a placeholder
    repo). Applied as the last step of `aggregate_by_repo`, it must win over
    the registry-derived value. Each override is a dict
    `{"repo", "git_url", "valid"}`; the `valid` pin is surfaced by the
    loader but applied later by `build_validation`, not here.
    """

    def test_load_overrides_missing_file_returns_empty(self, tmp_path):
        assert load_repo_overrides(tmp_path / "absent.csv") == {}

    def test_load_overrides_parses_and_normalises(self, tmp_path):
        p = tmp_path / "overrides.csv"
        _write_csv(p, ["package", "ecosystem", "repo", "git_url", "valid", "reason"],
                   [["@sinclair/typebox", "npm", "SinclairZX81/TypeBox", "", "", "bad npm meta"]])
        idx = load_repo_overrides(p)
        # key is (package, ecosystem); slug is lowercased like the rest of the pipeline
        assert idx == {("@sinclair/typebox", "npm"):
                       {"repo": "sinclairzx81/typebox", "git_url": "", "valid": ""}}

    def test_load_overrides_skips_blank_reason(self, tmp_path):
        # A curated override MUST carry a reason; a blank-reason row is dropped.
        p = tmp_path / "overrides.csv"
        _write_csv(p, ["package", "ecosystem", "repo", "git_url", "valid", "reason"],
                   [["pkg", "npm", "owner/repo", "", "", ""]])
        assert load_repo_overrides(p) == {}

    def test_load_overrides_surfaces_git_url_and_valid_pin(self, tmp_path):
        # A git_url-only override and a valid-only pin both load (with a reason).
        p = tmp_path / "overrides.csv"
        _write_csv(p, ["package", "ecosystem", "repo", "git_url", "valid", "reason"],
                   [["pkg-a", "pypi", "", "https://Example.com/X.git", "", "non-gh upstream"],
                    ["pkg-b", "crates", "owner/repo", "", "True", "rescue false-negative"]])
        idx = load_repo_overrides(p)
        assert idx[("pkg-a", "pypi")] == {
            "repo": "", "git_url": "https://example.com/x.git", "valid": ""}
        assert idx[("pkg-b", "crates")] == {
            "repo": "owner/repo", "git_url": "", "valid": "True"}

    def test_override_forces_github_repo_over_registry_value(self):
        # A package whose registry-derived github_repo / git_url point at the
        # WRONG repo. With an explicit override list the call must rewrite
        # both to the curated repo, beating the registry-provided value.
        # (Uses a synthetic package not in the shipped overrides file so the
        #  "before" state is the unmodified registry value.)
        rows = [
            _pkg_row("some-pkg", "npm",
                     github_repo="evil/placeholder",
                     git_url="https://github.com/evil/placeholder.git",
                     pagerank="1.0", value_class="A"),
        ]
        overrides = {("some-pkg", "npm"):
                     {"repo": "real/upstream", "git_url": "", "valid": ""}}
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        # Without the override the aggregate carries the wrong repo …
        assert aggs[0]["repo"] == "evil/placeholder"
        # … applying the override forces the correct repo + consistent git_url.
        fixed = apply_repo_overrides(aggs, rows, overrides)
        assert fixed[0]["repo"] == "real/upstream"
        assert fixed[0]["platform"] == "github"
        assert fixed[0]["git_url"] == "https://github.com/real/upstream.git"

    def test_git_url_only_override_sets_url_and_rederives_identity(self):
        # A git_url-only override declares a non-GitHub canonical source: it
        # rewrites git_url AND re-derives (platform, repo) from that URL,
        # dropping the member-derived github identity so the new git_url
        # becomes the validation target. Here a GitLab URL → platform=gitlab
        # and the gitlab project path as repo.
        rows = [
            _pkg_row("some-pkg", "pypi",
                     github_repo="owner/repo",
                     git_url="https://github.com/owner/repo.git",
                     pagerank="1.0", value_class="A"),
        ]
        overrides = {("some-pkg", "pypi"):
                     {"repo": "", "git_url": "https://gitlab.com/gnome/glib.git", "valid": ""}}
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        fixed = apply_repo_overrides(aggs, rows, overrides)
        assert fixed[0]["git_url"] == "https://gitlab.com/gnome/glib.git"
        assert fixed[0]["platform"] == "gitlab"
        assert fixed[0]["repo"] == "gnome/glib"
        assert "github_repo" not in fixed[0]

    def test_git_url_only_override_clears_stale_repo_id(self):
        # Regression: git/gettext + libgpg-error. A package resolved against a
        # salsa packaging repo (gl/ repo_id stamped by the resolve step) is
        # then redirected by a git_url-only override to its real custom-host
        # upstream. The gl/ id belongs to the discarded salsa identity and
        # must be cleared — a `custom` platform row never carries a repo_id.
        rows = [
            _pkg_row("gettext", "cpp",
                     github_repo="",
                     git_url="https://salsa.debian.org/sanvila/gettext.git",
                     pagerank="1.0", value_class="A"),
        ]
        rows[0]["repo_id"] = "gl/debian-91954"
        overrides = {("gettext", "cpp"):
                     {"repo": "", "git_url": "https://git.savannah.gnu.org/git/gettext.git",
                      "valid": ""}}
        fixed = apply_repo_overrides(
            aggregate_by_repo(rows, drop_d_class=False), rows, overrides)
        assert fixed[0]["platform"] == "custom"
        assert fixed[0]["repo_id"] == ""

    def test_git_url_override_matching_member_upstream_keeps_repo_id(self):
        # A curated gitlab URL that IS the member upstream the id was resolved
        # from (e.g. tiff → gitlab.com/libtiff/libtiff, id gl/4720790) keeps
        # that id — the override confirms the identity, it doesn't replace it.
        rows = [
            _pkg_row("tiff", "cpp",
                     github_repo="",
                     git_url="https://gitlab.com/libtiff/libtiff.git",
                     pagerank="1.0", value_class="A"),
        ]
        rows[0]["repo_id"] = "gl/4720790"
        overrides = {("tiff", "cpp"):
                     {"repo": "", "git_url": "https://gitlab.com/libtiff/libtiff.git",
                      "valid": ""}}
        fixed = apply_repo_overrides(
            aggregate_by_repo(rows, drop_d_class=False), rows, overrides)
        assert fixed[0]["platform"] == "gitlab"
        assert fixed[0]["repo_id"] == "gl/4720790"

    def test_repo_override_clears_non_github_repo_id(self):
        # A repo-override forces (platform=github, slug); a member-derived gl/
        # id contradicts that platform and must not survive onto the row.
        rows = [
            _pkg_row("some-pkg", "cpp",
                     github_repo="",
                     git_url="https://salsa.debian.org/debian/some-pkg.git",
                     pagerank="1.0", value_class="A"),
        ]
        rows[0]["repo_id"] = "gl/debian-123"
        overrides = {("some-pkg", "cpp"):
                     {"repo": "real/upstream", "git_url": "", "valid": ""}}
        fixed = apply_repo_overrides(
            aggregate_by_repo(rows, drop_d_class=False), rows, overrides)
        assert fixed[0]["platform"] == "github"
        assert fixed[0]["repo_id"] == ""

    def test_override_is_keyed_per_ecosystem(self):
        # An override for npm must not touch a same-named pypi package.
        rows = [
            _pkg_row("typebox", "npm", github_repo="wrong/repo",
                     git_url="https://github.com/wrong/repo.git", pagerank="1.0"),
            _pkg_row("typebox", "pypi", github_repo="other/repo",
                     git_url="https://github.com/other/repo.git", pagerank="1.0"),
        ]
        overrides = {("typebox", "npm"):
                     {"repo": "right/repo", "git_url": "", "valid": ""}}
        aggs = apply_repo_overrides(
            aggregate_by_repo(rows, drop_d_class=False), rows, overrides)
        npm_agg = next(a for a in aggs if a["git_url"].endswith("right/repo.git"))
        assert npm_agg["repo"] == "right/repo"
        assert npm_agg["platform"] == "github"
        # the pypi typebox group is untouched
        pypi_agg = next(a for a in aggs if a["repo"] == "other/repo")
        assert pypi_agg["git_url"] == "https://github.com/other/repo.git"

    def test_no_overrides_is_a_noop(self):
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        before = aggs[0]["repo"]
        after = apply_repo_overrides(aggs, rows, {})
        assert after[0]["repo"] == before

    def test_override_applied_end_to_end_via_aggregate_by_repo(self, tmp_path):
        # Drives the real chokepoint: aggregate_by_repo calls apply_repo_overrides
        # internally, loading the curated CSV from OVERRIDES_FILE. Point that at
        # a temp file so the test is hermetic.
        import src.value.unify_value_data as mod
        ov = tmp_path / "overrides.csv"
        _write_csv(ov, ["package", "ecosystem", "repo", "git_url", "valid", "reason"],
                   [["@sinclair/typebox", "npm", "sinclairzx81/typebox", "", "", "wrong upstream"]])
        original = mod.OVERRIDES_FILE
        mod.OVERRIDES_FILE = ov
        try:
            rows = [
                _pkg_row("@sinclair/typebox", "npm",
                         github_repo="sinclairzx81/sinclair-typebox",
                         git_url="https://github.com/sinclairzx81/sinclair-typebox.git",
                         pagerank="1.0", value_class="A"),
            ]
            aggs = aggregate_by_repo(rows, drop_d_class=False)
        finally:
            mod.OVERRIDES_FILE = original
        assert aggs[0]["repo"] == "sinclairzx81/typebox"
        assert aggs[0]["platform"] == "github"
        assert aggs[0]["git_url"] == "https://github.com/sinclairzx81/typebox.git"

    def test_override_repo_plus_nongithub_git_url_sets_mirror_url(self):
        # An override with both `repo` (GitHub slug) AND a non-GitHub `git_url`
        # keeps the GitHub mirror as the canonical identity and stores the
        # upstream as `mirror_url` metadata (e.g. gcc-mirror/gcc → gcc.gnu.org,
        # torvalds/linux → git.kernel.org). This is distinct from the retired
        # archived-mirror exemption. Uses a package absent from the shipped
        # overrides.csv so aggregate_by_repo's internal override is a no-op and
        # the explicit override under test is the only one applied.
        rows = [
            _pkg_row("faux-mirror-pkg", "cpp", github_repo="acme/gcc-mirror",
                     git_url="https://github.com/acme/gcc-mirror.git",
                     repo_id="gh/12345", pagerank="1.0"),
        ]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        overrides = {("faux-mirror-pkg", "cpp"): {
            "repo": "acme/gcc-mirror",
            "git_url": "https://gcc.gnu.org/git/gcc.git",
            "valid": "",
        }}
        fixed = apply_repo_overrides(aggs, rows, overrides)
        assert fixed[0]["repo"] == "acme/gcc-mirror"
        assert fixed[0]["platform"] == "github"
        assert fixed[0]["git_url"] == "https://github.com/acme/gcc-mirror.git"
        assert fixed[0]["mirror_url"] == "https://gcc.gnu.org/git/gcc.git"

    def test_shipped_overrides_file_includes_typebox(self):
        # The committed override file must carry the typebox correction.
        idx = load_repo_overrides()
        assert idx[("@sinclair/typebox", "npm")]["repo"] == "sinclairzx81/typebox"


# ── _strip_internals direct test ─────────────────────────────────────────────

class TestStripInternals:
    def test_drops_scratch_keys(self):
        d = {
            "repo": "x/y", "platform": "github", "_pkgs_npm": 5,
            "_pr_sum_npm": 1.0, "_pr_pct_npm": 99.0, "_top_pkg_npm": "x",
            "group_key": "x/y", "class": "A",
        }
        clean = _strip_internals(d)
        assert clean == {"repo": "x/y", "platform": "github", "class": "A"}


# ── write_value_data ─────────────────────────────────────────────────────────

class TestWriteValueData:
    def test_round_trip(self, tmp_path):
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        out = tmp_path / "value-data.csv"
        write_value_data(aggs, path=out)
        with open(out, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            written = list(reader)
            assert reader.fieldnames == FIELDS
        assert len(written) == 1
        assert written[0]["repo"] == "x/y"
        assert written[0]["platform"] == "github"
        # Single-package universe → cum_share=100% → class C (see
        # test_single_package_single_ecosystem for the reasoning).
        assert written[0]["class"] == "C"
        assert written[0]["class_pypi"] == ""

    def test_creates_parent_directory(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "value-data.csv"
        assert not out.parent.exists()
        write_value_data([], path=out)
        assert out.exists()


# ── end-to-end via collect + aggregate + write ───────────────────────────────

class TestEndToEnd:
    @pytest.fixture(autouse=True)
    def _isolate_overrides(self, monkeypatch):
        """Isolate the end-to-end aggregation from the curated overrides.csv
        (the `glibc` fixture below would otherwise pick up a real override)."""
        monkeypatch.setattr(
            "src.value.unify_value_data.load_repo_overrides", lambda *a, **k: {}
        )

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

        aggs = aggregate_by_repo(all_rows, drop_d_class=False)
        assert len(aggs) == 3  # babel/babel, lodash/lodash, glibc orphan

        out = tmp_path / "value-data.csv"
        write_value_data(aggs, path=out)

        with open(out, encoding="utf-8") as f:
            written = list(csv.DictReader(f))

        # All written rows have exactly the canonical FIELDS, in order
        with open(out, encoding="utf-8") as f:
            assert csv.DictReader(f).fieldnames == FIELDS

        # Babel monorepo collapsed
        babel = next(r for r in written if r["repo"] == "babel/babel")
        assert babel["platform"] == "github"
        assert babel["packages"] == "2"
        assert babel["top_eco_pkg"] == "@babel/core"  # higher pagerank than parser
        assert babel["ecosystems"] == "npm"

        # Glibc's non-GitHub upstream keeps its git_url; identity is derived
        # from it (sourceware cgit → custom host, `git/glibc` path).
        glibc = next(r for r in written if r["top_eco_pkg"] == "glibc")
        assert glibc["platform"] == "custom"
        assert glibc["repo"] == "git/glibc"
        assert glibc["git_url"] == "https://sourceware.org/git/glibc.git"
        assert glibc["ecosystems"] == "cpp"


# ── invariants ───────────────────────────────────────────────────────────────

class TestInvariants:
    def test_class_rank_covers_abc_in_strict_order(self):
        assert CLASS_RANK == {"A": 0, "B": 1, "C": 2}

    def test_fields_contains_required_columns(self):
        for col in ("repo", "platform", "repo_id", "git_url", "git_valid",
                    "ecosystems", "packages",
                    "top_eco", "top_eco_pkg", "top_eco_pct", "class"):
            assert col in FIELDS
        for eco in ECOSYSTEMS:
            assert f"class_{eco}" in FIELDS
        # the old bare-slug column is gone — identity is now (repo, platform, repo_id)
        assert "github_repo" not in FIELDS
        assert "gh_repo_id" not in FIELDS
        # is_eol is intentionally NOT in FIELDS — owned by eligibility-data.csv
        assert "is_eol" not in FIELDS

    def test_ecosystems_tuple_canonical(self):
        assert ECOSYSTEMS == ("npm", "pypi", "crates", "cpp")

    def test_identity_columns_lead_fields(self):
        # The repo-identity triple leads value.csv in a fixed order:
        # repo, platform, repo_id — the slug, its host class, and the stable
        # numeric id sit together at the front of the row.
        assert FIELDS.index("repo") == 0
        assert FIELDS.index("platform") == 1
        assert FIELDS.index("repo_id") == 2
        assert FIELDS[3] == "git_url"

    def test_git_valid_column_present_and_legacy_columns_dropped(self):
        # The per-repo validity column is `git_valid`; the old bare `valid`
        # and the previous gh_valid pair are gone. llm_guess removed entirely.
        # Verdicts now live in validation.csv.
        # Column order: git_url → mirror_url → git_valid.
        assert "git_valid" in FIELDS
        assert "valid" not in FIELDS
        assert FIELDS[FIELDS.index("git_url") + 1] == "mirror_url"
        assert FIELDS[FIELDS.index("mirror_url") + 1] == "git_valid"
        for dropped in ("gh_valid", "llm_guess"):
            assert dropped not in FIELDS

    def test_aggregate_emits_empty_git_valid_placeholder(self):
        # unify leaves `git_valid` empty on every aggregate; build_validation fills it.
        rows = [_pkg_row("a", "npm", github_repo="x/y", pagerank="1.0")]
        aggs = aggregate_by_repo(rows, drop_d_class=False)
        assert aggs[0]["git_valid"] == ""


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


class TestPrScore:
    """pr_score: per-eco ln(PR mass) min-max → p2-norm across ecos → max=100."""

    def test_top_repo_is_100_and_order_follows_mass(self):
        rows = [
            _pkg_row("big", "npm", github_repo="o/big", pagerank="0.5"),
            _pkg_row("mid", "npm", github_repo="o/mid", pagerank="0.05"),
            _pkg_row("tiny", "npm", github_repo="o/tiny", pagerank="0.005"),
        ]
        aggs = {a["repo"]: a for a in aggregate_by_repo(rows)}
        assert aggs["o/big"]["pr_score"] == "100.00"
        # equal ln-spacing (×10 steps) → the middle repo lands halfway
        assert aggs["o/mid"]["pr_score"] == "50.00"
        assert aggs["o/tiny"]["pr_score"] == "0.00"   # the eco's min anchor

    def test_monorepo_sums_package_mass_before_log(self):
        rows = [
            _pkg_row("a1", "npm", github_repo="o/mono", pagerank="0.3"),
            _pkg_row("a2", "npm", github_repo="o/mono", pagerank="0.2"),
            _pkg_row("solo", "npm", github_repo="o/solo", pagerank="0.5"),
            _pkg_row("small", "npm", github_repo="o/small", pagerank="0.005"),
        ]
        aggs = {a["repo"]: a for a in aggregate_by_repo(rows)}
        # mono's mass (0.3+0.2) equals solo's single package → same score
        assert aggs["o/mono"]["pr_score"] == aggs["o/solo"]["pr_score"] == "100.00"

    def test_second_ecosystem_adds_with_p2_diminishing_returns(self):
        rows = [
            # champion of npm only
            _pkg_row("n", "npm", github_repo="o/npm-only", pagerank="0.9"),
            _pkg_row("nmin", "npm", github_repo="o/nmin", pagerank="0.001"),
            # champion of BOTH ecosystems → p2-norm = √2 of a single 1.0
            _pkg_row("b1", "npm", github_repo="o/both", pagerank="0.9"),
            _pkg_row("b2", "pypi", github_repo="o/both", pagerank="0.9"),
            _pkg_row("pmin", "pypi", github_repo="o/pmin", pagerank="0.001"),
        ]
        aggs = {a["repo"]: a for a in aggregate_by_repo(rows)}
        assert aggs["o/both"]["pr_score"] == "100.00"
        # single-eco champion = 100/√2 ≈ 70.71 — breadth rewarded, boundedly
        assert aggs["o/npm-only"]["pr_score"] == "70.71"

    def test_no_pagerank_signal_leaves_blank(self):
        rows = [
            _pkg_row("ranked", "npm", github_repo="o/ranked", pagerank="0.5"),
            _pkg_row("lesser", "npm", github_repo="o/lesser", pagerank="0.01"),
            _pkg_row("orphan", "cpp", github_repo="", git_url="", pagerank="0.0"),
        ]
        aggs = aggregate_by_repo(rows)
        blanks = [a for a in aggs if a["pr_score"] == ""]
        assert len(blanks) == 1                      # the zero-PR orphan
        assert blanks[0]["packages"] == 1
        ranked = next(a for a in aggs if a["repo"] == "o/ranked")
        assert ranked["pr_score"] == "100.00"
