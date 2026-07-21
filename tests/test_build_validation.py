"""Tests for src/value/build_validation.py — the validation rollup.

Covers target extraction from value rows, per-target source aggregation,
the cache→verdict load (with rename double-keying), override `valid` pins,
the hard coverage gate, and the AND-of-targets join into the `git_valid` column.
Synthetic in-memory rows / tmp_path CSVs only — no real `data/` reads.
"""

from __future__ import annotations

import csv

import pytest

from src.value import build_validation as bv


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


# ── collect_targets ──────────────────────────────────────────────────────────

class TestCollectTargets:
    def test_splits_github_vs_git_and_keeps_sources(self):
        rows = [
            {"repo": "Owner/Repo", "platform": "github",
             "git_url": "https://github.com/owner/repo.git",
             "ecosystems": "npm,pypi"},
            {"repo": "", "platform": "",
             "git_url": "https://example.com/x.git",
             "ecosystems": "crates"},
            {"repo": "", "platform": "", "git_url": "", "ecosystems": "npm"},  # orphan
        ]
        t = bv.collect_targets(rows)
        # github row keyed by lowercased slug; its derived github git_url ignored
        assert t[("owner/repo", "github_repo")] == {"npm", "pypi"}
        # non-github git URLs are canonicalized (https→git://) before keying
        assert t[("git://example.com/x.git", "git_url")] == {"crates"}
        # orphan contributes no target
        assert len(t) == 2

    def test_unions_sources_across_rows_for_same_target(self):
        rows = [
            {"repo": "a/b", "platform": "github", "git_url": "", "ecosystems": "npm"},
            {"repo": "A/B", "platform": "github", "git_url": "", "ecosystems": "crates"},
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
                     {"repo": "a/b", "git_url": "", "valid": "True"}}
        bv.apply_overrides(verdicts, overrides)
        assert verdicts[("a/b", "github_repo")]["valid"] is True
        assert verdicts[("a/b", "github_repo")]["checked_at"] == "override"

    def test_pin_without_target_is_skipped(self):
        verdicts = {}
        overrides = {("pkg", "npm"):
                     {"repo": "", "git_url": "", "valid": "True"}}
        bv.apply_overrides(verdicts, overrides)
        assert verdicts == {}

    def test_override_without_pin_does_not_touch_verdicts(self):
        verdicts = {("a/b", "github_repo"): {"valid": False, "checked_at": "t"}}
        overrides = {("pkg", "npm"):
                     {"repo": "a/b", "git_url": "", "valid": ""}}
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
        # sorted by (type, target) — "git_url" sorts before "github_repo".
        assert [r["target"] for r in rows] == ["u", "a/b"]
        gh_row = next(r for r in rows if r["type"] == "github_repo")
        assert gh_row == {"target": "a/b", "type": "github_repo",
                          "sources": "npm", "checked_at": "t1", "valid": "True"}

    def test_hard_gate_raises_naming_missing_target(self):
        targets = {("a/b", "github_repo"): {"npm"}}
        with pytest.raises(SystemExit) as e:
            bv.build(targets, verdicts={})
        assert "a/b" in str(e.value)


# ── join_valid (AND of targets, tri-state) ───────────────────────────────────

class TestJoinValid:
    def test_valid_follows_the_row_target_verdict(self):
        """git_valid=True iff the row's target (github_repo OR non-github
        git_url) is reachable — host-agnostic. A github_repo that 404s → False;
        a reachable non-github git_url → True; an unreachable one → False; an
        orphan (no target) → False."""
        target_valid = {
            ("a/b", "github_repo"): True,
            ("c/d", "github_repo"): False,
            ("git://sv.gnu.org/x.git", "git_url"): True,    # reachable non-github
            ("git://dead.example/y.git", "git_url"): False,  # unreachable non-github
        }
        rows = [
            {"repo": "a/b", "platform": "github",
             "git_url": "https://github.com/a/b.git"},   # valid github → True
            {"repo": "c/d", "platform": "github",
             "git_url": "https://github.com/c/d.git"},   # github 404 → False
            {"repo": "gnu/x", "platform": "custom",
             "git_url": "git://sv.gnu.org/x.git"},       # reachable non-github → True
            {"repo": "dead/y", "platform": "custom",
             "git_url": "git://dead.example/y.git"},     # unreachable non-github → False
            {"repo": "", "platform": "", "git_url": ""},  # orphan → False
        ]
        out = bv.join_valid(rows, target_valid)
        assert [r["git_valid"] for r in out] == ["True", "False", "True", "False", "False"]


# ── _verify_non_github integration (Part A) ──────────────────────────────────

class TestVerifyNonGithubIntegration:
    """main() must always call _verify_non_github with non-github URLs before
    the rollup. There is no flag that skips it — the 365-day TTL inside
    _verify_non_github is what makes a warm re-run a no-op."""

    def _make_value_csv(self, tmp_path):
        """Write a minimal value.csv with one github and one non-github row."""
        p = tmp_path / "value.csv"
        _write(p,
               ["repo", "platform", "git_url", "git_valid", "ecosystems",
                "packages", "top_eco", "top_eco_pkg", "top_eco_pct", "class",
                "repo_id", "canonical_url",
                "class_npm", "class_pypi", "class_crates", "class_cpp"],
               [
                   {"repo": "a/b", "platform": "github",
                    "git_url": "https://github.com/a/b.git",
                    "git_valid": "", "ecosystems": "npm", "packages": "1",
                    "top_eco": "npm", "top_eco_pkg": "pkg", "top_eco_pct": "99",
                    "class": "A", "repo_id": "", "canonical_url": "",
                    "class_npm": "A", "class_pypi": "", "class_crates": "", "class_cpp": ""},
                   {"repo": "", "platform": "gitlab",
                    "git_url": "https://gitlab.com/x/y.git",
                    "git_valid": "", "ecosystems": "npm", "packages": "1",
                    "top_eco": "npm", "top_eco_pkg": "pkg2", "top_eco_pct": "50",
                    "class": "B", "repo_id": "", "canonical_url": "",
                    "class_npm": "B", "class_pypi": "", "class_crates": "", "class_cpp": ""},
               ])
        return p

    def _make_caches(self, tmp_path):
        """Write stub github/repos.csv and git/urls.csv so hard gate passes."""
        gh = tmp_path / "repos.csv"
        _write(gh, ["repo", "valid", "full_name", "fetched_at"],
               [{"repo": "a/b", "valid": "True",
                 "full_name": "a/b", "fetched_at": "2026-01-01T00:00:00Z"}])
        git = tmp_path / "urls.csv"
        _write(git, ["url", "valid", "method", "checked_at"],
               [{"url": "https://gitlab.com/x/y.git", "valid": "True",
                 "method": "ls-remote", "checked_at": "2026-01-01T00:00:00Z"}])
        return gh, git

    def _make_stub_verdicts(self):
        """Verdicts covering the rows in _make_value_csv (passes the hard gate)."""
        return {
            ("a/b", "github_repo"): {"valid": True, "checked_at": "2026-01-01T00:00:00Z"},
            ("https://gitlab.com/x/y.git", "git_url"): {"valid": True, "checked_at": "2026-01-01T00:00:00Z"},
        }

    def test_main_calls_verify_non_github_with_nongithub_urls(
            self, tmp_path, monkeypatch):
        """main() collects non-github git_urls and passes them to
        _verify_non_github before the rollup."""
        import sys
        value_csv = self._make_value_csv(tmp_path)
        captured: list = []

        def fake_verify(urls, force=False):
            captured.append({"urls": list(urls), "force": force})
            return {}

        monkeypatch.setattr(bv, "_verify_non_github", fake_verify)
        monkeypatch.setattr(bv, "VALUE_FILE", value_csv)
        monkeypatch.setattr(bv, "VALIDATION_FILE", tmp_path / "validation.csv")
        monkeypatch.setattr(sys, "argv", ["build_validation"])

        stub_verdicts = self._make_stub_verdicts()
        monkeypatch.setattr(bv, "load_verdicts", lambda: stub_verdicts)
        monkeypatch.setattr(bv, "load_repo_overrides", lambda: {})
        monkeypatch.setattr(bv, "write_value_data", lambda rows: None)

        bv.main()

        assert len(captured) == 1
        assert "https://gitlab.com/x/y.git" in captured[0]["urls"]
        # github URL is excluded
        assert "https://github.com/a/b.git" not in captured[0]["urls"]
        assert captured[0]["force"] is False

    def test_verify_non_github_always_runs(self, tmp_path, monkeypatch):
        """No flag suppresses the reachability check.

        --offline used to skip it, which let a run produce a validation table
        built purely from whatever happened to be cached. The TTL inside
        _verify_non_github already makes a warm re-run free, so the call is
        now unconditional.
        """
        import sys
        value_csv = self._make_value_csv(tmp_path)
        called: list = []

        def fake_verify(urls, force=False):
            called.append(True)
            return {}

        monkeypatch.setattr(bv, "_verify_non_github", fake_verify)
        monkeypatch.setattr(bv, "VALUE_FILE", value_csv)
        monkeypatch.setattr(bv, "VALIDATION_FILE", tmp_path / "validation.csv")
        monkeypatch.setattr(sys, "argv", ["build_validation"])

        stub_verdicts = self._make_stub_verdicts()
        monkeypatch.setattr(bv, "load_verdicts", lambda: stub_verdicts)
        monkeypatch.setattr(bv, "load_repo_overrides", lambda: {})
        monkeypatch.setattr(bv, "write_value_data", lambda rows: None)

        bv.main()

        assert called == [True], "_verify_non_github must always run"

    def test_main_passes_force_true_when_refresh(self, tmp_path, monkeypatch):
        """--refresh passes force=True to _verify_non_github."""
        import sys
        value_csv = self._make_value_csv(tmp_path)
        captured: list = []

        def fake_verify(urls, force=False):
            captured.append(force)
            return {}

        monkeypatch.setattr(bv, "_verify_non_github", fake_verify)
        monkeypatch.setattr(bv, "VALUE_FILE", value_csv)
        monkeypatch.setattr(bv, "VALIDATION_FILE", tmp_path / "validation.csv")
        monkeypatch.setattr(sys, "argv", ["build_validation", "--refresh"])

        stub_verdicts = self._make_stub_verdicts()
        monkeypatch.setattr(bv, "load_verdicts", lambda: stub_verdicts)
        monkeypatch.setattr(bv, "load_repo_overrides", lambda: {})
        monkeypatch.setattr(bv, "write_value_data", lambda rows: None)

        bv.main()

        assert captured == [True]
