"""Tests for src/risk/build_funding.py — join, info cols, funding score."""

from dataclasses import dataclass

from src.risk import build_funding as bf


def test_oc_avg_funding_zero_default():
    assert bf.oc_avg_funding("", {}) == "0"                       # no slug → $0
    assert bf.oc_avg_funding("ghost", {"x": {"raised_2024": "5"}}) == "0"
    assert bf.oc_avg_funding("x", {"x": {"raised_2024": "10", "raised_2025": "20"}}) == "15"


def test_assemble_row_stars_forks_sponsorships():
    row = bf.assemble_row(
        repo="o/r", repo_id="1",
        sponsors={"github_sponsors": "12"},
        yml={"has_funding_yml": "True", "funding_yml_platforms": "github"},
        export={}, foundation_host="", oc_budgets={},
        repo_meta={"stars": "5000", "forks": "300"},
        sponsoring_count="39",
    )
    assert row["gh_sponsors_in"] == "12"
    assert row["gh_sponsors_out"] == "39"
    assert row["gh_sponsorships"] == "51"      # in + out
    assert row["gh_stars"] == "5000"           # info column
    assert row["gh_forks"] == "300"            # info column
    assert row["oc_avg_funding"] == "0"        # no OC → $0
    assert "score" not in row                  # filled by build()


def test_build_funding_score_lower_funding_higher_score(monkeypatch):
    @dataclass
    class E:
        repo: str
        repo_id: str = ""

    def rows_by_repo(p):
        p = str(p)
        if "sponsors.csv" in p:
            return {"o/poor": {"github_sponsors": "0"}, "o/rich": {"github_sponsors": "100"}}
        if "funding-yml" in p:
            return {"o/rich": {"open_collective": "rich"}}
        return {}

    monkeypatch.setattr(bf, "load_risk_repos", lambda: [E("o/poor"), E("o/rich")])
    monkeypatch.setattr(bf, "load_rows_by_repo", rows_by_repo)
    monkeypatch.setattr(bf, "load_column_by_repo", lambda p, c: {})
    monkeypatch.setattr(bf, "_export_by_repo", lambda p: {})
    monkeypatch.setattr(bf, "_load_oc", lambda p: {"rich": {"raised_2024": "10000"}})
    monkeypatch.setattr(bf, "_load_sponsoring", lambda p: {})

    rows = {r["repo"]: r for r in bf.build()}
    # o/poor: 0 sponsors + $0 OC → worst on both axes → score 100
    assert rows["o/poor"]["score"] == 100
    assert int(rows["o/rich"]["score"]) < int(rows["o/poor"]["score"])
