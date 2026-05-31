"""Tests for src/pipeline/risk/build_funding.py — join, channels_count, oc_avg_funding."""

from src.pipeline.risk import build_funding as bf


def test_assemble_row_joins_and_counts_channels():
    row = bf.assemble_row(
        repo="vuejs/core", repo_id="11730342",
        sponsors={"github_sponsors": "12", "sponsoring_count": "39",
                  "fetched_at": "2026-05-19T10:00:00+00:00"},
        yml={"has_funding_yml": "True", "funding_yml_platforms": "github,open_collective",
             "open_collective": "vuejs", "fetched_at": "2026-05-19T11:00:00+00:00"},
        export={"channel_platforms": "github,open_collective,bank", "open_collective": "vuejs"},
        foundation_host="",
        oc_budgets={"vuejs": {"raised_2021": "100", "raised_2022": "200",
                              "raised_2023": "", "raised_2024": "300", "raised_2025": ""}},
    )
    assert row["has_funding_json"] == "True"
    assert row["github_sponsors"] == "12"      # inbound
    assert row["sponsoring_count"] == "39"     # outbound (carried through)
    # union of {github, open_collective} and {github, open_collective, bank} = 3
    assert row["channels_count"] == "3"
    # mean of 100, 200, 300 (years with data) = 200
    assert row["oc_avg_funding"] == "200"
    assert row["fetched_at"] == "2026-05-19T11:00:00+00:00"
    assert "funding_class" not in row


def test_assemble_row_no_funding_sources():
    row = bf.assemble_row(
        repo="acornjs/acorn", repo_id="1", sponsors={}, yml={}, export={},
        foundation_host="apache", oc_budgets={})
    assert row["has_funding_json"] == "False"
    assert row["channels_count"] == "0"
    assert row["oc_avg_funding"] == ""
    assert row["foundation_host"] == "apache"


def test_oc_avg_funding_missing_slug_or_data():
    assert bf.oc_avg_funding("", {}) == ""
    assert bf.oc_avg_funding("ghost", {"vuejs": {"raised_2024": "5"}}) == ""
    assert bf.oc_avg_funding("x", {"x": {"raised_2024": "", "raised_2025": ""}}) == ""
    assert bf.oc_avg_funding("x", {"x": {"raised_2024": "10", "raised_2025": "20"}}) == "15"
