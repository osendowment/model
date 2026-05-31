"""Tests for src/pipeline/risk/build_complexity.py."""


def test_scoreercentile_columns():
    from src.common.percentiles import add_percentiles
    rows = [
        {"loc_2025_eoy": "2000000", "cyclomatic_max": "90"},
        {"loc_2025_eoy": "5000", "cyclomatic_max": "8"},
        {"loc_2025_eoy": "50000", "cyclomatic_max": "30"},
    ]
    add_percentiles(rows,
                    pctl_specs=[("loc_2025_eoy", True), ("cyclomatic_max", True)],
                    composite_cols=["loc_2025_eoy_p", "cyclomatic_max_p"],
                    dim_col="score")
    assert rows[0]["loc_2025_eoy_p"] == 100.0      # biggest codebase = worst
    assert rows[0]["score"] == 100.0
