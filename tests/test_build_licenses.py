"""Tests for src/eligibility/build_licenses.py — SPDX → OSS classification."""

from __future__ import annotations

import csv

import pytest

from src.eligibility import build_licenses as bl

# A tiny approved set for expression-handling tests — the real set
# (data/sources/osi/oss-licenses.csv) is only used in the policy tests below.
APPROVED = {"mit", "apache-2.0", "gpl-3.0", "curl"}


class TestClassifyOss:
    def test_known_osi_license(self):
        assert bl.classify_oss("mit", APPROVED) is True
        assert bl.classify_oss("Apache-2.0", APPROVED) is True  # case-insensitive

    def test_unknown_returns_none(self):
        for s in ("", "noassertion", "other", "none"):
            assert bl.classify_oss(s, APPROVED) is None, s

    def test_proprietary_returns_false(self):
        assert bl.classify_oss("proprietary-acme", APPROVED) is False

    def test_spdx_or_expression_any_match_wins(self):
        assert bl.classify_oss("mit or apache-2.0", APPROVED) is True
        # OR with one approved + one proprietary → True (pick the approved).
        assert bl.classify_oss("proprietary-acme or mit", APPROVED) is True

    def test_crates_slash_form(self):
        assert bl.classify_oss("mit/apache-2.0", APPROVED) is True

    def test_spdx_suffix_stripped(self):
        assert bl.classify_oss("gpl-3.0-or-later", APPROVED) is True
        assert bl.classify_oss("apache-2.0+", APPROVED) is True

    def test_with_exception_clause(self):
        assert bl.classify_oss(
            "apache-2.0 with llvm-exception", APPROVED) is True


def _read_real_approved() -> set[str]:
    """The on-disk OSS set, read directly (no ensure() → no network)."""
    if not bl.OSI_FILE.exists():
        pytest.skip("data/sources/osi/oss-licenses.csv not present")
    with open(bl.OSI_FILE, encoding="utf-8") as f:
        return {(r.get("spdx_id") or "").strip().lower()
                for r in csv.DictReader(f)} - {""}


class TestOssPolicy:
    """The repo's OSS policy — OSI-approved ∪ curated extras, content
    licenses excluded — as encoded in data/sources/osi/oss-licenses.csv."""

    def test_extras_are_oss(self):
        approved = _read_real_approved()
        for spdx in ("curl", "ftl", "libpng-2.0", "mit-cmu",
                     "psf-2.0", "blessing",
                     # ncurses' X11 permissive variant — genuinely OSS but
                     # outside OSI's formal list (added to curated extras).
                     "x11-distribute-modifications-variant"):
            assert bl.classify_oss(spdx, approved) is True, spdx

    def test_content_licenses_are_not_oss(self):
        # Strict policy: FSF-libre content licenses are NOT software OSS —
        # CC-BY / CC0 are excluded so data-only repos don't count as eligible.
        approved = _read_real_approved()
        assert bl.classify_oss("cc-by-4.0", approved) is False
        assert bl.classify_oss("cc0-1.0", approved) is False

    def test_deprecated_gpl_ids_still_oss(self):
        # GitHub's API returns the deprecated SPDX forms (gpl-2.0, lgpl-2.1);
        # they are kept in the approved set so the plain join works.
        approved = _read_real_approved()
        assert bl.classify_oss("gpl-2.0", approved) is True
        assert bl.classify_oss("lgpl-2.1", approved) is True


class TestLicenseOverrides:
    """The manual `license` assertion in eligibility/overrides.csv — the
    highest-priority source, fixing repos whose LICENSE detection fails
    upstream (GitHub Licensee `noassertion` on bundled/dual/stacked files)."""

    def test_load_maps_repo_id_to_lowercased_license(self, tmp_path):
        p = tmp_path / "overrides.csv"
        p.write_text(
            "repo,repo_id,host,host_type,gh_user,owner,owner_type,oc_slug,license,eol,reason\n"
            "nodejs/node,27193779,,,nodejs,,,,MIT,,noassertion fix\n"
            "no-id/skipme,,,,,,,,,mit,,\n"          # no repo_id → skipped
            "no-lic/skipme,999,,,,,,,,,\n"          # no license → skipped
        )
        out = bl.load_license_overrides(p)
        assert out == {"27193779": "mit"}           # lowercased, id-keyed

    def test_load_missing_file_is_empty(self, tmp_path):
        assert bl.load_license_overrides(tmp_path / "nope.csv") == {}

    def test_asserted_license_classifies_oss(self):
        # The seven assertions we ship must all classify as OSS.
        approved = _read_real_approved()
        for spdx in ("mit", "python-2.0", "gpl-2.0-only", "unicode-3.0",
                     "bsd-3-clause", "lgpl-2.1-only or mpl-1.1"):
            assert bl.classify_oss(spdx, approved) is True, spdx

    def test_real_overrides_file_has_node_assertion(self):
        """The shipped overrides.csv pins node's license (regression guard
        against the `noassertion` false-negative that excluded it)."""
        if not bl.OVERRIDES_FILE.exists():
            pytest.skip("overrides.csv not present")
        out = bl.load_license_overrides()
        assert out.get("gh/27193779") == "mit"       # nodejs/node (namespaced repo_id)


class TestSourcePrecedence:
    def _build(self, monkeypatch, *, registry=None, github=None, gitlab=None,
               overrides=None):
        from dataclasses import dataclass

        import src.eligibility.build_licenses as bl

        @dataclass
        class E:
            repo: str
            repo_id: str

        monkeypatch.setattr(bl, "load_top_repos",
                            lambda **kw: [E("gnome/glib", "gl/gnome-658")])
        monkeypatch.setattr(bl, "load_oss_approved", lambda: {"lgpl-2.1", "mit"})
        monkeypatch.setattr(bl, "load_license_overrides", lambda: overrides or {})
        monkeypatch.setattr(bl, "load_registry_licenses", lambda: registry or {})
        monkeypatch.setattr(bl, "load_github_licenses", lambda: github or {})
        monkeypatch.setattr(bl, "load_gitlab_licenses", lambda: gitlab or {})
        return bl.build()[0]

    def test_registry_noassertion_does_not_shadow_gitlab_detection(self, monkeypatch):
        # Regression: glib — the cpp registry row says `noassertion` while the
        # GitLab API detects lgpl-2.1. The junk sentinel must not claim the slot.
        row = self._build(monkeypatch,
                          registry={"gnome/glib": "noassertion"},
                          gitlab={"gl/gnome-658": "lgpl-2.1"})
        assert (row["license"], row["license_source"]) == ("lgpl-2.1", "gitlab")
        assert row["oss"] is True

    def test_meaningful_registry_still_wins_over_gitlab(self, monkeypatch):
        row = self._build(monkeypatch,
                          registry={"gnome/glib": "mit"},
                          gitlab={"gl/gnome-658": "lgpl-2.1"})
        assert (row["license"], row["license_source"]) == ("mit", "registry")

    def test_all_sources_junk_keeps_first_sentinel_traceable(self, monkeypatch):
        row = self._build(monkeypatch,
                          registry={"gnome/glib": "noassertion"},
                          gitlab={"gl/gnome-658": "other"})
        assert (row["license"], row["license_source"]) == ("noassertion", "registry")
        assert row["oss"] == ""   # unknown, not False
