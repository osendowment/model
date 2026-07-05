"""Tests for the risk-pipeline repo loaders in src/common/repos.py."""

import csv

from src.common import repos


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def test_load_top_repos_filters_classes_and_enriches(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "Owner/A", "git_valid": "True", "class": "A", "platform": "github"},
        {"repo": "owner/b", "git_valid": "True", "class": "B", "platform": "github"},
        {"repo": "owner/c", "git_valid": "True", "class": "C", "platform": "github"},
        {"repo": "", "git_valid": "True", "class": "A", "platform": ""},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11", "archived": "False", "size": "5", "stars": "9"},
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    # owner/b archived, owner/c out of classes, "" orphan.
    assert {e.repo for e in out} == {"owner/a"}
    assert out[0].repo_id == "gh/11"  # loader namespaces bare GitHub ids
    assert out[0].value_class == "A"


def test_load_top_repos_filters_valid_by_default(tmp_path, monkeypatch):
    """git_valid is gated by default — only git_valid==True in-class rows are kept."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "owner/live", "git_valid": "True", "class": "A", "platform": "github"},
        {"repo": "owner/dead", "git_valid": "False", "class": "A", "platform": "github"},
        {"repo": "owner/blank", "git_valid": "", "class": "B", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert {e.repo for e in out} == {"owner/live"}
    # opting out includes invalid/blank rows again
    opted = repos.load_top_repos(value_file=str(value), repos_file=str(gh), skip_invalid=False)
    assert {e.repo for e in opted} == {"owner/live", "owner/dead", "owner/blank"}


def test_load_top_repos_keeps_archived_when_flag_off(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "owner/b", "git_valid": "True", "class": "B", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh), skip_archived=False)
    assert {e.repo for e in out} == {"owner/b"}


def test_load_top_repos_keeps_archived_live_upstream_mirror(tmp_path, monkeypatch):
    """An archived GitHub mirror of a live non-github upstream (per
    overrides.csv) is KEPT; a plain archived repo (no mirror override) is
    DROPPED."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "bminor/glibc", "git_valid": "True", "class": "A", "platform": "github"},
        {"repo": "owner/plainarchived", "git_valid": "True", "class": "A", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "bminor/glibc", "valid": "True", "repo_id": "10",
         "archived": "True", "size": "5", "stars": "9"},
        {"repo": "owner/plainarchived", "valid": "True", "repo_id": "20",
         "archived": "True", "size": "1", "stars": "1"},
    ])
    overrides = tmp_path / "overrides.csv"
    _write(overrides, ["package", "ecosystem", "repo", "git_url", "valid", "reason"], [
        # mirror: non-github git_url -> exempt from skip_archived
        {"package": "glibc", "ecosystem": "cpp", "repo": "bminor/glibc",
         "git_url": "https://sourceware.org/git/glibc.git", "valid": "", "reason": "mirror"},
        # not a mirror: github git_url -> NOT exempt (but it's not archived here anyway)
        {"package": "x", "ecosystem": "cpp", "repo": "owner/plainarchived",
         "git_url": "https://github.com/owner/plainarchived.git", "valid": "", "reason": "x"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(
        value_file=str(value), repos_file=str(gh), overrides_file=str(overrides)
    )
    # glibc mirror kept despite archived; plain archived repo dropped.
    assert {e.repo for e in out} == {"bminor/glibc"}


def test_load_top_repos_dedup_highest_class_wins(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "owner/dup", "git_valid": "True", "class": "B", "platform": "github"},
        {"repo": "Owner/Dup", "git_valid": "True", "class": "A", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/dup", "valid": "True", "repo_id": "7", "archived": "False", "size": "0", "stars": "0"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert len(out) == 1
    assert out[0].value_class == "A"


def test_load_top_repos_canonicalises_renamed_repo(tmp_path, monkeypatch):
    """A stale slug in value-data.csv resolves to the repo's current name."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_valid", "class", "platform"], [
        {"repo": "gozala/events", "git_valid": "True", "class": "A", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "full_name", "archived", "size", "stars"], [
        {"repo": "gozala/events", "valid": "True", "repo_id": "1649251",
         "full_name": "browserify/events", "archived": "False", "size": "9", "stars": "3"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert [e.repo for e in out] == ["browserify/events"]
    assert out[0].repo_id == "gh/1649251"


def test_load_top_repos_sets_git_url_github(tmp_path, monkeypatch):
    """A github entry carries value.csv's git_url — byte-identical to the
    canonical github clone URL the old hardcoded clone path built."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_url", "git_valid", "class", "platform"], [
        {"repo": "owner/a", "git_url": "https://github.com/owner/a.git",
         "git_valid": "True", "class": "A", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11",
         "archived": "False", "size": "5", "stars": "9"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert out[0].git_url == "https://github.com/owner/a.git"


def test_load_top_repos_git_url_github_fallback_when_blank(tmp_path, monkeypatch):
    """A github entry with no git_url in value.csv falls back to its canonical
    github clone URL; enrichment (no git_url column) never blanks it."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "git_url", "git_valid", "class", "platform"], [
        {"repo": "owner/a", "git_url": "",
         "git_valid": "True", "class": "A", "platform": "github"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11",
         "archived": "False", "size": "5", "stars": "9"},
    ])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert out[0].git_url == "https://github.com/owner/a.git"


def test_load_top_repos_sets_git_url_gitlab(tmp_path, monkeypatch):
    """A gitlab entry routes to its real gitlab clone URL, not github."""
    value = tmp_path / "value.csv"
    _write(value, ["repo", "repo_id", "git_url", "git_valid", "class", "platform"], [
        {"repo": "gnome/glib", "repo_id": "gl/gitlab.gnome.org-658",
         "git_url": "https://gitlab.gnome.org/gnome/glib.git",
         "git_valid": "True", "class": "A", "platform": "gitlab"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [])
    monkeypatch.setattr(repos, "TOP_REPO_CLASSES", {"A", "B"})
    monkeypatch.setattr(repos, "TOP_REPO_PLATFORMS", {"github", "gitlab"})
    monkeypatch.setattr(repos, "_read_gitlab_projects", lambda *a, **k: {})
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert [e.repo for e in out] == ["gnome/glib"]
    assert out[0].git_url == "https://gitlab.gnome.org/gnome/glib.git"


def test_load_repo_ids(tmp_path):
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "full_name"], [
        {"repo": "Owner/Name", "valid": "True", "repo_id": "99", "full_name": "Owner/Name"},
        {"repo": "stale/slug", "valid": "True", "repo_id": "77", "full_name": "fresh/slug"},
        {"repo": "owner/noid", "valid": "True", "repo_id": "", "full_name": "owner/noid"},
    ])
    ids = repos.load_repo_ids(repos_file=str(gh))
    assert ids["owner/name"] == "gh/99"
    # both the stale and the canonical slug resolve to the same id
    assert ids["stale/slug"] == "gh/77"
    assert ids["fresh/slug"] == "gh/77"
    assert "owner/noid" not in ids


def test_to_repo_id_namespaces_and_is_idempotent():
    """Bare GitHub ids are promoted to gh/<id>; already-namespaced ids and
    empties pass through unchanged (so re-running the loader never double-
    prefixes)."""
    assert repos.to_repo_id(119609) == "gh/119609"
    assert repos.to_repo_id("119609") == "gh/119609"
    assert repos.to_repo_id("  42 ") == "gh/42"
    # idempotent — already-namespaced host ids are left alone
    assert repos.to_repo_id("gh/119609") == "gh/119609"
    assert repos.to_repo_id("gl/456") == "gl/456"                       # gitlab.com bare id
    assert repos.to_repo_id("gl/salsa.debian.org-678") == "gl/salsa.debian.org-678"
    # empty / missing -> empty (never "gh/")
    assert repos.to_repo_id("") == ""
    assert repos.to_repo_id(None) == ""


def test_read_github_repos_idempotent_on_prefixed_id(tmp_path):
    """A repos.csv already carrying gh/-prefixed ids is not double-prefixed."""
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "full_name"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "gh/11", "full_name": "owner/a"},
    ])
    _, meta = repos._read_github_repos(str(gh))
    assert meta["owner/a"].repo_id == "gh/11"
    assert repos.load_repo_ids(repos_file=str(gh))["owner/a"] == "gh/11"


def test_load_default_branches(tmp_path):
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "full_name", "default_branch"], [
        {"repo": "Owner/Name", "valid": "True", "full_name": "Owner/Name",
         "default_branch": "main"},
        {"repo": "stale/slug", "valid": "True", "full_name": "fresh/slug",
         "default_branch": "master"},
        {"repo": "owner/nobranch", "valid": "True", "full_name": "owner/nobranch",
         "default_branch": ""},
    ])
    branches = repos.load_default_branches(repos_file=str(gh))
    assert branches["owner/name"] == "main"
    # both the stale and the canonical slug resolve to the same branch
    assert branches["stale/slug"] == "master"
    assert branches["fresh/slug"] == "master"
    # a row with no default_branch is skipped
    assert "owner/nobranch" not in branches


def test_read_github_repos_populates_valid_full_name_mirror_url(tmp_path):
    """Verify that _read_github_repos populates the new valid/full_name/mirror_url fields."""
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "full_name", "mirror_url", "repo_id", "archived", "size", "stars"], [
        # Valid repo with mirror URL
        {"repo": "gcc-mirror/gcc", "valid": "True", "full_name": "gcc-mirror/gcc",
         "mirror_url": "git://gcc.gnu.org/git/gcc.git", "repo_id": "123", "archived": "False", "size": "100", "stars": "10"},
        # Valid repo without mirror URL
        {"repo": "owner/normal", "valid": "True", "full_name": "owner/normal",
         "mirror_url": "", "repo_id": "456", "archived": "False", "size": "50", "stars": "5"},
        # Invalid repo with mirror URL
        {"repo": "some/invalid", "valid": "False", "full_name": "some/invalid",
         "mirror_url": "https://example.com/repo.git", "repo_id": "789", "archived": "False", "size": "10", "stars": "1"},
        # Repo with "1" as valid value (should be treated as True)
        {"repo": "owner/alt", "valid": "1", "full_name": "owner/alt",
         "mirror_url": "", "repo_id": "000", "archived": "False", "size": "20", "stars": "2"},
        # Repo with whitespace in valid value
        {"repo": "owner/spaces", "valid": " true ", "full_name": "owner/spaces",
         "mirror_url": "  https://example.com/spaced.git  ", "repo_id": "111", "archived": "False", "size": "30", "stars": "3"},
    ])
    canon, meta = repos._read_github_repos(str(gh))

    # Test gcc-mirror repo
    gcc_entry = meta["gcc-mirror/gcc"]
    assert gcc_entry.valid is True
    assert gcc_entry.full_name == "gcc-mirror/gcc"
    assert gcc_entry.mirror_url == "git://gcc.gnu.org/git/gcc.git"

    # Test normal repo without mirror
    normal_entry = meta["owner/normal"]
    assert normal_entry.valid is True
    assert normal_entry.full_name == "owner/normal"
    assert normal_entry.mirror_url == ""

    # Test invalid repo
    invalid_entry = meta["some/invalid"]
    assert invalid_entry.valid is False
    assert invalid_entry.full_name == "some/invalid"
    assert invalid_entry.mirror_url == "https://example.com/repo.git"

    # Test "1" as valid
    alt_entry = meta["owner/alt"]
    assert alt_entry.valid is True
    assert alt_entry.full_name == "owner/alt"

    # Test whitespace handling
    spaces_entry = meta["owner/spaces"]
    assert spaces_entry.valid is True
    assert spaces_entry.full_name == "owner/spaces"
    assert spaces_entry.mirror_url == "https://example.com/spaced.git"
