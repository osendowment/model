from src.sources.floss_fund.directory import (
    export_repo_slug,
    github_org_page,
    load_directory_repos,
    normalize_github_repo,
)


def test_normalize_basic():
    assert normalize_github_repo("https://github.com/vuejs/core") == "vuejs/core"


def test_normalize_strips_git_and_trailing_slash():
    assert normalize_github_repo("https://github.com/Eugeny/russh.git/") == "eugeny/russh"


def test_normalize_deep_path_keeps_owner_repo():
    assert normalize_github_repo("https://github.com/owner/repo/tree/main") == "owner/repo"


def test_normalize_non_github_or_blank_is_none():
    assert normalize_github_repo("https://gitlab.com/foo/bar") is None
    assert normalize_github_repo("") is None
    assert normalize_github_repo(None) is None


class TestGithubOrgPage:
    def test_org_page(self):
        assert github_org_page("https://github.com/zulip") == "zulip"
        assert github_org_page("https://github.com/OpenMRS/") == "openmrs"

    def test_specific_repo_is_not_an_org(self):
        assert github_org_page("https://github.com/vuejs/core") is None

    def test_reserved_paths_excluded(self):
        assert github_org_page("https://github.com/sponsors") is None

    def test_non_github_or_blank(self):
        assert github_org_page("https://gitlab.com/foo") is None
        assert github_org_page("") is None
        assert github_org_page(None) is None


class TestExportRepoSlug:
    def test_raw_github_url(self):
        assert export_repo_slug({"project_repository": "https://github.com/vuejs/core"}) == "vuejs/core"

    def test_resolved_redirect_wins(self):
        # raw is a redirect host; the fetcher-resolved URL provides the real repo.
        row = {"project_repository": "https://tukaani.org/xz/redirect-to-github-xz",
               "project_repository_resolved": "https://github.com/tukaani-project/xz"}
        assert export_repo_slug(row) == "tukaani-project/xz"

    def test_non_github_no_resolution_is_none(self):
        assert export_repo_slug({"project_repository": "https://gitlab.com/x/y",
                                 "project_repository_resolved": ""}) is None

    def test_missing_resolved_column_falls_back_to_raw(self):
        # backward compat: old export rows without the resolved column.
        assert export_repo_slug({"project_repository": "https://github.com/o/r"}) == "o/r"


def test_load_directory_repos(tmp_path):
    p = tmp_path / "funding-json.csv"
    p.write_text(
        "project_repository,project_repository_resolved\n"
        "https://github.com/vuejs/core,\n"
        ",\n"
        "https://gitlab.com/x/y,\n"
        "https://github.com/Owner/Repo.git,\n"
        "https://tukaani.org/xz/redirect,https://github.com/tukaani-project/xz\n",
        encoding="utf-8",
    )
    assert load_directory_repos(p) == {"vuejs/core", "owner/repo", "tukaani-project/xz"}


def test_load_directory_repos_missing_file(tmp_path):
    assert load_directory_repos(tmp_path / "nope.csv") == set()
