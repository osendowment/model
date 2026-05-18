"""Tests for contributors module — _compute_bus_factor + bot filtering."""

import pytest

from src.github.fetch_contributors_metrics import (
    _compute_bus_factor,
    compute_lifetime_metrics,
)
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

    def test_hhi_never_below_mathematical_floor(self):
        """HHI for n equal contributors is exactly 1/n — never below.

        Regression guard: the old `total_override` path used the lifetime
        /commits count (bots included) as the denominator, which pushed
        HHI under its 1/n floor and inflated the bus factor.
        """
        contribs = [Contributor(login=f"dev{i}", commits=10, lines_changed=10)
                    for i in range(7)]
        bf, _sorted, hhi = _compute_bus_factor(contribs)
        assert hhi == pytest.approx(1 / 7)  # not a deflated ~0.0017
        assert bf == 4  # 4 of 7 equal contributors clear the 50% threshold


class TestComputeLifetimeMetrics:
    def test_total_commits_does_not_distort_bf_hhi(self):
        """A large lifetime total_commits must not change BF/HHI.

        Regression for the denominator bug: BF/HHI are computed from the
        visible non-bot contributors' own commit shares. Passing the
        repo's bot-inflated lifetime commit count (here 1000, vs 100
        visible) previously deflated HHI ~12x and inflated BF.
        """
        data = [
            {"login": "alice", "contributions": 90, "type": "User"},
            {"login": "bob", "contributions": 10, "type": "User"},
        ]
        results = compute_lifetime_metrics(data, total_commits=1000,
                                           total_contributors=42)
        _label, rr = results[0]
        assert rr.hhi == pytest.approx(0.82)  # 0.9^2 + 0.1^2
        assert rr.bus_factor == 1             # alice alone clears 50%
        assert rr.total_commits == 1000       # still recorded, just unused
        assert rr.total_contributors == 42

    def test_bot_commits_excluded_from_denominator(self):
        """Bot contributions are excluded from the BF/HHI denominator."""
        data = [
            {"login": "alice", "contributions": 90, "type": "User"},
            {"login": "bob", "contributions": 10, "type": "User"},
            {"login": "dependabot[bot]", "contributions": 900, "type": "Bot"},
        ]
        _label, rr = compute_lifetime_metrics(data, total_commits=1000)[0]
        assert rr.hhi == pytest.approx(0.82)  # bot's 900 commits not in denom
        assert rr.bus_factor == 1


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
