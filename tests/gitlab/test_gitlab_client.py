"""Tests for gitlab_client URL/host helpers — pure functions, no network."""
from __future__ import annotations

from src.sources.gitlab import gitlab_client as gc


class TestIsGitlabHost:
    def test_known_saas(self):
        assert gc.is_gitlab_host("gitlab.com")

    def test_known_self_hosted(self):
        assert gc.is_gitlab_host("salsa.debian.org")
        assert gc.is_gitlab_host("invent.kde.org")

    def test_gitlab_prefixed_host_is_detected(self):
        # Any gitlab.* subdomain counts as a GitLab instance.
        assert gc.is_gitlab_host("gitlab.gnome.org")
        assert gc.is_gitlab_host("gitlab.freedesktop.org")

    def test_github_is_not_gitlab(self):
        assert not gc.is_gitlab_host("github.com")


class TestParseGitUrl:
    def test_https_dot_git(self):
        assert gc.parse_git_url("https://salsa.debian.org/debian/foo.git") == (
            "salsa.debian.org", "debian/foo")

    def test_multilevel_namespace(self):
        assert gc.parse_git_url("https://gitlab.com/group/sub/proj") == (
            "gitlab.com", "group/sub/proj")

    def test_trailing_slash_and_case(self):
        assert gc.parse_git_url("https://gitlab.com/Group/Proj/") == (
            "gitlab.com", "Group/Proj")

    def test_non_gitlab_returns_none(self):
        assert gc.parse_git_url("https://github.com/npm/cli") is None

    def test_strips_web_suffix(self):
        assert gc.parse_git_url("https://salsa.debian.org/debian/foo/-/tree/master") == (
            "salsa.debian.org", "debian/foo")


class TestUrlBuilders:
    def test_encode_project_path(self):
        assert gc.encode_project_path("group/sub/proj") == "group%2Fsub%2Fproj"

    def test_api_base(self):
        assert gc.api_base("salsa.debian.org") == "https://salsa.debian.org/api/v4"

    def test_clone_url(self):
        assert gc.clone_url("gitlab.com", "group/proj") == "https://gitlab.com/group/proj.git"

    def test_make_repo_id(self):
        assert gc.make_repo_id("salsa.debian.org", 678) == "gl/salsa.debian.org/678"
        assert gc.make_repo_id("gitlab.com", "278964") == "gl/gitlab.com/278964"
