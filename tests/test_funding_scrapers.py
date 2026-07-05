"""Tests for the foundation-scraper write path + match join.

Covers the auditability / idempotency robustness added to
`src/sources/funding/`:

  * `write_projects` stamps a `fetched_at` audit column.
  * the MIN-ROW GUARD refuses to overwrite a good projects.csv with a
    blocked / partial (0-row or <50%) scrape.
  * `is_output_fresh` gates a re-run on the shared funding TTL.
  * `match_repos` stamps a `host_checked` audit column on host-by-repo.

No network: scrapers' `fetch()` is never called — only the shared write/gate
helpers and the pure `classify` join are exercised, against tmp paths.
"""

import argparse
import csv

import src.sources.funding._common as common
import src.sources.funding.match_repos as match_repos
from src.common.freshness import FUNDING_TTL_DAYS
from src.sources.funding._common import (
    FETCHED_AT_COL,
    is_output_fresh,
    write_projects,
)


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _rows(n):
    return [{"name": f"p{i}", "github_repo": f"org/p{i}"} for i in range(n)]


class TestWriteProjects:
    def test_stamps_fetched_at_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, written = write_projects("acme", _rows(3), ["name", "github_repo"])
        assert written is True
        out = _read(path)
        assert len(out) == 3
        assert FETCHED_AT_COL in out[0]
        assert out[0][FETCHED_AT_COL]  # non-empty timestamp

    def test_first_write_with_no_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        # Even a 0-row first scrape writes (no good data to protect).
        path, written = write_projects("acme", [], ["name", "github_repo"])
        assert written is True
        assert _read(path) == []

    def test_explicit_fetched_at_is_used(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, _ = write_projects("acme", _rows(2), ["name", "github_repo"],
                                 fetched_at="2024-01-02T03:04:05+00:00")
        out = _read(path)
        assert all(r[FETCHED_AT_COL] == "2024-01-02T03:04:05+00:00" for r in out)

    def test_guard_refuses_zero_row_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, _ = write_projects("acme", _rows(10), ["name", "github_repo"])
        # A blocked scrape returns 0 rows — must NOT clobber the good file.
        path2, written = write_projects("acme", [], ["name", "github_repo"])
        assert path2 == path
        assert written is False
        assert len(_read(path)) == 10  # old data intact

    def test_guard_refuses_below_half_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, _ = write_projects("acme", _rows(10), ["name", "github_repo"])
        # 4 < 50% of 10 → guarded.
        _, written = write_projects("acme", _rows(4), ["name", "github_repo"])
        assert written is False
        assert len(_read(path)) == 10

    def test_force_bypasses_guard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, _ = write_projects("acme", _rows(10), ["name", "github_repo"])
        # --force is the escape hatch when a foundation legitimately shrank.
        _, written = write_projects("acme", _rows(2), ["name", "github_repo"], force=True)
        assert written is True
        assert len(_read(path)) == 2

    def test_guard_allows_at_or_above_half(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        path, _ = write_projects("acme", _rows(10), ["name", "github_repo"])
        # 5 == 50% of 10 → allowed (not strictly below threshold).
        _, written = write_projects("acme", _rows(5), ["name", "github_repo"])
        assert written is True
        assert len(_read(path)) == 5

    def test_rerun_same_rows_is_stable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        ts = "2024-05-06T07:08:09+00:00"
        path, _ = write_projects("acme", _rows(6), ["name", "github_repo"],
                                 fetched_at=ts)
        first = open(path, encoding="utf-8").read()
        # Same rows + same stamp → byte-identical file (idempotent refresh).
        write_projects("acme", _rows(6), ["name", "github_repo"], fetched_at=ts)
        assert open(path, encoding="utf-8").read() == first


class TestIsOutputFresh:
    def _args(self, ttl=FUNDING_TTL_DAYS, force=False):
        return argparse.Namespace(ttl=ttl, force=force)

    def test_fresh_file_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        write_projects("acme", _rows(3), ["name", "github_repo"])
        assert is_output_fresh("acme", self._args()) is True

    def test_force_bypasses_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        write_projects("acme", _rows(3), ["name", "github_repo"])
        assert is_output_fresh("acme", self._args(force=True)) is False

    def test_ttl_zero_forces_refresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        write_projects("acme", _rows(3), ["name", "github_repo"])
        assert is_output_fresh("acme", self._args(ttl=0)) is False

    def test_missing_file_is_not_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(common, "DATA_DIR", tmp_path)
        assert is_output_fresh("never-scraped", self._args()) is False


class TestMatchReposHostChecked:
    def _write_repos(self, tmp_path, rows):
        p = tmp_path / "top-repos.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["repo", "repo_id", "homepage"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return p

    def test_host_checked_uses_foundation_timestamp(self, tmp_path, monkeypatch):
        repos = self._write_repos(tmp_path, [
            {"repo": "apache/kafka", "homepage": ""},
            {"repo": "random/unmatched", "homepage": ""},
        ])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        slug_idx = {"apache/kafka": "apache"}
        rows = match_repos.classify(
            slug_idx, {}, {},
            fetched_at_by_host={"apache": "2023-09-09T00:00:00+00:00"},
        )
        by_repo = {r["repo"]: r for r in rows}
        # matched repo → carries the apache projects.csv timestamp
        assert by_repo["apache/kafka"]["host"] == "apache"
        assert by_repo["apache/kafka"]["host_checked"] == "2023-09-09T00:00:00+00:00"
        # unmatched repo → blank (no host signal), NEVER a run timestamp, so the
        # join is idempotent across runs.
        assert by_repo["random/unmatched"]["host"] == ""
        assert by_repo["random/unmatched"]["host_checked"] == ""

    def test_host_checked_blank_when_no_foundation_timestamp(self, tmp_path, monkeypatch):
        repos = self._write_repos(tmp_path, [{"repo": "apache/kafka", "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        # Legacy projects.csv with no fetched_at → blank host_checked,
        # deterministic (never a churning run timestamp).
        rows = match_repos.classify(
            {"apache/kafka": "apache"}, {}, {}, fetched_at_by_host={},
        )
        assert rows[0]["host_checked"] == ""

    def test_host_column_present_and_unchanged(self, tmp_path, monkeypatch):
        # build_funding reads `host` by name — guard that the column survives.
        repos = self._write_repos(tmp_path, [{"repo": "cncf/etcd", "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = match_repos.classify(
            {"cncf/etcd": "cncf"}, {}, {},
            fetched_at_by_host={"cncf": "2025-01-01T00:00:00+00:00"},
        )
        assert set(rows[0]) == {"repo", "repo_id", "host", "host_source", "host_checked"}
        assert rows[0]["host"] == "lf/cncf"  # qualified parent/child unchanged

    def test_repo_id_carried_through(self, tmp_path, monkeypatch):
        # host-by-repo.csv joins into build_funding on the STABLE repo_id — if
        # match_repos drops it, every foundation-host intent silently vanishes.
        repos = self._write_repos(
            tmp_path, [{"repo": "cncf/etcd", "repo_id": "11225014", "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = match_repos.classify({"cncf/etcd": "cncf"}, {}, {}, fetched_at_by_host={})
        assert rows[0]["repo_id"] == "11225014"

    def test_gnu_org_prefix_maps_to_fsf_gnu(self, tmp_path, monkeypatch):
        # A GNU mirror org (autotools-mirror/*) is attributed to the GNU project
        # under its FSF parent → host "fsf/gnu", so m4/autoconf/libtool flip intent.
        repos = self._write_repos(
            tmp_path, [{"repo": "autotools-mirror/m4", "repo_id": "66880383",
                        "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = match_repos.classify({}, match_repos.ORG_HOST, {}, fetched_at_by_host={})
        assert rows[0]["host"] == "fsf/gnu"
        assert rows[0]["host_source"] == "org_prefix"

    def test_gnome_org_prefix_maps_to_gnome(self, tmp_path, monkeypatch):
        # GNOME mirrors gitlab.gnome.org to github.com/GNOME/* → org-prefix host.
        repos = self._write_repos(
            tmp_path, [{"repo": "gnome/pango", "repo_id": "1", "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = match_repos.classify({}, match_repos.ORG_HOST, {}, fetched_at_by_host={})
        assert rows[0]["host"] == "gnome"

    def test_xorg_exact_slug_maps_to_xorg(self, tmp_path, monkeypatch):
        # X.Org GitHub mirrors live in a MIXED third-party org, so libxcb matches
        # only by the exact per-project slug from the roster (not an org rule).
        repos = self._write_repos(
            tmp_path, [{"repo": "gitlab-freedesktop-mirrors/libxcb",
                        "repo_id": "1", "homepage": ""}])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = match_repos.classify(
            {"gitlab-freedesktop-mirrors/libxcb": "xorg"}, {}, {}, fetched_at_by_host={})
        assert rows[0]["host"] == "xorg"

    def test_freedesktop_domain_does_not_over_attribute_to_xorg(self, tmp_path, monkeypatch):
        # x.org → xorg, but freedesktop.org is a broader umbrella (wayland,
        # dbus, pipewire) and must NOT be attributed to X.Org.
        repos = self._write_repos(tmp_path, [
            {"repo": "xorg/thing", "repo_id": "1", "homepage": "https://www.x.org/"},
            {"repo": "fd/thing", "repo_id": "2", "homepage": "https://www.freedesktop.org/"},
        ])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = {r["repo"]: r for r in
                match_repos.classify({}, {}, {}, fetched_at_by_host={})}
        assert rows["xorg/thing"]["host"] == "xorg"
        assert rows["fd/thing"]["host"] == ""

    def test_reference_subdomain_never_attributes_host(self, tmp_path, monkeypatch):
        # A repo whose homepage is a PEP/docs page (peps.python.org) merely
        # *links* to the foundation — it must NOT become host=psf. Guards the
        # rogdham/backports.zstd → peps.python.org/pep-0784 false positive.
        repos = self._write_repos(tmp_path, [
            {"repo": "rogdham/backports.zstd", "repo_id": "1",
             "homepage": "https://peps.python.org/pep-0784/"},
            {"repo": "psf/real", "repo_id": "2", "homepage": "https://www.python.org/"},
        ])
        monkeypatch.setattr(match_repos, "REPOS_FILE", repos)
        rows = {r["repo"]: r for r in
                match_repos.classify({}, {}, {}, fetched_at_by_host={})}
        assert rows["rogdham/backports.zstd"]["host"] == ""   # reference link, not a host
        assert rows["psf/real"]["host"] == "psf"              # real python.org home still matches
