"""Tests for src/pipeline/risk/build_security.py — security percentile logic."""


def test_security_percentile_columns():
    from src.pipeline.common.percentiles import add_percentiles
    rows = [
        {"openssf_score": "2", "cve_count_5y": "10"},   # low score + many CVEs = worst
        {"openssf_score": "9", "cve_count_5y": "0"},
        {"openssf_score": "6", "cve_count_5y": "1"},
    ]
    add_percentiles(rows,
                    pctl_specs=[("openssf_score", False), ("cve_count_5y", True)],
                    composite_cols=["openssf_score_p", "cve_count_5y_p"],
                    dim_col="score")
    assert rows[0]["openssf_score_p"] == 100.0   # lowest score = worst
    assert rows[0]["cve_count_5y_p"] == 100.0    # most CVEs = worst
    assert rows[0]["score"] == 100.0
