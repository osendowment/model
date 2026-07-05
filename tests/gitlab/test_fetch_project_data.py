"""Tests for gitlab/fetch_project_data — project fetch, flatten, redirects.

The GitLab API is faked with a scripted FakeLimiter (mirrors the GitHub
fetch_repo_owner_data test), so no network is touched.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from src.sources.gitlab.fetch_project_data import (
    PROJECT_FIELDS,
    _fetch_namespace,
    _fetch_project,
    _filter_stale,
    _flat_namespace,
    _flat_project,
    load_gitlab_rows,
    upsert,
)


class FakeResponse:
    def __init__(self, status, *, location=None, json_body=None):
        self.status = status
        self.headers = {}
        if location is not None:
            self.headers["Location"] = location
        self._json = json_body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return ""


class FakeLimiter:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.n = 0

    async def get(self, session, host, url):
        self.calls.append((host, url))
        self.n += 1
        return self._responses.pop(0)


def _project_body(pid, path, kind="group"):
    return {
        "id": pid, "path_with_namespace": path, "name": path.split("/")[-1],
        "default_branch": "main", "star_count": 12, "forks_count": 3,
        "open_issues_count": 4, "archived": False, "visibility": "public",
        "web_url": f"https://gitlab.com/{path}", "topics": ["a", "b"],
        "created_at": "2015-01-01T00:00:00Z", "last_activity_at": "2026-01-01T00:00:00Z",
        "license": {"key": "mit", "nickname": "MIT"},
        "namespace": {"kind": kind, "full_path": path.rsplit("/", 1)[0]},
    }


class TestFlatProject:
    def test_maps_fields_and_repo_id(self):
        row = _flat_project(_project_body(678, "debian/foo"), "salsa.debian.org",
                            "salsa.debian.org/debian/foo")
        assert row["repo_id"] == "gl/salsa.debian.org-678"
        assert row["project_id"] == 678
        assert row["valid"] is True
        assert row["owner_type"] == "Organization"   # kind=group → Organization
        assert row["namespace_kind"] == "group"
        assert row["license"] == "mit"
        assert row["default_branch"] == "main"
        assert row["stars"] == 12
        assert row["topics"] == "a | b"

    def test_user_namespace_maps_to_user(self):
        row = _flat_project(_project_body(1, "alice/proj", kind="user"),
                            "gitlab.com", "gitlab.com/alice/proj")
        assert row["owner_type"] == "User"


class TestFetchProject:
    async def test_200_returns_ok_row(self):
        item = {"host": "salsa.debian.org", "path": "debian/foo",
                "project": "salsa.debian.org/debian/foo"}
        lim = FakeLimiter([FakeResponse(200, json_body=_project_body(678, "debian/foo"))])
        key, row, status = await _fetch_project(lim, None, item)
        assert status == "ok"
        assert key == "salsa.debian.org/debian/foo"
        assert row["repo_id"] == "gl/salsa.debian.org-678"

    async def test_404_returns_sparse_invalid_row(self):
        item = {"host": "gitlab.com", "path": "gone/x", "project": "gitlab.com/gone/x"}
        lim = FakeLimiter([FakeResponse(404)])
        key, row, status = await _fetch_project(lim, None, item)
        assert status == "404"
        assert row["valid"] is False
        assert row["project"] == "gitlab.com/gone/x"

    async def test_follows_redirect_chain(self):
        item = {"host": "gitlab.com", "path": "old/x", "project": "gitlab.com/old/x"}
        lim = FakeLimiter([
            FakeResponse(301, location="https://gitlab.com/api/v4/projects/9"),
            FakeResponse(200, json_body=_project_body(9, "new/x")),
        ])
        key, row, status = await _fetch_project(lim, None, item)
        assert status == "ok"
        assert key == "gitlab.com/old/x"          # key stays what we asked
        assert row["repo_id"] == "gl/9"
        assert len(lim.calls) == 2


class TestLoadGitlabRows:
    def test_selects_only_gitlab_hosts_and_dedups(self, tmp_path: Path):
        csv_text = (
            "github_repo,gh_repo_id,git_url,valid,class\n"
            "npm/cli,1,https://github.com/npm/cli.git,True,A\n"
            ",,https://salsa.debian.org/debian/foo.git,False,B\n"
            ",,https://salsa.debian.org/debian/foo.git,False,C\n"   # dup
            ",,https://gitlab.com/group/sub/proj,False,C\n"
            ",,https://bitbucket.org/team/repo,False,C\n"           # not gitlab
        )
        vf = tmp_path / "value.csv"
        vf.write_text(csv_text, encoding="utf-8")
        rows = load_gitlab_rows(vf)
        keys = sorted(r["project"] for r in rows)
        assert keys == ["gitlab.com/group/sub/proj", "salsa.debian.org/debian/foo"]
        foo = next(r for r in rows if r["host"] == "salsa.debian.org")
        assert foo["path"] == "debian/foo"

    def test_classes_filter_scopes_to_given_classes(self, tmp_path: Path):
        csv_text = (
            "github_repo,gh_repo_id,git_url,valid,class\n"
            ",,https://salsa.debian.org/debian/aaa.git,False,A\n"
            ",,https://salsa.debian.org/debian/bbb.git,False,B\n"
            ",,https://salsa.debian.org/debian/ccc.git,False,C\n"
        )
        vf = tmp_path / "value.csv"
        vf.write_text(csv_text, encoding="utf-8")
        keys = sorted(r["project"] for r in load_gitlab_rows(vf, classes={"A", "B"}))
        assert keys == ["salsa.debian.org/debian/aaa", "salsa.debian.org/debian/bbb"]
        # None (default) keeps every class
        assert len(load_gitlab_rows(vf)) == 3


def _ns_body(nid, full_path, kind="group"):
    return {"id": nid, "name": full_path.split("/")[-1], "path": full_path.split("/")[-1],
            "full_path": full_path, "kind": kind,
            "web_url": f"https://gitlab.com/{full_path}", "description": "d"}


class TestNamespace:
    def test_flat_namespace(self):
        row = _flat_namespace(_ns_body(5, "debian"), "salsa.debian.org",
                              "salsa.debian.org/debian")
        assert row["namespace"] == "salsa.debian.org/debian"
        assert row["namespace_id"] == 5
        assert row["kind"] == "group"
        assert row["host"] == "salsa.debian.org"

    async def test_fetch_namespace_200(self):
        item = {"host": "salsa.debian.org", "full_path": "debian",
                "namespace": "salsa.debian.org/debian"}
        lim = FakeLimiter([FakeResponse(200, json_body=_ns_body(5, "debian"))])
        key, row, status = await _fetch_namespace(lim, None, item)
        assert status == "ok"
        assert row["namespace_id"] == 5

    async def test_fetch_namespace_404(self):
        item = {"host": "gitlab.com", "full_path": "gone",
                "namespace": "gitlab.com/gone"}
        lim = FakeLimiter([FakeResponse(404)])
        key, row, status = await _fetch_namespace(lim, None, item)
        assert status == "404"
        assert row is None


def _iso_days_ago(n):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestUpsertAndStale:
    def test_upsert_roundtrip(self, tmp_path):
        out = tmp_path / "projects.csv"
        n = upsert(out, "project", PROJECT_FIELDS,
                   [{"project": "gitlab.com/a/b", "valid": True,
                     "repo_id": "gl/1", "fetched_at": _iso_days_ago(0)}])
        assert n == 1
        # re-upsert same key overwrites, not appends
        n2 = upsert(out, "project", PROJECT_FIELDS,
                    [{"project": "gitlab.com/a/b", "valid": True,
                      "repo_id": "gl/1", "fetched_at": _iso_days_ago(0)}])
        assert n2 == 1

    def test_filter_stale_skips_fresh_keeps_old_and_missing(self, tmp_path):
        existing = {
            "gitlab.com/a/b": {"project": "gitlab.com/a/b", "fetched_at": _iso_days_ago(10)},
            "gitlab.com/c/d": {"project": "gitlab.com/c/d", "fetched_at": _iso_days_ago(200)},
        }
        items = [
            {"project": "gitlab.com/a/b"}, {"project": "gitlab.com/c/d"},
            {"project": "gitlab.com/e/f"},
        ]
        to_fetch, fresh, missing = _filter_stale(items, existing, "project", False)
        keys = sorted(x["project"] for x in to_fetch)
        assert keys == ["gitlab.com/c/d", "gitlab.com/e/f"]   # old + missing
        assert fresh == 1 and missing == 1
