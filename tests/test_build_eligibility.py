"""Tests for src/eligibility/build_eligibility.py — the AND-rollup."""

from __future__ import annotations

from src.common.repos import RepoEntry
from src.eligibility import build_eligibility as be


def _write(path, header, rows):
    path.write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))


def test_eligible_is_and_of_four_flags(tmp_path, monkeypatch):
    slugs = ("a/all", "b/no-oss", "c/no-intent", "d/company", "e/inactive",
             "f/missing-funding")
    entries = [RepoEntry(repo=r, repo_id=str(i + 1))
               for i, r in enumerate(slugs)]
    monkeypatch.setattr(be, "load_top_repos", lambda *a, **k: entries)

    # The rollup joins the per-dimension CSVs by repo_id, not slug.
    lic = tmp_path / "licenses.csv"
    _write(lic, "repo,repo_id,oss",
           ["a/all,1,True", "b/no-oss,2,", "c/no-intent,3,True",
            "d/company,4,True", "e/inactive,5,True",
            "f/missing-funding,6,True"])
    fund = tmp_path / "funding.csv"
    _write(fund, "repo,repo_id,intent,nonprofit",
           ["a/all,1,True,True", "b/no-oss,2,True,True",
            "c/no-intent,3,False,True", "d/company,4,True,False",
            "e/inactive,5,True,True"])
    act = tmp_path / "active.csv"
    _write(act, "repo,repo_id,active",
           ["a/all,1,True", "b/no-oss,2,True", "c/no-intent,3,True",
            "d/company,4,True", "e/inactive,5,False",
            "f/missing-funding,6,True"])
    monkeypatch.setattr(be, "LICENSES_FILE", lic)
    monkeypatch.setattr(be, "FUNDING_FILE", fund)
    monkeypatch.setattr(be, "ACTIVE_FILE", act)

    rows = {r["repo"]: r for r in be.build()}
    assert rows["a/all"]["eligible"] is True
    # rename-proof: flags resolve by id even when the intermediate row's
    # slug is stale (pre-rename) — a slug join would lose every flag here
    # each single failing flag blocks eligibility
    assert rows["b/no-oss"]["eligible"] is False       # oss="" (unknown) → False
    assert rows["c/no-intent"]["eligible"] is False
    assert rows["d/company"]["eligible"] is False      # nonprofit=False
    assert rows["e/inactive"]["eligible"] is False
    # missing funding row → intent defaults False (nonprofit defaults True)
    f = rows["f/missing-funding"]
    assert (f["intent"], f["nonprofit"]) == (False, True)
    assert f["eligible"] is False


def test_rollup_flags_resolve_by_id_across_a_rename(tmp_path, monkeypatch):
    """A repo renamed after the intermediates were built: the scope holds the
    NEW canonical slug, the intermediate rows still carry the OLD slug but
    the same immutable repo_id. The id join must find every flag; the old
    slug join silently lost them all (eligible flipped to False)."""
    entries = [RepoEntry(repo="react/react", repo_id="10270250")]
    monkeypatch.setattr(be, "load_top_repos", lambda *a, **k: entries)
    lic = tmp_path / "licenses.csv"
    _write(lic, "repo,repo_id,oss", ["facebook/react,10270250,True"])
    fund = tmp_path / "funding.csv"
    _write(fund, "repo,repo_id,intent,nonprofit",
           ["facebook/react,10270250,True,True"])
    act = tmp_path / "active.csv"
    _write(act, "repo,repo_id,active", ["facebook/react,10270250,True"])
    monkeypatch.setattr(be, "LICENSES_FILE", lic)
    monkeypatch.setattr(be, "FUNDING_FILE", fund)
    monkeypatch.setattr(be, "ACTIVE_FILE", act)

    rows = {r["repo"]: r for r in be.build()}
    assert rows["react/react"]["eligible"] is True
