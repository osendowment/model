"""Tests for src/sources/floss_fund/funding_json.py channel→platform parsing."""

from src.sources.floss_fund.funding_json import parse_channels


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
