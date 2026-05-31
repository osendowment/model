"""Tests for src/pipeline/risk/build_funding.py — source join + has_funding_json."""

from src.pipeline.risk import build_funding as bf


def test_assemble_row_joins_sources_and_derives_json():
    row = bf.assemble_row(
        repo="vuejs/core", repo_id="11730342",
        sponsors={"github_sponsors": "12", "fetched_at": "2026-05-19T10:00:00+00:00"},
        yml={"has_funding_yml": "True", "funding_yml_platforms": "github",
             "fetched_at": "2026-05-19T11:00:00+00:00"},
        foundation_host="",
        directory_repos={"vuejs/core"},
    )
    assert row == {
        "repo": "vuejs/core", "repo_id": "11730342", "github_sponsors": "12",
        "has_funding_yml": "True", "funding_yml_platforms": "github",
        "has_funding_json": "True", "foundation_host": "",
        "fetched_at": "2026-05-19T11:00:00+00:00"}  # latest of the two sources
    assert "funding_class" not in row


def test_assemble_row_absent_from_directory_is_false():
    row = bf.assemble_row(
        repo="acornjs/acorn", repo_id="1", sponsors={}, yml={},
        foundation_host="apache", directory_repos=set())
    assert row["has_funding_json"] == "False"
    assert row["github_sponsors"] == ""
    assert row["foundation_host"] == "apache"
