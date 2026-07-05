"""Tests for src/eligibility/build_active.py — EOL override + archived flag."""

from __future__ import annotations

from src.common.repos import RepoEntry
from src.eligibility import build_active as ba


def test_load_eol_overrides_parses_tristate(tmp_path):
    """Only rows with a non-empty `eol` cell land in the index; True/False
    are both explicit verdicts (False pins a repo alive). Rows with a
    repo_id key by id (rename-proof); id-less hand rows fall back to slug."""
    f = tmp_path / "overrides.csv"
    f.write_text("repo,repo_id,host,eol,reason\n"
                 "dead/proj,111,,True,upstream announced EOL\n"
                 "alive/proj,222,,False,pinned alive\n"
                 "handmade/no-id,,,True,added before id backfill\n"
                 "other/proj,333,somehost.org,,host-only row\n")
    by_id, by_slug = ba.load_eol_overrides(f)
    assert by_id == {"111": True, "222": False}
    assert by_slug == {"handmade/no-id": True}


def test_active_truth_table(monkeypatch):
    """active = NOT eol AND (NOT archived OR mirror-exempt)."""
    entries = [
        RepoEntry(repo="ok/alive", repo_id="1"),
        RepoEntry(repo="ok/archived", repo_id="2", archived=True),
        RepoEntry(repo="bminor/glibc", repo_id="3", archived=True),  # mirror-exempt
        RepoEntry(repo="dead/eol", repo_id="4"),
    ]
    monkeypatch.setattr(ba, "load_top_repos",
                        lambda skip_archived: entries)
    # eol override joined by repo_id — the slug may drift after a rename
    monkeypatch.setattr(ba, "load_eol_overrides", lambda: ({"4": True}, {}))
    monkeypatch.setattr(ba, "load_live_upstream_mirrors",
                        lambda: {"bminor/glibc"})

    rows = {r["repo"]: r for r in ba.build()}
    assert rows["ok/alive"]["active"] is True
    assert rows["ok/archived"]["active"] is False       # archived → inactive
    assert rows["bminor/glibc"]["active"] is True       # archived mirror kept
    assert rows["bminor/glibc"]["mirror"] is True
    assert rows["dead/eol"]["active"] is False          # eol override wins
