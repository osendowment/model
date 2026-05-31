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


def test_bus_factor_hhi_blank_when_no_contributors():
    """Empty / all-zero contributor set -> ("", ""), never (0, 0).

    Bus factor and HHI are undefined for an empty population. A real 0 would
    rank as MAX concentration risk (bf 0 < 1) while simultaneously ranking as
    MIN HHI risk — a self-contradicting lack-of-data value masquerading as a
    measurement.
    """
    from src.risk.build_concentration import _bus_factor_hhi
    assert _bus_factor_hhi([]) == ("", "")
    assert _bus_factor_hhi([0, 0]) == ("", "")
    bf, hhi = _bus_factor_hhi([10, 5])
    assert bf == 1 and hhi != ""  # a real population still computes


def test_github_metrics_blank_bf_for_bot_only_repo():
    """A repo whose only /contributors rows are bots -> bf/hhi blank, not 0.

    The raw commit count stays real; only the (undefined) concentration
    metrics blank out.
    """
    from src.risk.build_concentration import github_metrics
    m = github_metrics([
        {"login": "dependabot[bot]", "contributions": "7", "account_type": "Bot"},
    ])
    assert m["bf_commits_github"] == ""
    assert m["hhi_commits_github"] == ""
    assert m["active_contributors_github"] == 0
    assert m["total_commits_github"] == 7


def test_git_metrics_blank_window_when_no_recent_human_commits():
    """Repo with human commits only before the 2021-2025 window.

    Regression for the dormant-window / bot-only-window rows that were
    emitting bf_commits_git_2021_2025=0 (-> percentile 100). Lifetime is
    computable; the empty window must be blank, not 0.
    """
    from src.risk.build_concentration import git_metrics
    m = git_metrics([
        {"author_name": "Alice", "author_email": "alice@example.com",
         "year": "2019", "commits": "40"},
        {"author_name": "Bob", "author_email": "bob@example.com",
         "year": "2019", "commits": "10"},
    ])
    assert m["bf_commits_git"] != ""            # lifetime computable
    assert m["commits_git_2021_2025"] == 0      # genuinely no window commits
    assert m["active_contributors_git_2021_2025"] == 0
    assert m["bf_commits_git_2021_2025"] == ""  # window bus factor undefined
    assert m["hhi_commits_git_2021_2025"] == ""
