"""Tests for src/risk/build_concentration.py concentration_class."""

import pytest

from src.risk.build_concentration import concentration_class


@pytest.mark.parametrize("bus_factor, hhi, expected", [
    ("1", "8000", "A"),    # both at the A threshold
    ("0", "10000", "A"),   # maximally concentrated
    ("1", "7999", "B"),    # bf ok for A, HHI just below → B
    ("2", "5000", "B"),    # both at the B threshold
    ("2", "4999", "C"),    # bf ok for B, HHI just below → C
    ("4", "2500", "C"),    # both at the C threshold
    ("2", "2578", "C"),    # isaacs/wrappy
    ("4", "2499", "D"),    # HHI just below C → D
    ("12", "332", "D"),    # scipy/scipy — many contributors
    # Joint test: an extreme HHI alone does NOT qualify when the bus factor
    # is healthy — concentration risk needs both signals.
    ("5", "9000", "D"),
])
def test_concentration_class_boundaries(bus_factor, hhi, expected):
    assert concentration_class(bus_factor, hhi) == expected


def test_concentration_class_unknown_is_blank():
    # Either input missing / unparseable → can't classify.
    assert concentration_class("", "8000") == ""
    assert concentration_class("1", "") == ""
    assert concentration_class("  ", "  ") == ""
    assert concentration_class("n/a", "8000") == ""
    assert concentration_class(None, None) == ""
