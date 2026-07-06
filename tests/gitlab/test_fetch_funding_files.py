"""Tests for src/sources/gitlab/fetch_funding_files.py — parsing + row shape."""

from src.sources.gitlab.fetch_funding_files import (
    FIELDS,
    _has_signal,
    parse_funding_yml_platforms,
)


def test_parse_funding_yml_inline_values():
    text = ("github: [octocat, hubot]\n"
            "ko_fi: somebody\n"
            "unknown_platform: x\n")
    assert parse_funding_yml_platforms(text) == ["github", "ko_fi"]


def test_parse_funding_yml_block_list_and_comments():
    text = ("# funding\n"
            "custom:\n"
            "  - https://example.org/donate\n"
            "liberapay: someone  # handle\n")
    assert parse_funding_yml_platforms(text) == ["custom", "liberapay"]


def test_parse_funding_yml_empty_keys_declare_nothing():
    # a bare `github:` with no value and no list items is not a declaration
    assert parse_funding_yml_platforms("github:\n") == []
    assert parse_funding_yml_platforms("github: []\n") == []
    assert parse_funding_yml_platforms("") == []
    # indented (non-top-level) keys never match
    assert parse_funding_yml_platforms("  github: x\n") == []


def test_has_signal_drives_ttl():
    assert _has_signal({"has_funding_yml": "True", "has_funding_json_file": "False"})
    assert _has_signal({"has_funding_yml": "False", "has_funding_json_file": "True"})
    assert not _has_signal({"has_funding_yml": "False", "has_funding_json_file": "False"})


def test_fields_meet_source_schema_contract():
    # repo-keyed source CSV: stable repo_id + fetch date + success flag.
    assert {"repo", "repo_id", "status", "fetched_at"} <= set(FIELDS)
