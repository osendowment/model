"""Tests for src/risk/build_concentration.py."""


def test_bus_factor_one_scores_100_concentration():
    from src.common.percentiles import add_percentiles
    rows = [
        {"bf_commits_git_5y": "1", "hhi_commits_git_5y": "9000"},
        {"bf_commits_git_5y": "5", "hhi_commits_git_5y": "2000"},
        {"bf_commits_git_5y": "3", "hhi_commits_git_5y": "5000"},
    ]
    add_percentiles(
        rows,
        pctl_specs=[("bf_commits_git_5y", False),
                    ("hhi_commits_git_5y", True)],
        composite_cols=["bf_commits_git_5y_p", "hhi_commits_git_5y_p"],
        dim_col="score",
    )
    assert rows[0]["bf_commits_git_5y_p"] == 100.0
    assert rows[0]["hhi_commits_git_5y_p"] == 100.0
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
    metrics blank out. GitHub columns are `_gh_alltime` (uncapped API lifetime).
    """
    from src.risk.build_concentration import github_metrics
    m = github_metrics([
        {"login": "dependabot[bot]", "contributions": "7", "account_type": "Bot"},
    ])
    assert m["bf_commits_gh_alltime"] == ""
    assert m["hhi_commits_gh_alltime"] == ""
    assert m["active_contributors_gh_alltime"] == 0
    assert m["total_commits_gh_alltime"] == 7


def test_git_metrics_blank_window_when_no_recent_human_commits():
    """Repo with human commits only before the _5y window.

    Regression for the dormant-window / bot-only-window rows that were
    emitting bf_commits_git_5y=0 (-> percentile 100). _full is computable;
    the empty window must be blank, not 0.
    """
    from src.risk.build_concentration import git_metrics
    m = git_metrics([
        {"author_name": "Alice", "author_email": "alice@example.com",
         "year": "2019", "commits": "40"},
        {"author_name": "Bob", "author_email": "bob@example.com",
         "year": "2019", "commits": "10"},
    ])
    assert m["bf_commits_git_full"] != ""        # _full computable
    assert m["commits_git_5y"] == 0              # genuinely no window commits
    assert m["active_contributors_git_5y"] == 0
    assert m["bf_commits_git_5y"] == ""          # window bus factor undefined
    assert m["hhi_commits_git_5y"] == ""


def test_git_metrics_full_caps_at_last_complete_year():
    """_full excludes commits in a not-yet-complete year (> last complete year).

    A commit in the partial current year must not inflate total_commits_git_full
    or count its author as a _full contributor — keeps _full reproducible.
    """
    from src.risk.build_concentration import LAST_COMPLETE_YEAR, git_metrics
    future = LAST_COMPLETE_YEAR + 1
    m = git_metrics([
        {"author_name": "Alice", "author_email": "a@x.com",
         "year": str(LAST_COMPLETE_YEAR), "commits": "40"},
        {"author_name": "Alice", "author_email": "a@x.com",
         "year": str(future), "commits": "5"},
        {"author_name": "Bob", "author_email": "b@x.com",
         "year": str(future), "commits": "10"},
    ])
    assert m["total_commits_git_full"] == 40   # the future-year commits excluded
    assert m["contributors_git_full"] == 1     # Bob (future-only) is not a _full contributor
