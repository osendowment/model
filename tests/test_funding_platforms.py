from src.pipeline.common.funding_platforms import (
    FUNDING_PLATFORMS,
    platform_handle_from_url,
    normalize_oc_slug,
)


def test_canonical_set_has_core_platforms():
    for p in ("github", "open_collective", "patreon", "tidelift", "custom"):
        assert p in FUNDING_PLATFORMS


def test_platform_handle_github_sponsors():
    assert platform_handle_from_url("https://github.com/sponsors/openssl") == ("github", "openssl")


def test_platform_handle_open_collective():
    assert platform_handle_from_url("https://opencollective.com/vuejs") == ("open_collective", "vuejs")


def test_platform_handle_patreon_and_kofi():
    assert platform_handle_from_url("https://www.patreon.com/aiohttp") == ("patreon", "aiohttp")
    assert platform_handle_from_url("https://ko-fi.com/foo") == ("ko_fi", "foo")


def test_platform_handle_unknown_returns_none_and_full_url():
    plat, handle = platform_handle_from_url("https://example.com/donate")
    assert plat is None
    assert handle == "https://example.com/donate"


def test_platform_handle_blank():
    assert platform_handle_from_url("") == (None, "")


def test_normalize_oc_slug():
    assert normalize_oc_slug("babel") == "babel"
    assert normalize_oc_slug("https://opencollective.com/Babel") == "babel"
    assert normalize_oc_slug("opencollective.com/aiohttp#section") == "aiohttp"
    assert normalize_oc_slug("") == ""
