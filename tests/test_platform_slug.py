"""Regression tests for the `(platform, repo)` identity derivation that backs
the `platform` / `repo` columns of `data/value/value.csv`.

`platform_and_slug` (build_git_urls) is the single source of truth for a
non-GitHub row's identity; `_identity` (unify_value_data) wraps it with the
GitHub-slug-wins rule used when grouping packages into repos.
"""

import pytest

from src.value.build_git_urls import platform_and_slug
from src.value.unify_value_data import _identity


class TestPlatformAndSlug:
    @pytest.mark.parametrize("url,expected", [
        # host → (platform, slug). Slugs are lowercased.
        ("https://github.com/Facebook/React.git", ("github", "facebook/react")),
        ("https://gitlab.gnome.org/gnome/glib.git", ("gitlab", "gnome/glib")),
        # GitLab keeps arbitrarily-nested group paths (not truncated to 2 parts).
        ("https://salsa.debian.org/xorg-team/lib/libx11.git",
         ("gitlab", "xorg-team/lib/libx11")),
        ("https://codeberg.org/tlocke/pg8000.git", ("codeberg", "tlocke/pg8000")),
        ("https://bitbucket.org/multicoreware/x265_git.git",
         ("bitbucket", "multicoreware/x265_git")),
        # sourcehut keeps the ~user prefix and uses no .git suffix.
        ("git://git.sr.ht/~kvsari/derive-getters", ("sourcehut", "~kvsari/derive-getters")),
        # custom host → best-effort path.
        ("git://git.savannah.gnu.org/gettext.git", ("custom", "gettext")),
        # unrecognised / non-git → empty identity.
        ("https://example.com/not-a-repo", ("", "")),
        ("", ("", "")),
    ])
    def test_derives_platform_and_slug(self, url, expected):
        assert platform_and_slug(url) == expected

    def test_scheme_is_normalised_before_classifying(self):
        # ssh + git:// forms resolve to the same (platform, slug) as https.
        assert platform_and_slug("git@github.com:torvalds/linux.git") == ("github", "torvalds/linux")
        assert platform_and_slug("git://github.com/torvalds/linux.git") == ("github", "torvalds/linux")


class TestIdentity:
    def test_github_slug_wins_regardless_of_url(self):
        # A member-derived github slug takes the github identity; verify_git_urls
        # later reconciles git_url to https://github.com/<slug>.git.
        assert _identity("facebook/react", "https://gitlab.com/x/y.git") == ("github", "facebook/react")

    def test_non_github_url_is_classified_when_no_github_slug(self):
        assert _identity("", "https://gitlab.gnome.org/gnome/glib.git") == ("gitlab", "gnome/glib")

    def test_orphan_has_empty_identity(self):
        assert _identity("", "") == ("", "")
