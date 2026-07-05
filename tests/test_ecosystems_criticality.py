"""Tests for src/sources/ecosystems/criticality.

Network is faked with a scripted FakeSession keyed by URL, so no HTTP is
touched. Covers repo selection (class filter, dedup, URL derivation from main's
value.csv schema), field extraction, and the cpp registry fallback (spack
preferred, debian last).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import src.sources.ecosystems.criticality as cr
from src.sources.ecosystems.criticality import (
    Repo,
    _fetch_one,
    _repo_url,
    extract,
    load_repos,
)


class TestRepoUrl:
    def test_repo_url_strips_git_and_prefers_git_url(self):
        assert _repo_url("https://salsa.debian.org/debian/bzip2.git", "debian/bzip2",
                         "gitlab") == "https://salsa.debian.org/debian/bzip2"

    def test_repo_url_falls_back_to_github_slug(self):
        assert _repo_url("", "expressjs/express", "github") \
            == "https://github.com/expressjs/express"

    def test_repo_url_no_github_fallback_for_non_github_platform(self):
        # a blank git_url on a non-github row yields no URL (nothing to key on)
        assert _repo_url("", "some/thing", "gitlab") == ""

    def test_repo_url_empty_when_nothing(self):
        assert _repo_url("", "", "github") == ""


class TestLoadRepos:
    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "value.csv"
        p.write_text(
            "repo,platform,repo_id,git_url,git_valid,top_eco,top_eco_pkg,class\n" + body,
            encoding="utf-8")
        return p

    def test_filters_class_requires_pkg_and_mapped_eco_dedups(self, tmp_path):
        vf = self._write(tmp_path, (
            "expressjs/express,github,gh/111,,True,npm,express,A\n"
            "a/b,github,gh/222,,True,npm,,A\n"           # no top_eco_pkg → skip
            "c/d,github,gh/333,,True,go,thing,A\n"       # unmapped eco → skip
            "e/f,github,gh/444,,True,pypi,six,B\n"       # class B → skip when A
            "dup/one,github,gh/111,https://github.com/expressjs/express.git,True,npm,express,A\n"  # dup url
        ))
        repos = load_repos({"A"}, value_file=vf)
        # repo_id is read straight from value.csv, no derivation
        assert [(r.package, r.ecosystem, r.repo_id) for r in repos] \
            == [("express", "npm", "gh/111")]
        # class None → includes the B row too
        pkgs = sorted(r.package for r in load_repos(None, value_file=vf))
        assert pkgs == ["express", "six"]


class TestExtract:
    def test_pulls_rankings_critical_dependents(self):
        payload = {"name": "express", "critical": True,
                   "rankings": {"average": 0.079}, "dependent_repos_count": 1853938,
                   "dependent_packages_count": 93237}
        f = extract(payload)
        assert f["critical"] == "True"
        assert f["rank_average"] == "0.079"
        assert f["dependent_repos_count"] == "1853938"
        assert f["dependent_packages_count"] == "93237"

    def test_missing_fields_blank_not_zero_confusion(self):
        # critical absent → "" (unknown), NOT "False"; rank absent → "";
        # dependent counts absent → "" (unknown), NOT "0".
        f = extract({"name": "x"})
        assert f["critical"] == ""
        assert f["rank_average"] == ""
        assert f["dependent_repos_count"] == ""
        assert f["dependent_packages_count"] == ""

    def test_real_zero_dependents_kept_distinct_from_absent(self):
        # a genuine 0 renders "0", never blank — distinct from the absent case.
        f = extract({"name": "x", "critical": False, "dependent_repos_count": 0,
                     "dependent_packages_count": 0})
        assert f["critical"] == "False"
        assert f["dependent_repos_count"] == "0"
        assert f["dependent_packages_count"] == "0"


# ── fake session for _fetch_one ──────────────────────────────────────────────


class FakeResp:
    def __init__(self, status, body=None, raise_json=False):
        self.status = status
        self._body = body or {}
        self._raise_json = raise_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if self._raise_json:
            raise ValueError("malformed JSON")
        return self._body


class FakeSession:
    """Serves scripted bodies keyed by a substring of the request URL."""
    def __init__(self, by_registry: dict[str, object]):
        self.by_registry = by_registry
        self.calls: list[str] = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for reg, resp in self.by_registry.items():
            if f"/registries/{reg}/" in url:
                return resp
        return FakeResp(404)


class TestFetchOneRegistryFallback:
    async def test_cpp_prefers_spack_then_falls_back_to_debian(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)  # cache writes to tmp
        repo = Repo(repo_id="gl/salsa.debian.org/1",
                    repository_url="https://salsa.debian.org/debian/bzip2",
                    cls="A", valid="True", ecosystem="cpp", package="bzip2")
        # spack has it with a real rank → debian never queried
        sess = FakeSession({"spack.io": FakeResp(200, {
            "name": "bzip2", "rankings": {"average": 0.375},
            "dependent_repos_count": 0, "critical": None})})
        res = await _fetch_one(sess, repo, asyncio.Semaphore(1))
        assert res.ok and res.registry_hit == "spack.io"
        assert res.rank_average == "0.375"
        assert any("/registries/spack.io/" in c for c in sess.calls)
        assert not any("/registries/debian-13/" in c for c in sess.calls)

    async def test_cpp_falls_back_when_spack_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
        repo = Repo(repo_id="", repository_url="https://x/ca-certificates",
                    cls="A", valid="True", ecosystem="cpp", package="ca-certificates")
        # spack/conan/vcpkg 404, debian-13 has it (rank 100)
        sess = FakeSession({"debian-13": FakeResp(200, {
            "name": "ca-certificates", "rankings": {"average": 100},
            "dependent_repos_count": 0, "critical": None})})
        res = await _fetch_one(sess, repo, asyncio.Semaphore(1))
        assert res.ok and res.registry_hit == "debian-13"
        assert res.rank_average == "100"

    async def test_not_found_anywhere_sets_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
        repo = Repo(repo_id="", repository_url="https://x/gnumpc",
                    cls="A", valid="True", ecosystem="cpp", package="gnumpc")
        sess = FakeSession({})  # every registry 404s
        res = await _fetch_one(sess, repo, asyncio.Semaphore(1))
        assert res.ok is False
        assert res.error == "not-found-in-any-registry"

    async def test_transient_500_is_fetch_error_not_missed(self, monkeypatch, tmp_path):
        # A 5xx on a registry must be recorded as a retryable fetch-error, NOT
        # conflated with a genuine not-found — the auditability invariant.
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
        repo = Repo(repo_id="gh/1", repository_url="https://github.com/x/y",
                    cls="A", valid="True", ecosystem="pypi", package="six")
        sess = FakeSession({"pypi.org": FakeResp(500)})   # only registry, 500s
        res = await _fetch_one(sess, repo, asyncio.Semaphore(1))
        assert res.ok is False
        assert res.error == "fetch-error"
        # single registry + one retry ⇒ two attempts
        assert sum("/registries/pypi.org/" in c for c in sess.calls) == 2

    async def test_malformed_json_is_fetch_error_and_does_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
        repo = Repo(repo_id="gh/1", repository_url="https://github.com/x/y",
                    cls="A", valid="True", ecosystem="npm", package="left-pad")
        sess = FakeSession({"npmjs.org": FakeResp(200, raise_json=True)})
        res = await _fetch_one(sess, repo, asyncio.Semaphore(1))  # must not raise
        assert res.ok is False and res.error == "fetch-error"


class TestPackageOverrides:
    def test_override_rewrites_package_name_by_url(self, tmp_path, monkeypatch):
        vf = tmp_path / "value.csv"
        vf.write_text(
            "repo,platform,repo_id,git_url,git_valid,top_eco,top_eco_pkg,class\n"
            "mpc/mpc,gitlab,gl/gitlab.inria.fr-33626,https://gitlab.inria.fr/mpc/mpc.git,False,cpp,gnumpc,A\n"  # bad value name
            "expressjs/express,github,gh/1,,True,npm,express,A\n",                                              # untouched
            encoding="utf-8")
        monkeypatch.setattr(cr, "PACKAGE_OVERRIDES",
                            {"https://gitlab.inria.fr/mpc/mpc": "mpc"})
        repos = {r.repository_url: r for r in cr.load_repos({"A"}, value_file=vf)}
        assert repos["https://gitlab.inria.fr/mpc/mpc"].package == "mpc"   # overridden
        # gitlab repo_id read straight from value.csv
        assert repos["https://gitlab.inria.fr/mpc/mpc"].repo_id == "gl/gitlab.inria.fr-33626"
        assert repos["https://github.com/expressjs/express"].package == "express"


class TestLoadReposValidColumn:
    def test_valid_is_carried_from_git_valid(self, tmp_path):
        vf = tmp_path / "value.csv"
        vf.write_text(
            "repo,platform,repo_id,git_url,git_valid,top_eco,top_eco_pkg,class\n"
            "a/b,github,gh/1,,False,npm,leftpad,A\n"          # not valid, still included
            "c/d,github,gh/2,,True,npm,express,A\n",
            encoding="utf-8")
        repos = {r.package: r for r in cr.load_repos({"A"}, value_file=vf)}
        assert repos["leftpad"].valid == "False"   # invalid class-A kept
        assert repos["express"].valid == "True"


class TestTTLNoOp:
    async def test_fresh_cache_skips_fetch(self, tmp_path, monkeypatch):
        # A fresh cache entry ⇒ run() must not open a session / hit the network.
        monkeypatch.setattr(cr, "DATA_DIR", tmp_path)
        monkeypatch.setattr(cr, "OUT_FILE", tmp_path / "criticality.csv")
        repo = Repo(repo_id="gh/1", repository_url="https://github.com/x/express",
                    cls="A", valid="True", ecosystem="npm", package="express")
        monkeypatch.setattr(cr, "load_repos", lambda classes, value_file=None: [repo])
        # write a fresh cache file where run() will look for it
        cr._write_cached(cr._cache_path("npm", "express"), {
            "ecosystem": "npm", "package": "express", "registry_hit": "npmjs.org",
            "fetched_at": cr._now_iso(),
            "data": {"name": "express", "critical": True,
                     "rankings": {"average": 0.08}, "dependent_repos_count": 999},
        })
        # if a session were opened the test env has no network; assert it isn't
        called = {"n": 0}
        orig = cr.aiohttp.ClientSession
        monkeypatch.setattr(cr.aiohttp, "ClientSession",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1) or orig(*a, **k))
        stats = await cr.run({"A"}, ttl_days=90, concurrency=2)
        assert called["n"] == 0            # no session opened → no fetch
        assert stats.cached == 1 and stats.fetched == 0 and stats.found == 1
