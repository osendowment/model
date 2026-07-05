"""Tests for src/eligibility/build_active.py — EOL override + archived flag."""

from __future__ import annotations

from src.common.repos import RepoEntry
from src.eligibility import build_active as ba


def test_load_eol_overrides_parses_tristate(tmp_path):
    """Only rows with a non-empty `eol` cell land in the index; True/False
    are both explicit verdicts (False pins a repo alive)."""
    f = tmp_path / "overrides.csv"
    f.write_text("repo,host,eol,reason\n"
                 "dead/proj,,True,upstream announced EOL\n"
                 "alive/proj,,False,pinned alive\n"
                 "other/proj,somehost.org,,host-only row\n")
    idx = ba.load_eol_overrides(f)
    assert idx == {"dead/proj": True, "alive/proj": False}


def test_active_truth_table(monkeypatch):
    """active = NOT eol AND (NOT archived OR mirror-exempt)."""
    entries = [
        RepoEntry(repo="ok/alive"),
        RepoEntry(repo="ok/archived", archived=True),
        RepoEntry(repo="bminor/glibc", archived=True),   # mirror-exempt
        RepoEntry(repo="dead/eol"),
    ]
    monkeypatch.setattr(ba, "load_top_repos",
                        lambda skip_archived: entries)
    monkeypatch.setattr(ba, "load_eol_overrides", lambda: {"dead/eol": True})
    monkeypatch.setattr(ba, "load_live_upstream_mirrors",
                        lambda: {"bminor/glibc"})

    rows = {r["repo"]: r for r in ba.build()}
    assert rows["ok/alive"]["active"] is True
    assert rows["ok/archived"]["active"] is False       # archived → inactive
    assert rows["bminor/glibc"]["active"] is True       # archived mirror kept
    assert rows["bminor/glibc"]["mirror"] is True
    assert rows["dead/eol"]["active"] is False          # eol override wins
