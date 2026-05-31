"""Tests for src/pipeline/risk/build_workload.py — workload percentile logic."""


def test_scoreercentile_columns():
    from src.common.percentiles import add_percentiles
    rows = [
        {"loc_per_ac": "100000", "cve_per_ac": "5", "nni_per_ac": "50"},
        {"loc_per_ac": "100", "cve_per_ac": "0", "nni_per_ac": "1"},
        {"loc_per_ac": "5000", "cve_per_ac": "1", "nni_per_ac": "10"},
    ]
    add_percentiles(rows,
                    pctl_specs=[("loc_per_ac", True), ("cve_per_ac", True),
                                ("nni_per_ac", True)],
                    composite_cols=["loc_per_ac_p", "cve_per_ac_p", "nni_per_ac_p"],
                    dim_col="score")
    assert rows[0]["score"] == 100.0   # worst on all three axes
