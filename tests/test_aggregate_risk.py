"""Tests for the risk-data aggregator — column join + dedup."""

from collections import Counter

from src.pipeline.risk.aggregate_risk import aggregate, _qualify_columns


class TestRiskExclusions:
    def test_sponsoring_count_excluded_from_funding(self):
        cols = ["repo", "repo_id", "github_sponsors", "sponsoring_count", "fetched_at"]
        out = [out_col for _, out_col in _qualify_columns("funding", cols)]
        assert "github_sponsors" in out       # inbound stays
        assert "sponsoring_count" not in out   # outbound excluded from risk.csv
        assert "funding_fetched_at" in out

    def test_no_exclusions_for_other_dims(self):
        out = [out_col for _, out_col in _qualify_columns("concentration", ["repo", "sponsoring_count"])]
        assert "sponsoring_count" in out  # only excluded for the funding dim


class TestAggregateColumns:
    def test_no_duplicate_columns(self):
        """risk-data.csv must never have a repeated column.

        Regression: `active_contributors` lives in both concentration.csv
        and workload.csv. Before dedup it landed twice in the fieldnames,
        producing a corrupt DictWriter header.
        """
        fieldnames, _rows = aggregate()
        dupes = [c for c, n in Counter(fieldnames).items() if n > 1]
        assert dupes == [], f"duplicate columns: {dupes}"

    def test_shared_column_emitted_once(self):
        """The active-contributors count lives in both concentration.csv and
        workload.csv; the aggregator must emit the shared column exactly once.
        """
        fieldnames, _rows = aggregate()
        assert fieldnames.count("active_contributors_git_2021_2025") == 1

    def test_rows_have_no_extra_keys(self):
        """Every row dict's keys are a subset of the declared fieldnames."""
        fieldnames, rows = aggregate()
        field_set = set(fieldnames)
        for row in rows[:50]:
            assert set(row).issubset(field_set)
