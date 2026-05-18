"""Tests for src/pipeline/eligibility.py — focused on EOL exclusion and
license classification, the two pieces of business logic that decide
whether a repo lands in `eligibility-data.csv` as eligible.

The module reads from many CSV/JSON files at import time; tests stub
those reads via monkeypatch rather than materialising a fake DATA_DIR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline.eligibility import classify_eligibility as eligibility


# ── classify_oss ────────────────────────────────────────────────────────────

class TestClassifyOss:
    def test_known_osi_license(self):
        assert eligibility.classify_oss("mit") is True
        assert eligibility.classify_oss("apache-2.0") is True

    def test_unknown_returns_none(self):
        assert eligibility.classify_oss("") is None
        assert eligibility.classify_oss("noassertion") is None
        assert eligibility.classify_oss("other") is None
        assert eligibility.classify_oss("none") is None

    def test_proprietary_returns_false(self):
        # A made-up proprietary identifier is firmly non-OSS.
        assert eligibility.classify_oss("proprietary-acme") is False

    def test_curl_license_is_oss(self):
        # Regression: SPDX flags `curl` as not-OSI and not-FSF-libre, but
        # curl is the canonical OSS critical-infrastructure project.
        # Maintained in `fetch_licenses.EXTRAS`.
        assert eligibility.classify_oss("curl") is True

    def test_extras_are_oss(self):
        # The full extras allowlist that lives outside OSI's official set.
        for spdx in ("curl", "ftl", "libpng-2.0", "mit-cmu",
                     "psf-2.0", "blessing"):
            assert eligibility.classify_oss(spdx) is True, spdx

    def test_content_licenses_are_not_oss(self):
        # Strict policy: FSF-libre content licenses are NOT software OSS.
        # CC-BY / CC0 / GFDL are intentionally excluded so data-only
        # repos (e.g. mdn/data, caniuse-lite) don't count as eligible.
        assert eligibility.classify_oss("cc-by-4.0") is False
        assert eligibility.classify_oss("cc-by-3.0") is False
        assert eligibility.classify_oss("cc0-1.0") is False

    def test_spdx_or_expression_any_match_wins(self):
        assert eligibility.classify_oss("mit or apache-2.0") is True
        # OR with one approved + one proprietary → True (pick the approved).
        assert eligibility.classify_oss("proprietary-acme or mit") is True

    def test_spdx_suffix_stripped(self):
        assert eligibility.classify_oss("gpl-3.0-or-later") is True
        assert eligibility.classify_oss("apache-2.0+") is True

    def test_with_exception_clause(self):
        # `apache-2.0 with classpath-exception-2.0` → strips the exception
        # clause and hits apache-2.0.
        assert eligibility.classify_oss(
            "apache-2.0 with classpath-exception-2.0"
        ) is True


# ── build_eligibility (regression: EOL must exclude) ────────────────────────

class TestEolExclusion:
    """Regression for: `load_repo_eol_index` was loaded but never read.

    A repo with `is_eol=True` and an OSS license previously came out as
    `eligibility=True`. After the fix, `is_eol=True` always overrides.
    """

    @pytest.fixture
    def stub_loaders(self, monkeypatch):
        """Stub every disk-reading dep of `build_eligibility` with empty
        fixtures so the test only exercises the eligibility logic itself."""
        monkeypatch.setattr(eligibility, "load_repo_host_index", lambda: {})
        monkeypatch.setattr(eligibility, "load_user_meta", lambda: {})
        monkeypatch.setattr(eligibility, "load_trademarks", lambda: ({}, {}))
        # Force the GH_REPOS_FILE existence check inside build_eligibility
        # to skip — the repo metadata block then becomes a no-op.
        monkeypatch.setattr(eligibility, "GH_REPOS_FILE", Path("/nonexistent"))
        return monkeypatch

    def _set_inputs(self, monkeypatch, *, value_rows, eol_index, registry_licenses):
        monkeypatch.setattr(eligibility, "load_eligible_value_rows",
                            lambda: value_rows)
        monkeypatch.setattr(eligibility, "load_repo_eol_index",
                            lambda: eol_index)
        monkeypatch.setattr(eligibility, "load_repo_registry_license",
                            lambda: registry_licenses)

    def test_eol_repo_with_oss_license_is_not_eligible(self, stub_loaders):
        self._set_inputs(stub_loaders,
            value_rows=[
                {"github_repo": "alive/proj", "class": "A", "gh_valid": "True"},
                {"github_repo": "dead/proj",  "class": "A", "gh_valid": "True"},
            ],
            eol_index={"alive/proj": False, "dead/proj": True},
            registry_licenses={"alive/proj": "mit", "dead/proj": "mit"},
        )
        rows = eligibility.build_eligibility()
        by_repo = {r["repo"]: r for r in rows}

        # Both have the same OSS license, but only the alive one is eligible.
        assert by_repo["alive/proj"]["is_eol"] is False
        assert by_repo["alive/proj"]["eligibility"] is True
        assert by_repo["dead/proj"]["is_eol"] is True
        assert by_repo["dead/proj"]["eligibility"] is False

    def test_repo_without_eol_signal_treated_as_alive(self, stub_loaders):
        # A repo absent from the per-eco EOL index gets is_eol="" and is
        # eligible as long as the license is OSS.
        self._set_inputs(stub_loaders,
            value_rows=[
                {"github_repo": "random/proj", "class": "B", "gh_valid": "True"},
            ],
            eol_index={},
            registry_licenses={"random/proj": "mit"},
        )
        rows = eligibility.build_eligibility()
        assert len(rows) == 1
        assert rows[0]["is_eol"] == ""
        assert rows[0]["eligibility"] is True

    def test_non_oss_license_overrides_eol_alive(self, stub_loaders):
        # is_eol=False, but license is non-OSS → not eligible.
        # Using a made-up SPDX id since the OSS set now spans
        # OSI ∪ FSF-libre ∪ EXTRAS — easiest to avoid every real OSS id.
        self._set_inputs(stub_loaders,
            value_rows=[
                {"github_repo": "alive/proprietary", "class": "A", "gh_valid": "True"},
            ],
            eol_index={"alive/proprietary": False},
            registry_licenses={"alive/proprietary": "proprietary-acme"},
        )
        rows = eligibility.build_eligibility()
        assert rows[0]["is_oss"] is False
        assert rows[0]["eligibility"] is False
