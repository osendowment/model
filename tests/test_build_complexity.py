"""Tests for src/pipeline/risk/build_complexity.py classification helpers."""

import pytest

from src.pipeline.risk.build_complexity import _worst_class, loc_class


@pytest.mark.parametrize("loc, expected", [
    ("1000000", "A"),   # lower edge of A (≥ 1M)
    ("3648106", "A"),   # rust-lang/rust
    ("999999", "B"),    # just below A
    ("100000", "B"),    # lower edge of B (≥ 100k)
    ("99999", "C"),     # just below B
    ("10000", "C"),     # lower edge of C (≥ 10k)
    ("9999", "D"),      # just below C
    ("80", "D"),        # isaacs/wrappy
    ("0", "D"),
])
def test_loc_class_boundaries(loc, expected):
    assert loc_class(loc) == expected


def test_loc_class_unknown_is_blank():
    # Missing / unparseable LOC can't be classified.
    assert loc_class("") == ""
    assert loc_class("   ") == ""
    assert loc_class("n/a") == ""
    assert loc_class(None) == ""


def test_worst_class_picks_highest_risk():
    # A is the worst; complexity_class = worst of (loc_class, cc_class).
    assert _worst_class("A", "D") == "A"
    assert _worst_class("D", "A") == "A"
    assert _worst_class("C", "B") == "B"
    assert _worst_class("D", "D") == "D"


def test_worst_class_ignores_empty():
    # An empty sub-class is skipped — a repo with only one signal still
    # gets a complexity_class from it.
    assert _worst_class("B", "") == "B"
    assert _worst_class("", "C") == "C"
    assert _worst_class("", "") == ""
