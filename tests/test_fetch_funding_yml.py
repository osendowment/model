"""Tests for src/sources/github/fetch_funding_yml.py.

The fetcher reads GitHub's resolved `repository.fundingLinks` (the "Sponsor
this project" sidebar widget) via GraphQL, rather than parsing a raw
`.github/FUNDING.yml`. These tests cover the pure transforms that turn a
GraphQL response into a funding-yml.csv row.
"""

import csv

import pytest

from src.common.funding_platforms import FUNDING_PLATFORMS
from src.sources.github import fetch_funding_yml
from src.sources.github.fetch_funding_yml import (
    build_query,
    build_row,
    handle_from_link,
    platform_key,
    rows_from_response,
)


class TestPlatformKey:
    def test_enum_lowercased_to_column(self):
        assert platform_key("LIBERAPAY") == "liberapay"
        assert platform_key("OPEN_COLLECTIVE") == "open_collective"
        assert platform_key("GITHUB") == "github"
        assert platform_key("BUY_ME_A_COFFEE") == "buy_me_a_coffee"

    def test_every_graphql_enum_value_maps_to_a_real_column(self):
        # The 13 values GitHub's FundingPlatform enum can return.
        enum = [
            "GITHUB", "PATREON", "OPEN_COLLECTIVE", "KO_FI", "TIDELIFT",
            "COMMUNITY_BRIDGE", "LIBERAPAY", "ISSUEHUNT", "LFX_CROWDFUNDING",
            "POLAR", "BUY_ME_A_COFFEE", "THANKS_DEV", "CUSTOM",
        ]
        for e in enum:
            assert platform_key(e) in FUNDING_PLATFORMS

    def test_unknown_enum_falls_back_to_custom(self):
        # Forward-compatible: a platform GitHub adds later lands under custom.
        assert platform_key("SOME_NEW_PLATFORM") == "custom"
        assert platform_key("") == "custom"


class TestHandleFromLink:
    def test_github_login(self):
        assert handle_from_link("github", "https://github.com/yyx990803") == "yyx990803"

    def test_open_collective_slug(self):
        assert handle_from_link("open_collective", "https://opencollective.com/vuejs") == "vuejs"

    def test_liberapay_preserves_case(self):
        # Non-github handles keep their original case (liberapay/Larhzu).
        assert handle_from_link("liberapay", "https://liberapay.com/Larhzu") == "Larhzu"

    def test_nested_path_takes_last_segment(self):
        assert handle_from_link("thanks_dev", "https://thanks.dev/u/gh/prettier") == "prettier"
        assert handle_from_link("tidelift", "https://tidelift.com/funding/github/npm/prettier") == "prettier"

    def test_custom_keeps_full_url(self):
        # custom is a free-form URL — preserve it whole for auditability.
        url = "https://example.com/donate?ref=gh"
        assert handle_from_link("custom", url) == url

    def test_trailing_slash(self):
        assert handle_from_link("github", "https://github.com/babel/") == "babel"

    def test_empty_url(self):
        assert handle_from_link("liberapay", "") == ""

    def test_bare_host_falls_back_to_url(self):
        assert handle_from_link("liberapay", "https://liberapay.com") == "https://liberapay.com"


class TestBuildRow:
    def test_xz_liberapay(self):
        row = build_row("tukaani-project/xz", [
            {"platform": "LIBERAPAY", "url": "https://liberapay.com/Larhzu"},
        ])
        assert row["has_funding_links"] == "True"
        assert row["funding_link_platforms"] == "liberapay"
        assert row["liberapay"] == "Larhzu"
        assert row["github"] == ""

    def test_github_logins_deduped_lowercased(self):
        row = build_row("o/r", [
            {"platform": "GITHUB", "url": "https://github.com/Alice"},
            {"platform": "GITHUB", "url": "https://github.com/alice"},
            {"platform": "GITHUB", "url": "https://github.com/Bob"},
        ])
        assert row["github"] == "alice,bob"

    def test_mixed_platforms(self):
        row = build_row("vuejs/core", [
            {"platform": "GITHUB", "url": "https://github.com/yyx990803"},
            {"platform": "OPEN_COLLECTIVE", "url": "https://opencollective.com/vuejs"},
        ])
        assert set(row["funding_link_platforms"].split(",")) == {"github", "open_collective"}
        assert row["github"] == "yyx990803"
        assert row["open_collective"] == "vuejs"

    def test_no_links(self):
        row = build_row("o/r", [])
        assert row["has_funding_links"] == "False"
        assert row["funding_link_platforms"] == ""
        for p in FUNDING_PLATFORMS:
            assert row[p] == ""

    def test_every_platform_column_present(self):
        row = build_row("o/r", [{"platform": "GITHUB", "url": "https://github.com/x"}])
        for p in FUNDING_PLATFORMS:
            assert p in row

    def test_has_funding_yml_flag(self):
        # file-existence flag is independent of resolved links
        assert build_row("o/r", [], has_yml=True)["has_funding_yml"] == "True"
        assert build_row("o/r", [], has_yml=False)["has_funding_yml"] == "False"
        assert build_row("o/r", [])["has_funding_yml"] == "False"  # default


class TestBuildQuery:
    def test_aliases_and_variables(self):
        query, variables = build_query(["a/b", "c/d"])
        assert "r0: repository(owner:$o0, name:$n0)" in query
        assert "r1: repository(owner:$o1, name:$n1)" in query
        assert "fundingLinks" in query
        assert variables == {"o0": "a", "n0": "b", "o1": "c", "n1": "d"}

    def test_includes_funding_yml_file_checks(self):
        query, _ = build_query(["a/b"])
        assert 'object(expression:"HEAD:.github/FUNDING.yml")' in query
        assert 'object(expression:"HEAD:FUNDING.yml")' in query
        assert 'object(expression:"HEAD:docs/FUNDING.yml")' in query
        assert 'g0: repository(owner:$o0, name:".github")' in query   # owner default


class TestRowsFromResponse:
    def test_normal(self):
        body = {"data": {
            "r0": {"fundingLinks": [{"platform": "LIBERAPAY", "url": "https://liberapay.com/Larhzu"}]},
            "r1": {"fundingLinks": []},
        }}
        rows = rows_from_response(["tukaani-project/xz", "o/r"], body)
        assert rows["tukaani-project/xz"]["liberapay"] == "Larhzu"
        assert rows["o/r"]["has_funding_links"] == "False"

    def test_not_found_recorded_as_no_funding(self):
        body = {
            "data": {"r0": {"fundingLinks": []}, "r1": None},
            "errors": [{"type": "NOT_FOUND", "path": ["r1"],
                        "message": "Could not resolve to a Repository"}],
        }
        rows = rows_from_response(["o/a", "gone/repo"], body)
        assert rows["o/a"]["has_funding_links"] == "False"
        # A repo GitHub says is gone is "checked, no funding" — written, not skipped.
        assert rows["gone/repo"]["has_funding_links"] == "False"

    def test_unknown_null_is_skipped(self):
        # A null alias with no NOT_FOUND error is a fetch failure, not "no funding"
        # — it must NOT be written as False (auditability).
        body = {
            "data": {"r0": {"fundingLinks": []}, "r1": None},
            "errors": [{"type": "SERVICE_UNAVAILABLE", "path": ["r1"]}],
        }
        rows = rows_from_response(["o/a", "o/b"], body)
        assert "o/a" in rows
        assert "o/b" not in rows

    def test_top_level_failure_raises(self):
        with pytest.raises(RuntimeError):
            rows_from_response(["o/a"], {"data": None, "errors": [{"message": "boom"}]})

    def test_funding_yml_file_sets_flag(self):
        # a FUNDING.yml file (repo root) but no resolved links → links False, yml True
        body = {"data": {"r0": {"fundingLinks": [], "y2": {"__typename": "Blob"}}, "g0": None}}
        rows = rows_from_response(["o/r"], body)
        assert rows["o/r"]["has_funding_links"] == "False"
        assert rows["o/r"]["has_funding_yml"] == "True"

    def test_owner_github_default_sets_yml(self):
        # repo has no file, but the owner's `.github` repo carries one → yml True
        body = {"data": {"r0": {"fundingLinks": []}, "g0": {"z2": {"__typename": "Blob"}}}}
        rows = rows_from_response(["o/r"], body)
        assert rows["o/r"]["has_funding_yml"] == "True"

    def test_no_funding_yml_file(self):
        body = {"data": {"r0": {"fundingLinks": []}, "g0": None}}
        rows = rows_from_response(["o/r"], body)
        assert rows["o/r"]["has_funding_yml"] == "False"


class TestLoadRepoIdMap:
    """_load_repo_id_map must resolve a GitHub-renamed repo under BOTH its
    stale looked-up slug (`repo`) and its current canonical slug (`full_name`),
    so the id-keyed downstream join keeps the FUNDING.yml signal across the
    post-rename window (the fetcher looks up by current canonical slug)."""

    def test_renamed_repo_resolves_under_both_slugs(self, tmp_path, monkeypatch):
        repos_csv = tmp_path / "repos.csv"
        with open(repos_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["repo", "full_name", "repo_id"])
            w.writeheader()
            # Renamed on GitHub: cached row keyed on old slug, now canonical `new/name`.
            w.writerow({"repo": "old/name", "full_name": "new/name", "repo_id": "gh/1"})

        monkeypatch.setattr(fetch_funding_yml, "GH_REPOS_FILE", repos_csv)

        m = fetch_funding_yml._load_repo_id_map()
        # Canonical slug (the one the fetcher actually looks up) — the rename bug.
        assert m["new/name"] == "gh/1"
        # Stale looked-up slug still resolves too.
        assert m["old/name"] == "gh/1"
