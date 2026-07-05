"""Tests for src/risk/build_security.py — security percentile logic."""


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
