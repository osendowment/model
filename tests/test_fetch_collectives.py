"""Tests for the OpenCollective reverse-map parser (fetch_collectives).

Focus: the search-URL guard. A collective whose GitHub link is a filtered
search (github.com/<org>?q=…) must NOT seed an org-level claim on the whole
org — the "Friends of Apache Grails" collective linked a Grails-scoped search
and leaked onto every apache/* repo until this guard was added.
"""
import csv

from src.sources.opencollective.fetch_collectives import (
    _gh_parse,
    _is_search_url,
    load_index,
)


def test_is_search_url():
    assert _is_search_url("https://github.com/apache?q=grails-&type=all")
    assert _is_search_url("https://github.com/apache?type=all&q=grails")
    # Clean profile / repo links are NOT searches.
    assert not _is_search_url("https://github.com/apache")
    assert not _is_search_url("https://github.com/oozcitak")
    assert not _is_search_url("https://github.com/tokio-rs/bytes")
    assert not _is_search_url("https://github.com/owner/repo?tab=readme")
    assert not _is_search_url(None)


def test_gh_parse_rejects_search_url():
    """A search URL yields no owner/repo, so the collective attributes to nothing."""
    assert _gh_parse("https://github.com/apache?q=grails-&type=all") == ("", "")
    # Sanity: a clean org link still parses.
    assert _gh_parse("https://github.com/apache") == ("apache", "")


def test_load_index_skips_search_url_rows(tmp_path):
    """A row stored (pre-fix) with github_owner=apache but a search github_url must
    be skipped on read, so it never claims the apache org; clean rows still map."""
    p = tmp_path / "collectives.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slug", "name", "github_owner", "github_repo", "github_url", "fetched_at"])
        # Leaky search-URL row (the friends-of-grails shape).
        w.writerow(["friends-of-grails", "Friends of Apache Grails", "apache", "",
                    "https://github.com/apache?q=grails-&type=all", "2026-06-29T00:00:00+00:00"])
        # Legit org-level and repo-level rows.
        w.writerow(["imagelistview", "Imagelistview", "oozcitak", "",
                    "https://github.com/oozcitak", "2026-06-29T00:00:00+00:00"])
        w.writerow(["tokio", "Tokio", "tokio-rs", "tokio-rs/bytes",
                    "https://github.com/tokio-rs/bytes", "2026-06-29T00:00:00+00:00"])
    by_repo, by_org = load_index(p)
    assert "apache" not in by_org           # search URL did NOT claim the org
    assert by_org.get("oozcitak") == "imagelistview"
    assert by_repo.get("tokio-rs/bytes") == "tokio"
