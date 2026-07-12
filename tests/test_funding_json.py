"""Tests for src/sources/floss_fund/funding_json.py channel→platform parsing,
redirect resolution of non-GitHub repo URLs, and repo_id stamping."""

from src.sources.floss_fund.funding_json import (
    _redirect_candidate_urls,
    parse_channels,
    resolve_repo_ids,
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


def test_redirect_candidate_urls_probes_unknown_github_slug():
    """A GitHub URL whose owner/repo the pipeline can't resolve (an unfollowed
    rename / out-of-scope slug) IS probed so GitHub's 301 recovers the current
    name; a slug already known to the model is skipped; `known_github_slugs=None`
    keeps the legacy skip-all-GitHub behaviour. Non-GitHub URLs are always
    candidates, whatever the known set."""
    old = [{"project_repository": "https://github.com/old/name"}]
    # old slug unknown → probe it (follow the rename redirect)
    assert _redirect_candidate_urls(old, known_github_slugs={"new/name"}) == {
        "https://github.com/old/name"}
    # slug already resolvable → skip (don't probe the thousands of matching manifests)
    assert _redirect_candidate_urls(old, known_github_slugs={"old/name"}) == set()
    # legacy: None → all GitHub URLs skipped
    assert _redirect_candidate_urls(old, known_github_slugs=None) == set()
    assert _redirect_candidate_urls(old) == set()
    # a non-GitHub URL is always a candidate, regardless of the known set
    non_gh = [{"project_repository": "https://tukaani.org/xz/redir"}]
    assert _redirect_candidate_urls(non_gh, known_github_slugs={"a/b"}) == {
        "https://tukaani.org/xz/redir"}


def test_resolve_repo_redirects_follows_unknown_github_rename():
    """A manifest declaring an OLD GitHub slug the model knows only under its NEW
    name has its rename redirect followed, recovering the current URL — the raw
    old slug alone would leave repo_id blank and get the row dropped."""
    rows = [{"project_repository": "https://github.com/old/name"}]
    # fake resolver stands in for GitHub's 301 old → new.
    fake = {"https://github.com/old/name": "https://github.com/new/name"}
    n = resolve_repo_redirects(rows, resolver=lambda u: fake.get(u),
                               known_github_slugs={"new/name"})
    assert n == 1
    assert rows[0]["project_repository_resolved"] == "https://github.com/new/name"


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


def test_resolve_repo_redirects_keeps_last_good_on_transient_failure():
    """A transient resolver failure must NOT blank a previously-resolved value."""
    rows = [{"project_repository": "https://tukaani.org/xz/redir"}]
    last_good = {"https://tukaani.org/xz/redir": "https://github.com/tukaani-project/xz"}
    # resolver fails this run (returns None for everything, simulating a network error).
    n = resolve_repo_redirects(rows, resolver=lambda u: None, max_workers=2,
                               last_good=last_good)
    assert n == 0
    # fresh resolve is empty, so we fall back to the last-good value, not "".
    assert rows[0]["project_repository_resolved"] == "https://github.com/tukaani-project/xz"


def test_resolve_repo_redirects_fresh_resolve_overrides_last_good():
    """A successful fresh resolve takes precedence over the last-good fallback."""
    rows = [{"project_repository": "https://tukaani.org/xz/redir"}]
    last_good = {"https://tukaani.org/xz/redir": "https://github.com/old/location"}
    fake = {"https://tukaani.org/xz/redir": "https://github.com/tukaani-project/xz"}
    n = resolve_repo_redirects(rows, resolver=lambda u: fake.get(u), max_workers=2,
                               last_good=last_good)
    assert n == 1
    assert rows[0]["project_repository_resolved"] == "https://github.com/tukaani-project/xz"


# ── repo_id stamping ─────────────────────────────────────────────────────────

def test_resolve_repo_ids_canonicalizes_and_never_invents():
    """repo_id is looked up via the canonical slug (a manifest URL may predate a
    rename); org-level / non-GitHub / out-of-scope rows stay blank — no id is
    ever invented."""
    rows = [
        {"project_repository": "https://github.com/gozala/events"},    # pre-rename slug
        {"project_repository": "https://tukaani.org/xz/redir",
         "project_repository_resolved": "https://github.com/tukaani-project/xz"},
        {"project_repository": "https://github.com/unknown/repo"},     # not in the maps
        {"project_repository": "https://github.com/someorg"},          # org page → no slug
        {"project_repository": "https://codeberg.org/a/b"},            # non-GitHub
    ]
    n = resolve_repo_ids(
        rows,
        ids={"browserify/events": "665422", "tukaani-project/xz": "394784806"},
        canon={"gozala/events": "browserify/events"})
    assert n == 2
    assert rows[0]["repo_id"] == "665422"      # old slug → canonical → id
    assert rows[1]["repo_id"] == "394784806"   # resolved redirect URL wins
    assert rows[2]["repo_id"] == ""            # unresolvable → blank, never invented
    assert rows[3]["repo_id"] == ""
    assert rows[4]["repo_id"] == ""


def test_resolve_repo_ids_preserves_api_resolved_disk_ids(tmp_path, monkeypatch):
    """A directory refresh rewrites the whole file; ids resolved once via the
    GitHub API (out-of-scope repos, unknown to the local maps) must survive
    through the disk-id base layer instead of being wiped back to blank."""
    import csv
    from src.sources.floss_fund import funding_json as fj

    prev = tmp_path / "funding-json.csv"
    with open(prev, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fj.OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerow({"id": "1",
                    "project_repository": "https://github.com/outof/scope",
                    "repo_id": "424242"})
    monkeypatch.setattr(fj, "OUTPUT_FILE", str(prev))
    monkeypatch.setattr(fj, "load_repo_ids", lambda: {})
    monkeypatch.setattr(fj, "load_value_repo_ids", lambda: {})
    monkeypatch.setattr(fj, "canonical_repo_map", lambda: {})

    fresh = [{"id": "1", "project_repository": "https://github.com/outof/scope"}]
    assert fj.resolve_repo_ids(fresh) == 1
    assert fresh[0]["repo_id"] == "424242"   # kept from disk, not wiped
