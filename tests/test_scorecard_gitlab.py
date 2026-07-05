"""Tests for the GitLab mode of src/sources/openssf/scorecard.

Covers the two GitLab-specific behaviours, with subprocess mocked so no
network / scorecard binary is touched:

  * ``run_scorecard(..., tolerate_partial=True)`` recovers the valid JSON
    scorecard emits on a *non-zero* exit (a single check errored, e.g. SAST's
    403 on GitLab's pipelines API) instead of discarding the whole repo — while
    the default (GitHub) path still treats a non-zero exit as an error.
  * ``load_gitlab_targets`` selects only valid, repo_id-bearing rows and honours
    the host filter.
  * token/checks plumbing: the GitLab path passes the token through
    ``GITLAB_AUTH_TOKEN`` and selects ``GITLAB_SCORECARD_CHECKS``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import src.sources.openssf.scorecard as sc
from src.sources.openssf.scorecard import (
    GITLAB_SCORECARD_CHECKS,
    load_gitlab_targets,
    run_scorecard,
)

# A trimmed but structurally faithful scorecard JSON payload.
_PARTIAL_JSON = json.dumps({
    "date": "2026-07-05T00:00:00Z",
    "repo": {"name": "gitlab.com/gnutls/libtasn1",
             "commit": "2f560e2b0ddca402f8b19d8a7cfd02871ea26927"},
    "score": 4.2,
    "checks": [
        {"name": "License", "score": 10},
        {"name": "Maintained", "score": 7},
    ],
})


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, *, returncode, stdout, stderr=""):
    """Patch subprocess.run + the binary lookup; capture the call args."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeCompleted(returncode, stdout, stderr)

    monkeypatch.setattr(sc, "_scorecard_bin", lambda: "scorecard")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


class TestRunScorecardPartial:
    def test_tolerate_partial_recovers_json_on_nonzero_exit(self, monkeypatch):
        _patch_run(monkeypatch, returncode=1, stdout=_PARTIAL_JSON,
                   stderr="Check SAST failed: 403 Forbidden ... /pipelines")
        data = run_scorecard("gitlab.com/gnutls/libtasn1", "glpat-x",
                             token_env="GITLAB_AUTH_TOKEN",
                             checks=GITLAB_SCORECARD_CHECKS,
                             tolerate_partial=True)
        assert not data.get("error")
        assert data["score"] == 4.2
        assert data["repo"]["commit"].startswith("2f560e2b")

    def test_default_path_discards_partial_on_nonzero_exit(self, monkeypatch):
        # GitHub behaviour is unchanged: a non-zero exit is an error, even
        # though stdout holds valid JSON.
        _patch_run(monkeypatch, returncode=1, stdout=_PARTIAL_JSON,
                   stderr="boom")
        data = run_scorecard("npm/cli", "ghp-x")
        assert data.get("error") is True
        assert "boom" in data["message"]

    def test_tolerate_partial_still_errors_on_nonjson(self, monkeypatch):
        _patch_run(monkeypatch, returncode=1, stdout="not json at all",
                   stderr="fatal")
        data = run_scorecard("gitlab.com/x/y", "glpat-x",
                             token_env="GITLAB_AUTH_TOKEN",
                             tolerate_partial=True)
        assert data.get("error") is True

    def test_tolerate_partial_still_errors_on_empty_stdout(self, monkeypatch):
        # A non-zero exit with no stdout at all is a genuine failure — the
        # recovery path must not turn it into a silent success.
        _patch_run(monkeypatch, returncode=1, stdout="   \n",
                   stderr="crashed before emitting JSON")
        data = run_scorecard("gitlab.com/x/y", "glpat-x",
                             token_env="GITLAB_AUTH_TOKEN",
                             tolerate_partial=True)
        assert data.get("error") is True
        assert "crashed" in data["message"]

    def test_tolerate_partial_ignores_scoreless_json(self, monkeypatch):
        # A JSON object with neither a score nor checks is not a usable result.
        _patch_run(monkeypatch, returncode=1,
                   stdout=json.dumps({"repo": {"commit": "abc"}}), stderr="x")
        data = run_scorecard("gitlab.com/x/y", "glpat-x",
                             token_env="GITLAB_AUTH_TOKEN",
                             tolerate_partial=True)
        assert data.get("error") is True

    def test_zero_exit_valid_json_unaffected(self, monkeypatch):
        _patch_run(monkeypatch, returncode=0, stdout=_PARTIAL_JSON)
        data = run_scorecard("gitlab.com/gnutls/libtasn1", "glpat-x",
                             token_env="GITLAB_AUTH_TOKEN",
                             tolerate_partial=True)
        assert not data.get("error")
        assert data["score"] == 4.2

    def test_token_env_and_checks_plumbing(self, monkeypatch):
        captured = _patch_run(monkeypatch, returncode=0, stdout=_PARTIAL_JSON)
        run_scorecard("gitlab.com/g/p", "glpat-secret",
                      token_env="GITLAB_AUTH_TOKEN",
                      checks=GITLAB_SCORECARD_CHECKS,
                      tolerate_partial=True)
        # token routed through GITLAB_AUTH_TOKEN, not GITHUB_AUTH_TOKEN
        assert captured["env"]["GITLAB_AUTH_TOKEN"] == "glpat-secret"
        # GitLab check subset selected on the command line
        assert f"--checks={GITLAB_SCORECARD_CHECKS}" in captured["cmd"]
        assert "--repo" in captured["cmd"]
        assert "gitlab.com/g/p" in captured["cmd"]


class TestLoadGitlabTargets:
    def _write(self, tmp_path: Path, rows: str) -> Path:
        p = tmp_path / "projects.csv"
        p.write_text(
            "project,host,repo_id,valid\n" + rows, encoding="utf-8")
        return p

    def test_selects_valid_repoid_rows_only(self, tmp_path, monkeypatch):
        csv_path = self._write(tmp_path, (
            "gitlab.com/gnutls/gnutls,gitlab.com,gl/gitlab.com/1,True\n"
            "gitlab.com/gone/x,gitlab.com,gl/gitlab.com/2,False\n"        # invalid
            "gitlab.com/no/id,gitlab.com,,True\n"                          # no repo_id
            "salsa.debian.org/debian/foo,salsa.debian.org,gl/salsa.debian.org/9,True\n"
        ))
        monkeypatch.setattr(sc, "GITLAB_PROJECTS", csv_path)
        targets = load_gitlab_targets()
        projects = sorted(t["project"] for t in targets)
        assert projects == ["gitlab.com/gnutls/gnutls",
                            "salsa.debian.org/debian/foo"]

    def test_host_filter(self, tmp_path, monkeypatch):
        csv_path = self._write(tmp_path, (
            "gitlab.com/a/b,gitlab.com,gl/gitlab.com/1,True\n"
            "salsa.debian.org/c/d,salsa.debian.org,gl/salsa.debian.org/2,True\n"
        ))
        monkeypatch.setattr(sc, "GITLAB_PROJECTS", csv_path)
        targets = load_gitlab_targets(hosts={"gitlab.com"})
        assert [t["project"] for t in targets] == ["gitlab.com/a/b"]
        assert targets[0]["repo_id"] == "gl/gitlab.com/1"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "GITLAB_PROJECTS", tmp_path / "nope.csv")
        assert load_gitlab_targets() == []


class TestRunGitlabOrchestration:
    """_run_gitlab's target-selection: drop hosts with no token, then (unless
    --force) drop already-scored projects, before handing off to
    fetch_all_gitlab. fetch_all_gitlab itself is stubbed — no subprocess."""

    def _wire(self, monkeypatch, *, targets, token_map, already_scored):
        import src.sources.gitlab.gitlab_client as glc
        monkeypatch.setattr(sc, "load_gitlab_targets", lambda hosts=None: [
            t for t in targets if not hosts or t["host"] in hosts])
        monkeypatch.setattr(glc, "load_token_map", lambda: dict(token_map))
        monkeypatch.setattr(sc, "load_already_scored", lambda p: set(already_scored))
        monkeypatch.setattr(sc, "display_summary", lambda r: None)
        captured: dict = {}

        async def fake_fetch(t, tm, concurrency=5):
            captured["targets"] = t
            captured["token_map"] = tm
            captured["concurrency"] = concurrency
            return {}

        monkeypatch.setattr(sc, "fetch_all_gitlab", fake_fetch)
        return captured

    def _args(self, **kw):
        import argparse
        base = {"host": None, "force": False, "concurrency": 5,
                "repos": [], "file": None}
        base.update(kw)
        return argparse.Namespace(**base)

    async def test_drops_no_token_host_and_already_scored(self, monkeypatch):
        targets = [
            {"project": "gitlab.com/a/b", "host": "gitlab.com", "repo_id": "gl/gitlab.com/1"},
            {"project": "gitlab.com/c/d", "host": "gitlab.com", "repo_id": "gl/gitlab.com/2"},
            {"project": "salsa.debian.org/x/y", "host": "salsa.debian.org",
             "repo_id": "gl/salsa.debian.org/3"},
        ]
        captured = self._wire(monkeypatch, targets=targets,
                              token_map={"gitlab.com": "glpat-x"},       # no salsa token
                              already_scored={"gitlab.com/a/b"})         # a/b already done
        await sc._run_gitlab(self._args())
        got = sorted(t["project"] for t in captured["targets"])
        assert got == ["gitlab.com/c/d"]        # salsa dropped (no token), a/b dropped (scored)
        assert captured["concurrency"] == 5

    async def test_force_keeps_already_scored(self, monkeypatch):
        targets = [
            {"project": "gitlab.com/a/b", "host": "gitlab.com", "repo_id": "gl/gitlab.com/1"},
            {"project": "gitlab.com/c/d", "host": "gitlab.com", "repo_id": "gl/gitlab.com/2"},
        ]
        captured = self._wire(monkeypatch, targets=targets,
                              token_map={"gitlab.com": "glpat-x"},
                              already_scored={"gitlab.com/a/b"})
        await sc._run_gitlab(self._args(force=True))
        got = sorted(t["project"] for t in captured["targets"])
        assert got == ["gitlab.com/a/b", "gitlab.com/c/d"]   # --force keeps the scored one

    async def test_no_token_at_all_makes_no_fetch(self, monkeypatch):
        targets = [{"project": "gitlab.com/a/b", "host": "gitlab.com",
                    "repo_id": "gl/gitlab.com/1"}]
        captured = self._wire(monkeypatch, targets=targets,
                              token_map={},                    # no tokens for any host
                              already_scored=set())
        await sc._run_gitlab(self._args())
        assert "targets" not in captured        # early return: fetch_all_gitlab never called
