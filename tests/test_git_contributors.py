"""Tests for src/sources/git/contributors.py — identity merging, bot detection, BF/HHI."""

import subprocess
from unittest.mock import patch

from src.sources.git.contributors import (
    _canon_email,
    _derive_login,
    _is_bot_identity,
    log_commits,
    merge_identities,
    metrics,
)


class TestCanonEmail:
    def test_lowercased(self):
        assert _canon_email("Foo@Bar.COM") == "foo@bar.com"

    def test_github_noreply_numbered_and_plain_collapse(self):
        # The numeric account-id prefix is stripped, so both forms of one
        # person's no-reply address normalise to the same key.
        assert _canon_email("12345+jane@users.noreply.github.com") == "jane@noreply"
        assert _canon_email("jane@users.noreply.github.com") == "jane@noreply"


class TestDeriveLogin:
    def test_prefers_noreply_login(self):
        assert _derive_login("Jane Doe", "99+jane@users.noreply.github.com") == "jane"

    def test_falls_back_to_email_local_part(self):
        assert _derive_login("Jane Doe", "jane.doe@example.com") == "jane.doe"


class TestIsBotIdentity:
    def test_bot_suffix_name(self):
        assert _is_bot_identity(["dependabot[bot]"], ["x@y.com"]) is True

    def test_known_bot_via_noreply_login(self):
        assert _is_bot_identity(
            ["GitHub Actions"],
            ["41898282+github-actions[bot]@users.noreply.github.com"],
        ) is True

    def test_bot_email(self):
        assert _is_bot_identity(["GitHub"], ["noreply@github.com"]) is True

    def test_plain_human_not_bot(self):
        assert _is_bot_identity(["Daniel Stenberg"], ["daniel@haxx.se"]) is False


class TestMergeIdentities:
    def test_same_email_different_names_merge(self):
        counts = {
            ("Jane Doe", "jane@example.com"): 10,
            ("jane", "jane@example.com"): 5,
        }
        authors = merge_identities(counts)
        assert len(authors) == 1
        assert authors[0].commits == 15
        assert authors[0].name == "Jane Doe"  # display = most-committing identity

    def test_same_full_name_different_emails_merge(self):
        counts = {
            ("Jane Doe", "jane@work.com"): 7,
            ("Jane Doe", "jane@home.com"): 3,
        }
        authors = merge_identities(counts)
        assert len(authors) == 1
        assert authors[0].commits == 10

    def test_distinct_single_token_handles_do_not_merge(self):
        # Single-token names are too collision-prone to merge on — only a
        # shared email or a full name (with a space) triggers a merge.
        counts = {
            ("ci", "ci@a.com"): 4,
            ("ci", "ci@b.com"): 6,
        }
        authors = merge_identities(counts)
        assert len(authors) == 2

    def test_numbered_and_plain_noreply_merge(self):
        counts = {
            ("Jane Doe", "100+jane@users.noreply.github.com"): 8,
            ("Jane Doe", "jane@users.noreply.github.com"): 2,
        }
        authors = merge_identities(counts)
        assert len(authors) == 1
        assert authors[0].commits == 10

    def test_empty(self):
        assert merge_identities({}) == []


class TestMetrics:
    @staticmethod
    def _rows(specs):
        """specs: list of (name, email, count) → flat (ts, name, email) rows."""
        rows = []
        for name, email, count in specs:
            rows.extend((1_600_000_000.0, name, email) for _ in range(count))
        return rows

    def test_dominant_author_gives_bus_factor_one(self):
        rows = self._rows([
            ("Alice Smith", "alice@x.com", 90),
            ("Bob Jones", "bob@x.com", 5),
            ("Carol Lee", "carol@x.com", 5),
        ])
        m = metrics(rows)
        assert m.bus_factor == 1
        assert m.active_contributors == 3

    def test_bots_excluded_from_metrics(self):
        rows = self._rows([
            ("Alice Smith", "alice@x.com", 50),
            ("Bob Jones", "bob@x.com", 50),
            ("dependabot[bot]", "dependabot@x.com", 999),
        ])
        m = metrics(rows)
        assert m.active_contributors == 2          # bot not counted as a contributor
        assert m.bots == 1
        assert m.commits == 100             # bot's 999 commits excluded
        # Two 50/50 humans → BF 1: the first already reaches the 50% threshold.
        assert m.bus_factor == 1

    def test_window_filters_by_author_date(self):
        # One 2019 commit, two 2024 commits (ts picked inside each year).
        ts_2019 = 1_550_000_000.0
        ts_2024 = 1_710_000_000.0
        rows = [
            (ts_2019, "Old Hand", "old@x.com"),
            (ts_2024, "New Dev", "new@x.com"),
            (ts_2024, "New Dev", "new@x.com"),
        ]
        windowed = metrics(rows, since=1_672_531_200.0, until=1_735_689_599.0)
        assert windowed.active_contributors == 1   # only "New Dev" falls in 2023-2025
        assert windowed.commits == 2


class TestLogCommits:
    def test_non_utf8_bytes_decode_leniently(self):
        # Long-lived repos (openssl) carry commits with latin-1 author bytes.
        # git log output must decode leniently — one bad byte must not crash
        # the whole repo. \xe9 is latin-1 'é', invalid as standalone UTF-8.
        raw = (b"1600000000\x1fJos\xe9\x1fjose@x.com\n"
               b"1600000001\x1fAlice\x1falice@x.com")
        fake = subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout=raw, stderr=b"")
        with patch("src.sources.git.contributors.subprocess.run", return_value=fake):
            rows = log_commits("/fake/clone")
        assert len(rows) == 2
        assert rows[0][0] == 1600000000.0 and rows[0][2] == "jose@x.com"
        assert rows[1] == (1600000001.0, "Alice", "alice@x.com")
