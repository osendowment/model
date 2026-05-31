"""Tests for the risk-data aggregator — column join + dedup."""

from collections import Counter

from src.pipeline.risk.aggregate_risk import aggregate, _qualify_columns


class TestRiskFundingWhitelist:
    def test_funding_carries_only_headline_metrics_no_fetched_at(self):
        # funding.csv has no fetched_at column, but even if it did the
        # whitelist would drop it — funding_fetched_at must not reach risk.csv.
        cols = ["repo", "repo_id", "gh_sponsors_in", "gh_sponsorships",
                "gh_sponsorships_p", "oc_avg_funding", "oc_avg_funding_p",
                "funding_p", "foundation_host", "fetched_at"]
        out = [out_col for _, out_col in _qualify_columns("funding", cols)]
        assert set(out) == {"gh_sponsorships", "oc_avg_funding", "funding_p"}
        assert "funding_fetched_at" not in out

    def test_other_dims_carry_everything_including_fetched_at(self):
        out = [out_col for _, out_col in
               _qualify_columns("concentration", ["repo", "anything_else", "fetched_at"])]
        assert "anything_else" in out                  # whitelist only applies to funding
        assert "concentration_fetched_at" in out       # other dims keep their freshness


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
