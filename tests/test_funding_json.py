"""Tests for src/sources/floss_fund/funding_json.py channel→platform parsing
and redirect resolution of non-GitHub repo URLs."""

from src.sources.floss_fund.funding_json import (
    _redirect_candidate_urls,
    parse_channels,
    resolve_repo_redirects,
)


def test_parse_channels_maps_platforms():
    channels = [
        {"type": "payment-provider", "address": "https://github.com/sponsors/openssl"},
        {"type": "other", "address": "https://opencollective.com/vuejs"},
        {"type": "bank", "description": "wire transfer"},  # no address
    ]
    handles, platforms = parse_channels(channels)
    assert handles["github"] == "openssl"
    assert handles["open_collective"] == "vuejs"
    assert platforms == "github,open_collective,bank"


def test_parse_channels_unknown_address_to_custom():
    handles, platforms = parse_channels([{"type": "other", "address": "https://example.com/give"}])
    assert handles["custom"] == "https://example.com/give"
    assert platforms == "custom"


def test_parse_channels_dedups_platform_names():
    channels = [
        {"type": "other", "address": "https://opencollective.com/a"},
        {"type": "other", "address": "https://opencollective.com/b"},
    ]
    handles, platforms = parse_channels(channels)
    assert handles["open_collective"] == "a,b"
    assert platforms == "open_collective"


def test_parse_channels_empty():
    assert parse_channels([]) == ({}, "")


# ── redirect resolution ──────────────────────────────────────────────────────

def test_redirect_candidate_urls_only_non_github_http():
    rows = [
        {"project_repository": "https://github.com/o/r"},      # already github → skip
        {"project_repository": "https://tukaani.org/xz/redir"},  # candidate
        {"project_repository": "https://gitlab.com/a/b"},        # candidate
        {"project_repository": ""},                              # empty → skip
        {"project_repository": "git@host:x"},                    # not http → skip
    ]
    assert _redirect_candidate_urls(rows) == {
        "https://tukaani.org/xz/redir", "https://gitlab.com/a/b"}


def test_resolve_repo_redirects_populates_resolved_column():
    rows = [
        {"project_repository": "https://github.com/o/r"},
        {"project_repository": "https://tukaani.org/xz/redir"},
        {"project_repository": "https://codeberg.org/a/b"},      # no github redirect
    ]
    # fake resolver: tukaani redirect lands on a github repo, codeberg does not.
    fake = {"https://tukaani.org/xz/redir": "https://github.com/tukaani-project/xz"}
    n = resolve_repo_redirects(rows, resolver=lambda u: fake.get(u), max_workers=2)
    assert n == 1
    assert rows[0]["project_repository_resolved"] == ""            # already github
    assert rows[1]["project_repository_resolved"] == "https://github.com/tukaani-project/xz"
    assert rows[2]["project_repository_resolved"] == ""            # not resolvable
