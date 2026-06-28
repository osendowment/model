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
    _write(value, ["github_repo", "valid", "class"], [
        {"github_repo": "Owner/A", "valid": "True", "class": "A"},
        {"github_repo": "owner/b", "valid": "True", "class": "B"},
        {"github_repo": "owner/c", "valid": "True", "class": "C"},
        {"github_repo": "", "valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11", "archived": "False", "size": "5", "stars": "9"},
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    # owner/b archived, owner/c out of classes, "" orphan.
    assert {e.repo for e in out} == {"owner/a"}
    assert out[0].repo_id == "11"
    assert out[0].value_class == "A"


def test_load_top_repos_filters_valid_by_default(tmp_path, monkeypatch):
    """valid is gated by default — only valid==True in-class rows are kept."""
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "valid", "class"], [
        {"github_repo": "owner/live", "valid": "True", "class": "A"},
        {"github_repo": "owner/dead", "valid": "False", "class": "A"},
        {"github_repo": "owner/blank", "valid": "", "class": "B"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert {e.repo for e in out} == {"owner/live"}
    # opting out includes invalid/blank rows again
    opted = repos.load_top_repos(value_file=str(value), repos_file=str(gh), skip_invalid=False)
    assert {e.repo for e in opted} == {"owner/live", "owner/dead", "owner/blank"}


def test_load_top_repos_keeps_archived_when_flag_off(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "valid", "class"], [
        {"github_repo": "owner/b", "valid": "True", "class": "B"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh), skip_archived=False)
    assert {e.repo for e in out} == {"owner/b"}


def test_load_top_repos_dedup_highest_class_wins(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "valid", "class"], [
        {"github_repo": "owner/dup", "valid": "True", "class": "B"},
        {"github_repo": "Owner/Dup", "valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/dup", "valid": "True", "repo_id": "7", "archived": "False", "size": "0", "stars": "0"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert len(out) == 1
    assert out[0].value_class == "A"


def test_load_top_repos_canonicalises_renamed_repo(tmp_path, monkeypatch):
    """A stale slug in value-data.csv resolves to the repo's current name."""
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "valid", "class"], [
        {"github_repo": "gozala/events", "valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "full_name", "archived", "size", "stars"], [
        {"repo": "gozala/events", "valid": "True", "repo_id": "1649251",
         "full_name": "browserify/events", "archived": "False", "size": "9", "stars": "3"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_top_repos(value_file=str(value), repos_file=str(gh))
    assert [e.repo for e in out] == ["browserify/events"]
    assert out[0].repo_id == "1649251"


def test_load_repo_ids(tmp_path):
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "full_name"], [
        {"repo": "Owner/Name", "valid": "True", "repo_id": "99", "full_name": "Owner/Name"},
        {"repo": "stale/slug", "valid": "True", "repo_id": "77", "full_name": "fresh/slug"},
        {"repo": "owner/noid", "valid": "True", "repo_id": "", "full_name": "owner/noid"},
    ])
    ids = repos.load_repo_ids(repos_file=str(gh))
    assert ids["owner/name"] == "99"
    # both the stale and the canonical slug resolve to the same id
    assert ids["stale/slug"] == "77"
    assert ids["fresh/slug"] == "77"
    assert "owner/noid" not in ids


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
