from src.floss_fund.directory import normalize_github_repo, load_directory_repos


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


def test_load_directory_repos(tmp_path):
    p = tmp_path / "funding-json.csv"
    p.write_text(
        "project_repository\n"
        "https://github.com/vuejs/core\n"
        "\n"
        "https://gitlab.com/x/y\n"
        "https://github.com/Owner/Repo.git\n",
        encoding="utf-8",
    )
    assert load_directory_repos(p) == {"vuejs/core", "owner/repo"}


def test_load_directory_repos_missing_file(tmp_path):
    assert load_directory_repos(tmp_path / "nope.csv") == set()
