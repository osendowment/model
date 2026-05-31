"""Tests for src/pipeline/value/build_validation.py — the validation rollup.

Covers target extraction from value rows, per-target source aggregation,
the cache→verdict load (with rename double-keying), override `valid` pins,
the hard coverage gate, and the AND-of-targets join into the `valid` column.
Synthetic in-memory rows / tmp_path CSVs only — no real `data/` reads.
"""

from __future__ import annotations

import csv

import pytest

from src.pipeline.value import build_validation as bv


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


# ── collect_targets ──────────────────────────────────────────────────────────

class TestCollectTargets:
    def test_splits_github_vs_git_and_keeps_sources(self):
        rows = [
            {"github_repo": "Owner/Repo",
             "git_url": "https://github.com/owner/repo.git",
             "ecosystems": "npm,pypi"},
            {"github_repo": "", "git_url": "https://example.com/x.git",
             "ecosystems": "crates"},
            {"github_repo": "", "git_url": "", "ecosystems": "npm"},  # orphan
        ]
        t = bv.collect_targets(rows)
        # github row keyed by lowercased slug; its derived github git_url ignored
        assert t[("owner/repo", "github_repo")] == {"npm", "pypi"}
        assert t[("https://example.com/x.git", "git_url")] == {"crates"}
        # orphan contributes no target
        assert len(t) == 2

    def test_unions_sources_across_rows_for_same_target(self):
        rows = [
            {"github_repo": "a/b", "git_url": "", "ecosystems": "npm"},
            {"github_repo": "A/B", "git_url": "", "ecosystems": "crates"},
        ]
        t = bv.collect_targets(rows)
        assert t[("a/b", "github_repo")] == {"npm", "crates"}


# ── load_verdicts ────────────────────────────────────────────────────────────

class TestLoadVerdicts:
    def test_keys_github_by_repo_and_full_name(self, tmp_path):
        gh = tmp_path / "repos.csv"
        _write(gh, ["repo", "valid", "full_name", "fetched_at"], [
            {"repo": "stale/slug", "valid": "True", "full_name": "fresh/slug",
             "fetched_at": "2026-01-01T00:00:00Z"},
        ])
        git = tmp_path / "urls.csv"
        _write(git, ["url", "valid", "method", "checked_at"], [
            {"url": "https://example.com/x.git", "valid": "False",
             "method": "ls-remote", "checked_at": "2026-02-02T00:00:00Z"},
        ])
        v = bv.load_verdicts(gh_path=gh, git_path=git)
        # both the asked slug and the renamed full_name resolve to the verdict
        assert v[("stale/slug", "github_repo")]["valid"] is True
        assert v[("fresh/slug", "github_repo")]["valid"] is True
        assert v[("https://example.com/x.git", "git_url")]["valid"] is False
        assert v[("https://example.com/x.git", "git_url")]["checked_at"] == \
            "2026-02-02T00:00:00Z"


# ── apply_overrides ──────────────────────────────────────────────────────────

class TestApplyOverrides:
    def test_valid_pin_overrides_cache_verdict(self):
        verdicts = {("a/b", "github_repo"): {"valid": False, "checked_at": "t"}}
        overrides = {("pkg", "npm"):
                     {"github_repo": "a/b", "git_url": "", "valid": "True"}}
        bv.apply_overrides(verdicts, overrides)
        assert verdicts[("a/b", "github_repo")]["valid"] is True
        assert verdicts[("a/b", "github_repo")]["checked_at"] == "override"

    def test_pin_without_target_is_skipped(self):
        verdicts = {}
        overrides = {("pkg", "npm"):
                     {"github_repo": "", "git_url": "", "valid": "True"}}
        bv.apply_overrides(verdicts, overrides)
        assert verdicts == {}

    def test_override_without_pin_does_not_touch_verdicts(self):
        verdicts = {("a/b", "github_repo"): {"valid": False, "checked_at": "t"}}
        overrides = {("pkg", "npm"):
                     {"github_repo": "a/b", "git_url": "", "valid": ""}}
        bv.apply_overrides(verdicts, overrides)
        assert verdicts[("a/b", "github_repo")]["valid"] is False


# ── build (hard gate + validation rows) ──────────────────────────────────────

class TestBuild:
    def test_builds_rows_and_valid_map(self):
        targets = {("a/b", "github_repo"): {"npm"},
                   ("u", "git_url"): {"pypi"}}
        verdicts = {("a/b", "github_repo"): {"valid": True, "checked_at": "t1"},
                    ("u", "git_url"): {"valid": False, "checked_at": "t2"}}
        rows, tvalid = bv.build(targets, verdicts)
        assert tvalid[("a/b", "github_repo")] is True
        assert tvalid[("u", "git_url")] is False
        # sorted by (type, target): github_repo before git_url
        assert [r["target"] for r in rows] == ["a/b", "u"]
        assert rows[0] == {"target": "a/b", "type": "github_repo",
                           "sources": "npm", "checked_at": "t1", "valid": "True"}

    def test_hard_gate_raises_naming_missing_target(self):
        targets = {("a/b", "github_repo"): {"npm"}}
        with pytest.raises(SystemExit) as e:
            bv.build(targets, verdicts={})
        assert "a/b" in str(e.value)


# ── join_valid (AND of targets, tri-state) ───────────────────────────────────

class TestJoinValid:
    def test_tri_state(self):
        target_valid = {("a/b", "github_repo"): True, ("u", "git_url"): False}
        rows = [
            {"github_repo": "a/b", "git_url": "https://github.com/a/b.git"},
            {"github_repo": "", "git_url": "u"},
            {"github_repo": "", "git_url": ""},  # orphan
        ]
        out = bv.join_valid(rows, target_valid)
        assert out[0]["valid"] == "True"
        assert out[1]["valid"] == "False"
        assert out[2]["valid"] == ""
