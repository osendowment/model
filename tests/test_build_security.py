"""Tests for src/risk/build_security.py — security percentile logic."""


def test_security_score_uses_neutral_cve_anchor():
    """score = geom-mean(openssf_score_p, cve_score), with cve_score using the
    neutral 0→50 anchor (mirrors build_security.build's second pass)."""
    from src.common.percentiles import add_percentiles
    from src.common.stats import floor_anchored_risk

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
                    dim_col="score")

    assert rows[1]["cve_score"] == 50.0          # 0 CVEs → neutral 50, not 78
    assert rows[2]["cve_score"] == 50.0
    assert rows[0]["cve_score"] == 100.0         # only non-zero → worst → 100
    assert rows[0]["openssf_score_p"] == 100.0   # lowest scorecard = worst
    assert rows[0]["score"] == 100.0             # worst on both axes
