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
                     "psf-2.0", "blessing"):
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
