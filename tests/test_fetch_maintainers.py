"""Tests for src/sources/ecosystems/fetch_maintainers.py."""
import csv
import json

from src.sources.ecosystems import fetch_maintainers as fm


def _write_packages_csv(path, rows):
    header = ["ecosystem", "package", "purl", "registry_hit", "repository_url",
              "homepage", "repo_host", "repo_full_name", "repo_archived",
              "repo_fork", "repo_stars", "last_synced_at", "fetched_at"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _write_repos_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "repo_id", "full_name"])
        w.writerows(rows)


def _write_cache(data_dir, eco, pkg, maintainers):
    cache_path = fm._cache_path(data_dir, eco, pkg)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {
        "ecosystem": eco,
        "registry_hit": f"{eco}.org",
        "fetched_at": "2026-07-01T00:00:00Z",
        "data": {"maintainers": maintainers},
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(wrapper, f)


def test_maintainer_rows_from_cache_skips_entries_with_no_login():
    wrapper = {
        "fetched_at": "2026-07-01T00:00:00Z",
        "data": {"maintainers": [
            {"login": "alice", "name": "Alice A", "email": "a@example.com",
             "role": None, "uuid": "alice", "html_url": "https://npmjs.com/~alice"},
            {"login": "", "name": "no login"},
        ]},
    }
    rows = fm._maintainer_rows_from_cache("npm", "leftpad", "gh/1", wrapper)
    assert len(rows) == 1
    assert rows[0] == {
        "ecosystem": "npm", "package": "leftpad", "repo_id": "gh/1",
        "login": "alice", "name": "Alice A", "email": "a@example.com",
        "role": "", "uuid": "alice", "html_url": "https://npmjs.com/~alice",
        "fetched_at": "2026-07-01T00:00:00Z",
    }


def test_build_resolves_repo_id_and_skips_cpp(tmp_path):
    packages_csv = tmp_path / "packages.csv"
    repos_csv = tmp_path / "repos.csv"
    _write_packages_csv(packages_csv, [
        ["npm", "leftpad", "", "npmjs.org", "https://github.com/foo/leftpad", "",
         "github", "foo/leftpad", "False", "False", "1", "", "2026-07-01T00:00:00Z"],
        ["cpp", "somepkg", "", "debian-13", "", "", "", "", "", "", "", "", ""],
    ])
    _write_repos_csv(repos_csv, [["foo/leftpad", "gh/42", "foo/leftpad"]])
    _write_cache(tmp_path, "npm", "leftpad", [
        {"login": "alice", "name": "Alice", "email": "", "role": None,
         "uuid": "alice", "html_url": ""},
    ])

    rows = fm.build(data_dir=tmp_path, packages_file=packages_csv,
                    repos_file=str(repos_csv))

    assert len(rows) == 1                       # cpp package skipped entirely
    assert rows[0]["login"] == "alice"
    assert rows[0]["repo_id"] == "gh/42"         # resolved via repos.csv full_name


def test_build_skips_package_with_no_cached_json(tmp_path):
    packages_csv = tmp_path / "packages.csv"
    repos_csv = tmp_path / "repos.csv"
    _write_packages_csv(packages_csv, [
        ["pypi", "nevercached", "", "pypi.org", "", "", "", "", "", "", "", "", ""],
    ])
    _write_repos_csv(repos_csv, [])

    rows = fm.build(data_dir=tmp_path, packages_file=packages_csv,
                    repos_file=str(repos_csv))
    assert rows == []
