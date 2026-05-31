"""Tests for the risk-data aggregator — column join + dedup."""

from collections import Counter

from src.risk.aggregate_risk import aggregate


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
