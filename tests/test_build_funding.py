"""Tests for src/pipeline/risk/build_funding.py funding_class."""

import pytest

from src.pipeline.risk.build_funding import funding_class


@pytest.mark.parametrize("sponsors, expected", [
    ("0", "A"),       # 0 sponsors
    ("1", "B"),       # B = 1-10
    ("10", "B"),      # upper edge of B
    ("11", "C"),      # lower edge of C
    ("100", "C"),     # upper edge of C
    ("101", "D"),     # lower edge of D
    ("271", "D"),     # observed max
])
def test_funding_class_boundaries(sponsors, expected):
    assert funding_class(sponsors) == expected


def test_funding_class_unknown_is_blank():
    # Missing / unparseable sponsor count can't be classified.
    assert funding_class("") == ""
    assert funding_class("   ") == ""
    assert funding_class("n/a") == ""
    assert funding_class(None) == ""
