"""Tests for src/eligibility/build_eligibility.py — the AND-rollup."""

from __future__ import annotations

from src.common.repos import RepoEntry
from src.eligibility import build_eligibility as be


def _write(path, header, rows):
    path.write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))


def test_eligible_is_and_of_four_flags(tmp_path, monkeypatch):
    entries = [RepoEntry(repo=r) for r in
               ("a/all", "b/no-oss", "c/no-intent", "d/company", "e/inactive",
                "f/missing-funding")]
    monkeypatch.setattr(be, "load_top_repos", lambda skip_archived: entries)

    lic = tmp_path / "licenses.csv"
    _write(lic, "repo,oss", ["a/all,True", "b/no-oss,", "c/no-intent,True",
                             "d/company,True", "e/inactive,True",
                             "f/missing-funding,True"])
    fund = tmp_path / "funding.csv"
    _write(fund, "repo,intent,nonprofit",
           ["a/all,True,True", "b/no-oss,True,True", "c/no-intent,False,True",
            "d/company,True,False", "e/inactive,True,True"])
    act = tmp_path / "active.csv"
    _write(act, "repo,active", ["a/all,True", "b/no-oss,True",
                                "c/no-intent,True", "d/company,True",
                                "e/inactive,False", "f/missing-funding,True"])
    monkeypatch.setattr(be, "LICENSES_FILE", lic)
    monkeypatch.setattr(be, "FUNDING_FILE", fund)
    monkeypatch.setattr(be, "ACTIVE_FILE", act)

    rows = {r["repo"]: r for r in be.build()}
    assert rows["a/all"]["eligible"] is True
    # each single failing flag blocks eligibility
    assert rows["b/no-oss"]["eligible"] is False       # oss="" (unknown) → False
    assert rows["c/no-intent"]["eligible"] is False
    assert rows["d/company"]["eligible"] is False      # nonprofit=False
    assert rows["e/inactive"]["eligible"] is False
    # missing funding row → intent defaults False (nonprofit defaults True)
    f = rows["f/missing-funding"]
    assert (f["intent"], f["nonprofit"]) == (False, True)
    assert f["eligible"] is False
