"""Unit tests for the newer pipeline_health checks + heal_repo_id_drift.

Both scripts live in scripts/ (not a package), so they are loaded via
importlib (same pattern as tests/test_audit_scripts.py). Every check reads
paths anchored on the module-global `ROOT`, so each test monkeypatches
`ROOT` at a tmp_path holding a minimal fixture data tree — no live data is
touched. Checks that scope by `load_top_repos` import it INSIDE the
function (`from src.common.repos import load_top_repos`), so patching the
attribute on `src.common.repos` is enough.
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load_script(filename: str):
    """Import a scripts/*.py file as a fresh module (scripts/ isn't a package)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        f"_phc_{filename[:-3]}", SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ph():
    """A fresh pipeline_health module per test (safe to monkeypatch)."""
    return _load_script("pipeline_health.py")


@pytest.fixture
def heal():
    """A fresh heal_repo_id_drift module per test."""
    return _load_script("heal_repo_id_drift.py")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _patch_scope(monkeypatch, slugs: list[str]) -> None:
    """load_top_repos → fixed slug list (checks only touch `.repo`)."""
    import src.common.repos as repos
    monkeypatch.setattr(
        repos, "load_top_repos",
        lambda *a, **k: [SimpleNamespace(repo=s) for s in slugs])


def _result(results, label):
    """(label, ok, detail) row with the given label — must be unique."""
    matches = [r for r in results if r[0] == label]
    assert len(matches) == 1, f"expected exactly one {label!r} row, got {results}"
    return matches[0]


# ── check_repo_id_shape ──────────────────────────────────────────────────────

class TestRepoIdShape:
    def test_canonical_shapes_pass(self, ph, monkeypatch, tmp_path):
        _write_csv(tmp_path / "data" / "sources" / "x" / "ids.csv",
                   ["repo", "repo_id"],
                   [{"repo": "a/a", "repo_id": "gh/123"},
                    {"repo": "b/b", "repo_id": "gl/456"},          # gitlab.com
                    {"repo": "c/c", "repo_id": "gl/debian-789"},   # nicknamed host
                    {"repo": "d/d", "repo_id": ""}])               # blank allowed
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        results = ph.check_repo_id_shape()
        label, ok, detail = _result(results, "repo_id shape")
        assert ok, detail
        assert len(results) == 1          # no per-file failure rows
        assert "1 repo_id-keyed files" in detail

    def test_non_canonical_shapes_fail(self, ph, monkeypatch, tmp_path):
        _write_csv(tmp_path / "data" / "sources" / "x" / "ids.csv",
                   ["repo", "repo_id"],
                   [{"repo": "a/a", "repo_id": "123"},                    # bare numeric
                    {"repo": "b/b", "repo_id": "gl/salsa.debian.org-1"},  # host, not nickname
                    {"repo": "c/c", "repo_id": "gh/x1"}])                 # garbage
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        results = ph.check_repo_id_shape()
        _, ok, _ = _result(results, "repo_id shape")
        assert not ok
        # per-file label is relative to ROOT (keeps the data/ prefix)
        _, file_ok, detail = _result(results, "data/sources/x/ids.csv:id-shape")
        assert not file_ok
        assert "3 non-canonical repo_id(s)" in detail
        for bad in ("123", "gl/salsa.debian.org-1", "gh/x1"):
            assert bad in detail

    def test_files_without_repo_id_column_are_skipped(self, ph, monkeypatch, tmp_path):
        _write_csv(tmp_path / "data" / "vendor.csv", ["package", "downloads"],
                   [{"package": "p", "downloads": "1"}])
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        label, ok, detail = _result(ph.check_repo_id_shape(), "repo_id shape")
        assert ok
        assert "0 repo_id-keyed files" in detail


# ── check_fetch_date_sanity ──────────────────────────────────────────────────

class TestFetchDateSanity:
    def _run(self, ph, monkeypatch, tmp_path, dates, contract=None):
        rel = "sources/test/dated.csv"
        _write_csv(tmp_path / "data" / rel, ["repo", "repo_id", "fetched_at"],
                   [{"repo": f"o/r{i}", "repo_id": f"gh/{i + 1}", "fetched_at": d}
                    for i, d in enumerate(dates)])
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        monkeypatch.setattr(ph, "SOURCE_SCHEMA_CONTRACT",
                            contract or {rel: "fetched_at"})
        return ph.check_fetch_date_sanity()

    def test_iso_dates_and_blanks_pass(self, ph, monkeypatch, tmp_path):
        results = self._run(ph, monkeypatch, tmp_path, [
            "2024-01-02T03:04:05Z",         # ISO with Z
            "2024-01-02T03:04:05+00:00",    # ISO with offset
            "2024-01-02 03:04:05.123456",   # naive, space separator
            "2024-01-02",                   # naive date-only
            "",                             # blank cell — rows predate tracking
        ])
        label, ok, detail = _result(results, "fetch dates sane")
        assert ok, detail
        assert len(results) == 1            # no per-file failure rows

    def test_malformed_and_future_dates_fail(self, ph, monkeypatch, tmp_path):
        results = self._run(ph, monkeypatch, tmp_path, [
            "not-a-date",             # malformed
            "2200-01-01T00:00:00Z",   # future-dated (freshness pin)
            "2024-01-02T03:04:05Z",   # a good cell doesn't mask the bad ones
        ])
        _, ok, _ = _result(results, "fetch dates sane")
        assert not ok
        _, file_ok, detail = _result(results, "sources/test/dated.csv:fetched_at")
        assert not file_ok
        assert "1 malformed" in detail and "'not-a-date'" in detail
        assert "1 future-dated" in detail and "2200-01-01" in detail

    def test_sidecar_contract_entries_check_the_sidecar(self, ph, monkeypatch, tmp_path):
        side_rel = "sources/test/dated.status.csv"
        _write_csv(tmp_path / "data" / side_rel, ["repo", "fetched_at"],
                   [{"repo": "o/r", "fetched_at": "2200-01-01T00:00:00Z"}])
        contract = {"sources/test/main.csv": ("sidecar", side_rel, "fetched_at")}
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        monkeypatch.setattr(ph, "SOURCE_SCHEMA_CONTRACT", contract)
        results = ph.check_fetch_date_sanity()
        _, ok, _ = _result(results, "fetch dates sane")
        assert not ok
        _, file_ok, detail = _result(results, f"{side_rel}:fetched_at")
        assert not file_ok and "future-dated" in detail


# ── check_source_value_id_agreement ──────────────────────────────────────────

class TestSourceValueIdAgreement:
    REL = "sources/test/joined.csv"

    def _run(self, ph, monkeypatch, tmp_path, source_rows, value_rows, scope):
        _write_csv(tmp_path / "data" / self.REL, ["repo", "repo_id", "metric"],
                   source_rows)
        _write_csv(tmp_path / "data" / "value" / "value.csv",
                   ["repo", "repo_id"], value_rows)
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        monkeypatch.setattr(ph, "ID_JOINED_SOURCES", [self.REL])
        _patch_scope(monkeypatch, scope)
        return ph.check_source_value_id_agreement()

    def test_matching_id_is_ok(self, ph, monkeypatch, tmp_path):
        results = self._run(
            ph, monkeypatch, tmp_path,
            source_rows=[{"repo": "a/a", "repo_id": "gh/1", "metric": "m"}],
            value_rows=[{"repo": "a/a", "repo_id": "gh/1"}],
            scope=["a/a"])
        label, ok, detail = _result(results, f"{self.REL}:id agreement")
        assert ok, detail
        assert "1 in-scope slugs agree" in detail
        assert "stale extra" not in detail

    def test_only_a_different_id_fails(self, ph, monkeypatch, tmp_path):
        # Source stamped the GitHub-mirror id; value.csv points at the GitLab
        # home — every join for b/b silently drops. Must FAIL.
        results = self._run(
            ph, monkeypatch, tmp_path,
            source_rows=[{"repo": "b/b", "repo_id": "gh/999", "metric": "m"}],
            value_rows=[{"repo": "b/b", "repo_id": "gl/2"}],
            scope=["b/b"])
        _, ok, detail = _result(results, f"{self.REL}:id agreement")
        assert not ok
        assert "1 in-scope slug(s) with no row under the value.csv id" in detail
        assert "b/b" in detail

    def test_mixed_slug_is_ok_but_counted_stale_extra(self, ph, monkeypatch, tmp_path):
        # c/c has rows under BOTH the current and a stale id: joins fine
        # (OK), but the stale rows are surfaced as dead weight in the detail.
        results = self._run(
            ph, monkeypatch, tmp_path,
            source_rows=[{"repo": "c/c", "repo_id": "gh/3", "metric": "m"},
                         {"repo": "c/c", "repo_id": "gh/888", "metric": "m2"}],
            value_rows=[{"repo": "c/c", "repo_id": "gh/3"}],
            scope=["c/c"])
        _, ok, detail = _result(results, f"{self.REL}:id agreement")
        assert ok, detail
        assert "1 slug(s) also carry stale extra ids" in detail


# ── check_value_domains ──────────────────────────────────────────────────────

class TestValueDomains:
    def _run(self, ph, monkeypatch, tmp_path, rows):
        _write_csv(tmp_path / "data" / "value" / "value.csv",
                   ["repo", "platform", "git_valid"], rows)
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        return ph.check_value_domains()

    def test_clean_fixture_passes_both(self, ph, monkeypatch, tmp_path):
        results = self._run(ph, monkeypatch, tmp_path, [
            {"repo": "a/a", "platform": "github", "git_valid": "True"},
            {"repo": "b/b", "platform": "gitlab", "git_valid": "False"},
            {"repo": "c/c", "platform": "", "git_valid": "False"},  # blank platform OK
        ])
        assert _result(results, "value.csv:platform")[1]
        assert _result(results, "value.csv:git_valid")[1]

    def test_stray_platform_and_nonboolean_valid_fail(self, ph, monkeypatch, tmp_path):
        results = self._run(ph, monkeypatch, tmp_path, [
            {"repo": "a/a", "platform": "gitea", "git_valid": "True"},   # stray platform
            {"repo": "b/b", "platform": "github", "git_valid": "yes"},   # non-boolean
        ])
        _, plat_ok, plat_detail = _result(results, "value.csv:platform")
        _, valid_ok, valid_detail = _result(results, "value.csv:git_valid")
        assert not plat_ok and "gitea" in plat_detail
        assert not valid_ok and "yes" in valid_detail


# ── check_npm_downloads_status ───────────────────────────────────────────────

class TestNpmDownloadsStatus:
    CHECKED = "2024-01-02 03:04:05.000001"   # the writer's %Y-%m-%d %H:%M:%S.%f

    def _run(self, ph, monkeypatch, tmp_path, status_rows, dl_rows):
        raw = tmp_path / "data" / "sources" / "npm" / "raw"
        _write_csv(raw / "downloads.status.csv",
                   ["package", "status", "checked_at"], status_rows)
        _write_csv(raw / "downloads.csv",
                   ["package", "year", "downloads"], dl_rows)
        monkeypatch.setattr(ph, "ROOT", tmp_path)
        return ph.check_npm_downloads_status()

    @staticmethod
    def _years():
        from src.common.params import YEARS
        return [str(y) for y in YEARS]

    def _dl_rows(self, package: str, downloads: str) -> list[dict]:
        return [{"package": package, "year": y, "downloads": downloads}
                for y in self._years()]

    def test_consistent_ok_row_passes(self, ph, monkeypatch, tmp_path):
        results = self._run(
            ph, monkeypatch, tmp_path,
            status_rows=[{"package": "p1", "status": "ok",
                          "checked_at": self.CHECKED}],
            dl_rows=self._dl_rows("p1", "10"))
        label, ok, detail = _result(results, "npm downloads.status")
        assert ok, detail
        assert "1 stamped packages consistent" in detail

    def test_bad_status_vocab_fails(self, ph, monkeypatch, tmp_path):
        results = self._run(
            ph, monkeypatch, tmp_path,
            status_rows=[{"package": "p1", "status": "error",
                          "checked_at": self.CHECKED}],
            dl_rows=self._dl_rows("p1", "10"))
        _, ok, detail = _result(results, "npm downloads.status")
        assert not ok and "bad status 'error'" in detail

    def test_not_found_with_nonzero_downloads_fails(self, ph, monkeypatch, tmp_path):
        results = self._run(
            ph, monkeypatch, tmp_path,
            status_rows=[{"package": "p2", "status": "not_found",
                          "checked_at": self.CHECKED}],
            dl_rows=self._dl_rows("p2", "5"))
        _, ok, detail = _result(results, "npm downloads.status")
        assert not ok and "not_found but has nonzero downloads" in detail

    def test_stamped_package_missing_years_fails(self, ph, monkeypatch, tmp_path):
        first_year = self._years()[0]
        results = self._run(
            ph, monkeypatch, tmp_path,
            status_rows=[{"package": "p3", "status": "ok",
                          "checked_at": self.CHECKED}],
            dl_rows=[{"package": "p3", "year": first_year, "downloads": "10"}])
        _, ok, detail = _result(results, "npm downloads.status")
        assert not ok and "stamped but years missing from downloads.csv" in detail

    def test_bad_checked_at_fails(self, ph, monkeypatch, tmp_path):
        results = self._run(
            ph, monkeypatch, tmp_path,
            status_rows=[{"package": "p4", "status": "ok",
                          "checked_at": "yesterday"}],
            dl_rows=self._dl_rows("p4", "10"))
        _, ok, detail = _result(results, "npm downloads.status")
        assert not ok and "bad checked_at" in detail


# ── heal_repo_id_drift ───────────────────────────────────────────────────────
#
# Fixture tree: value.csv assigns a/a → gl/11 (canonical GitLab home) and
# c/c → gh/3. sources/git/scc.csv (a real ID_JOINED_SOURCES entry — the heal
# script re-reads the list from the real pipeline_health.py, and files absent
# from the tmp tree are skipped) holds:
#   - a/a rows stamped gh/1 only  → drifted, restamp to gl/11;
#   - c/c rows under gh/3 AND gh/999 → mixed, untouched.
# sources/github/repos.csv (a BARE_ID_FILES entry) holds a bare numeric id.

class TestHealRepoIdDrift:
    SCC = "sources/git/scc.csv"
    REPOS = "sources/github/repos.csv"

    def _tree(self, heal, monkeypatch, tmp_path) -> Path:
        _write_csv(tmp_path / "data" / "value" / "value.csv",
                   ["repo", "repo_id"],
                   [{"repo": "a/a", "repo_id": "gl/11"},
                    {"repo": "c/c", "repo_id": "gh/3"}])
        _write_csv(tmp_path / "data" / self.SCC,
                   ["repo", "repo_id", "metric"],
                   [{"repo": "a/a", "repo_id": "gh/1", "metric": "m1"},
                    {"repo": "a/a", "repo_id": "gh/1", "metric": "m2"},
                    {"repo": "c/c", "repo_id": "gh/3", "metric": "m1"},
                    {"repo": "c/c", "repo_id": "gh/999", "metric": "m2"}])
        _write_csv(tmp_path / "data" / self.REPOS,
                   ["repo", "repo_id"],
                   [{"repo": "x/x", "repo_id": "123"},     # bare → gh/123
                    {"repo": "y/y", "repo_id": "gh/5"},    # already canonical
                    {"repo": "z/z", "repo_id": ""}])       # blank stays blank
        monkeypatch.setattr(heal, "ROOT", tmp_path)
        _patch_scope(monkeypatch, ["a/a", "c/c"])
        return tmp_path

    def _snapshot(self, tmp_path) -> dict[str, bytes]:
        return {p: (tmp_path / "data" / p).read_bytes()
                for p in (self.SCC, self.REPOS, "value/value.csv")}

    def test_dry_run_reports_but_changes_nothing(self, heal, monkeypatch, tmp_path):
        self._tree(heal, monkeypatch, tmp_path)
        before = self._snapshot(tmp_path)
        bare = dict((rel, n) for rel, n in heal.heal_bare_ids(write=False))
        drift = {rel: (n, slugs) for rel, n, slugs in heal.heal_drift(write=False)}
        assert bare[self.REPOS] == 1                     # would prefix x/x
        assert drift[self.SCC] == (2, ["a/a"])           # would restamp both a/a rows
        assert self._snapshot(tmp_path) == before        # nothing written

    def test_write_prefixes_bare_ids_and_restamps_drifted_only(
            self, heal, monkeypatch, tmp_path):
        self._tree(heal, monkeypatch, tmp_path)
        heal.heal_bare_ids(write=True)
        heal.heal_drift(write=True)

        repos = {r["repo"]: r["repo_id"]
                 for r in _read_csv(tmp_path / "data" / self.REPOS)}
        assert repos == {"x/x": "gh/123", "y/y": "gh/5", "z/z": ""}

        scc = [(r["repo"], r["repo_id"], r["metric"])
               for r in _read_csv(tmp_path / "data" / self.SCC)]
        # drifted-only slug: every row restamped to the value.csv id
        assert scc[0] == ("a/a", "gl/11", "m1")
        assert scc[1] == ("a/a", "gl/11", "m2")
        # mixed slug: untouched — live row keeps gh/3, stale row keeps gh/999
        assert scc[2] == ("c/c", "gh/3", "m1")
        assert scc[3] == ("c/c", "gh/999", "m2")

    def test_second_write_run_is_a_noop(self, heal, monkeypatch, tmp_path):
        self._tree(heal, monkeypatch, tmp_path)
        heal.heal_bare_ids(write=True)
        heal.heal_drift(write=True)
        after_first = self._snapshot(tmp_path)

        bare = dict((rel, n) for rel, n in heal.heal_bare_ids(write=True))
        drift = {rel: n for rel, n, _ in heal.heal_drift(write=True)}
        assert bare[self.REPOS] == 0
        assert drift[self.SCC] == 0
        assert self._snapshot(tmp_path) == after_first   # bytes identical
