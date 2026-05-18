"""Tests for contributors module — _compute_bus_factor + bot filtering."""

import pytest

from src.github.fetch_contributors_metrics import _compute_bus_factor
from src.github.models import Contributor, is_bot


class TestComputeBusFactor:
    def test_bot_filtering(self):
        contribs = [
            Contributor(login="alice", commits=100, lines_changed=1000),
            Contributor(login="dependabot[bot]", commits=50, lines_changed=500),
        ]
        bf, sorted_c, hhi = _compute_bus_factor(contribs)
        assert bf == 1
        bot = next(c for c in sorted_c if c.login == "dependabot[bot]")
        assert bot.is_bot is True

    def test_include_bots_flag(self):
        contribs = [
            Contributor(login="alice", commits=40, lines_changed=400),
            Contributor(login="dependabot[bot]", commits=40, lines_changed=400),
            Contributor(login="bob", commits=20, lines_changed=200),
        ]
        bf, sorted_c, _ = _compute_bus_factor(contribs, include_bots=True)
        assert bf == 2  # alice + dependabot both count as humans
        assert all(not c.is_bot for c in sorted_c)

    def test_hhi_calculation(self):
        contribs = [
            Contributor(login="alice", commits=90, lines_changed=900),
            Contributor(login="bob", commits=10, lines_changed=100),
        ]
        _, _, hhi = _compute_bus_factor(contribs)
        assert hhi == pytest.approx(0.82)

    def test_locs_base(self):
        contribs = [
            Contributor(login="alice", commits=5, lines_changed=1000, lines_added=800, lines_deleted=200),
            Contributor(login="bob", commits=50, lines_changed=100, lines_added=80, lines_deleted=20),
        ]
        bf, sorted_c, _ = _compute_bus_factor(contribs, base="locs")
        assert bf == 1
        assert sorted_c[0].login == "alice"  # more lines changed


class TestIsBotDetection:
    def test_known_bots(self):
        assert is_bot("dependabot[bot]") is True
        assert is_bot("renovate[bot]") is True
        assert is_bot("greenkeeper[bot]") is True

    def test_bot_suffix(self):
        assert is_bot("some-ci[bot]") is True

    def test_human(self):
        assert is_bot("alice") is False
        assert is_bot("bob-dev") is False

    def test_case_insensitive(self):
        assert is_bot("Dependabot[bot]") is True
