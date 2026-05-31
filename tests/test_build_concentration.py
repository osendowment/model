"""Tests for src/pipeline/risk/build_concentration.py."""


def test_bus_factor_one_scores_100_concentration():
    from src.common.percentiles import add_percentiles
    rows = [
        {"bf_commits_git_2021_2025": "1", "hhi_commits_git_2021_2025": "9000"},
        {"bf_commits_git_2021_2025": "5", "hhi_commits_git_2021_2025": "2000"},
        {"bf_commits_git_2021_2025": "3", "hhi_commits_git_2021_2025": "5000"},
    ]
    add_percentiles(
        rows,
        pctl_specs=[("bf_commits_git_2021_2025", False),
                    ("hhi_commits_git_2021_2025", True)],
        composite_cols=["bf_commits_git_2021_2025_p", "hhi_commits_git_2021_2025_p"],
        dim_col="score",
    )
    assert rows[0]["bf_commits_git_2021_2025_p"] == 100.0
    assert rows[0]["hhi_commits_git_2021_2025_p"] == 100.0
    assert rows[0]["score"] == 100.0
