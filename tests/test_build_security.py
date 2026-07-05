"""Tests for src/risk/build_security.py — security percentile logic + joins."""

from dataclasses import dataclass


@dataclass
class E:
    """Minimal RepoEntry stand-in. Defaults repo_id to the slug so id-keyed
    source mocks (keyed by name) match when a test doesn't care about renames."""
    repo: str
    repo_id: str = ""
    value_class: str = "A"

    def __post_init__(self):
        if not self.repo_id:
            self.repo_id = self.repo


def test_build_recovers_renamed_repo_by_repo_id(monkeypatch):
    """A repo renamed since collection (facebook/react → react/react) still joins
    its security data: every GitHub-identity join keys on the stable repo_id, not
    the (now-stale) name under which the data was fetched. Under the old
    name-keyed joins this row would come back blank."""
    from src.risk import build_security as bs

    sha = "deadbeef"
    # Long rows were fetched under the OLD name but carry the stable repo_id.
    openssf_rows = {
        ("facebook/react", sha, "score"): {
            "repo": "facebook/react", "repo_id": "10270250",
            "commit_sha": sha, "metric": "score", "value": "7",
            "checked_at": "2026-05-03T20:00:58+01:00",
        },
    }

    def fake_read_long(path, *a, **k):
        return openssf_rows if "openssf" in str(path) else {}

    monkeypatch.setattr(bs, "load_top_repos",
                        lambda: [E("react/react", "10270250")])
    monkeypatch.setattr(bs, "_per_year_shas", lambda f: {})   # force sha fallback
    monkeypatch.setattr(bs, "read_long", fake_read_long)
    monkeypatch.setattr(bs, "_load_ossfuzz", lambda: (set(), set()))
    monkeypatch.setattr(bs, "_load_cve_counts_5y", lambda: {"10270250": 3})
    monkeypatch.setattr(bs, "_load_osv_queried", lambda: {"10270250"})
    monkeypatch.setattr(bs, "load_column_by_id", lambda p, c: {"10270250": "gold"})

    row = {r["repo"]: r for r in bs.build()}["react/react"]
    assert row["openssf_score"] == "7"              # scorecard recovered by repo_id
    assert row["openssf_score_source"] == "openssf_local"
    assert row["cve_count_5y"] == "3"               # CVE join by repo_id
    assert row["bestpractices_badge_id"] == "gold"  # badge join by repo_id


def test_ossfuzz_enrollment_joins_by_repo_id_with_slug_fallback(tmp_path, monkeypatch):
    """OSS-Fuzz enrollment is repo_id-first: a projects.csv row still carrying
    the OLD slug but the correct stable repo_id marks the renamed repo enrolled
    (facebook/react → react/react). Rows with a blank repo_id (out-of-scope
    slugs the fetcher couldn't resolve) still match by canonical slug."""
    from src.risk import build_security as bs

    projects = tmp_path / "projects.csv"
    projects.write_text(
        '"project","language","github_repo","repo_id","main_repo","homepage","fetched_at"\n'
        # OLD slug, correct repo_id → must join by id despite the rename.
        '"react","javascript","facebook/react","10270250",'
        '"https://github.com/facebook/react","","2026-05-31T14:41:16+01:00"\n'
        # Blank repo_id → slug-fallback path.
        '"curl","c","curl/curl","",'
        '"https://github.com/curl/curl","curl.se","2026-05-31T14:41:16+01:00"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(bs, "OSSFUZZ_FILE", projects)
    monkeypatch.setattr(bs, "canonical_repo_map", lambda: {})  # slugs map to themselves
    monkeypatch.setattr(bs, "load_top_repos", lambda: [
        E("curl/curl", "310711"),
        E("react/react", "10270250"),        # renamed since collection
        E("torvalds/linux", "2325298"),      # not enrolled
    ])
    monkeypatch.setattr(bs, "_per_year_shas", lambda f: {})
    monkeypatch.setattr(bs, "read_long", lambda path, *a, **k: {})
    monkeypatch.setattr(bs, "_load_cve_counts_5y", lambda: {})
    monkeypatch.setattr(bs, "_load_osv_queried", lambda: set())
    monkeypatch.setattr(bs, "load_column_by_id", lambda p, c: {})

    rows = {r["repo"]: r for r in bs.build()}
    assert rows["react/react"]["ossfuzz_enrolled"] == "True"     # id join (rename)
    assert rows["curl/curl"]["ossfuzz_enrolled"] == "True"       # slug fallback
    assert rows["torvalds/linux"]["ossfuzz_enrolled"] == "False"


def test_security_score_uses_neutral_cve_anchor():
    """score = max(openssf_score_p, cve_score), with cve_score using the
    neutral 0→50 anchor (mirrors build_security.build's second pass)."""
    from src.common.percentiles import add_percentiles
    from src.common.stats import floor_anchored_risk, max_composite

    rows = [
        {"openssf_score": "2", "cve_count_5y": "10"},  # bad scorecard + CVEs = worst
        {"openssf_score": "9", "cve_count_5y": "0"},   # great scorecard, no CVEs
        {"openssf_score": "6", "cve_count_5y": "0"},
    ]
    cve_scores = floor_anchored_risk([float(r["cve_count_5y"]) for r in rows])
    for r, s in zip(rows, cve_scores):
        r["cve_score"] = s
    add_percentiles(rows,
                    pctl_specs=[("openssf_score", False)],
                    composite_cols=["openssf_score_p", "cve_score"],
                    dim_col="score",
                    composite_fn=max_composite)

    assert rows[1]["cve_score"] == 50.0          # 0 CVEs → neutral 50, not 78
    assert rows[2]["cve_score"] == 50.0
    assert rows[0]["cve_score"] == 100.0         # only non-zero → worst → 100
    assert rows[0]["openssf_score_p"] == 100.0   # lowest scorecard = worst
    assert rows[0]["score"] == 100.0             # worst on both axes → 100

    # Worst-of, not geom-mean: the great-scorecard/no-CVE repo scores on the
    # neutral CVE axis (50), NOT a geom mean that its low openssf_score_p would
    # drag under 50. Its openssf axis is the least risky of the three.
    assert rows[1]["openssf_score_p"] < 50.0
    assert rows[1]["score"] == 50.0


def test_cve_not_masked_by_good_hygiene():
    """A repo with real CVEs but a great Scorecard keeps a high security score
    under max — the CVE axis is not diluted away (the key behavioural change
    from the former geometric-mean composite)."""
    from src.common.percentiles import add_percentiles
    from src.common.stats import floor_anchored_risk, max_composite

    rows = [
        {"openssf_score": "10", "cve_count_5y": "8"},  # great hygiene, real CVEs
        {"openssf_score": "1", "cve_count_5y": "0"},   # awful hygiene, no CVEs
        {"openssf_score": "5", "cve_count_5y": "0"},
    ]
    cve_scores = floor_anchored_risk([float(r["cve_count_5y"]) for r in rows])
    for r, s in zip(rows, cve_scores):
        r["cve_score"] = s
    add_percentiles(rows,
                    pctl_specs=[("openssf_score", False)],
                    composite_cols=["openssf_score_p", "cve_score"],
                    dim_col="score",
                    composite_fn=max_composite)

    # row 0 has the best possible Scorecard (openssf_score_p is the lowest risk)
    # yet its real CVEs (cve_score=100) carry the score straight to 100.
    assert rows[0]["cve_score"] == 100.0
    assert rows[0]["openssf_score_p"] < 50.0
    assert rows[0]["score"] == 100.0
