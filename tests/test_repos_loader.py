"""Tests for the risk-pipeline repo loaders in src/pipeline/repos.py."""

import csv

from src.pipeline import repos


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def test_load_risk_repos_filters_classes_and_enriches(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "gh_valid", "class"], [
        {"github_repo": "Owner/A", "gh_valid": "True", "class": "A"},
        {"github_repo": "owner/b", "gh_valid": "True", "class": "B"},
        {"github_repo": "owner/c", "gh_valid": "True", "class": "C"},
        {"github_repo": "owner/dead", "gh_valid": "False", "class": "A"},
        {"github_repo": "", "gh_valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/a", "valid": "True", "repo_id": "11", "archived": "False", "size": "5", "stars": "9"},
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_risk_repos(value_file=str(value), repos_file=str(gh))
    # owner/b archived, owner/c out of classes, owner/dead invalid, "" orphan.
    assert {e.repo for e in out} == {"owner/a"}
    assert out[0].repo_id == "11"
    assert out[0].value_class == "A"


def test_load_risk_repos_keeps_archived_when_flag_off(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "gh_valid", "class"], [
        {"github_repo": "owner/b", "gh_valid": "True", "class": "B"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/b", "valid": "True", "repo_id": "22", "archived": "True", "size": "1", "stars": "1"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_risk_repos(value_file=str(value), repos_file=str(gh), skip_archived=False)
    assert {e.repo for e in out} == {"owner/b"}


def test_load_risk_repos_dedup_highest_class_wins(tmp_path, monkeypatch):
    value = tmp_path / "value.csv"
    _write(value, ["github_repo", "gh_valid", "class"], [
        {"github_repo": "owner/dup", "gh_valid": "True", "class": "B"},
        {"github_repo": "Owner/Dup", "gh_valid": "True", "class": "A"},
    ])
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id", "archived", "size", "stars"], [
        {"repo": "owner/dup", "valid": "True", "repo_id": "7", "archived": "False", "size": "0", "stars": "0"},
    ])
    monkeypatch.setattr(repos, "RISK_INPUT_CLASSES", ["A", "B"])
    out = repos.load_risk_repos(value_file=str(value), repos_file=str(gh))
    assert len(out) == 1
    assert out[0].value_class == "A"


def test_load_repo_ids(tmp_path):
    gh = tmp_path / "repos.csv"
    _write(gh, ["repo", "valid", "repo_id"], [
        {"repo": "Owner/Name", "valid": "True", "repo_id": "99"},
        {"repo": "owner/noid", "valid": "True", "repo_id": ""},
    ])
    assert repos.load_repo_ids(repos_file=str(gh)) == {"owner/name": "99"}
