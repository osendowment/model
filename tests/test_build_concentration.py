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


def test_git_metrics_dormant_window_imputes_max_concentration():
    """Dormant repo: real history, but ZERO commits in the _5y window.

    A successfully-fetched repo nobody has touched in five years is a maximal
    single-point-of-failure, so the _5y axis is imputed to the worst valid pair
    (bus factor 1, HHI 10000). Crucially the *_full* axis is NOT imputed — it
    keeps its real computed values (here HHI 6800 from the 40/10 split), so the
    imputation is visibly confined to the window.
    """
    from src.risk.build_concentration import git_metrics
    m = git_metrics([
        {"author_name": "Alice", "author_email": "alice@example.com",
         "year": "2019", "commits": "40"},
        {"author_name": "Bob", "author_email": "bob@example.com",
         "year": "2019", "commits": "10"},
    ])
    assert m["commits_git_5y"] == 0              # genuinely no window commits
    assert m["active_contributors_git_5y"] == 0
    assert m["bf_commits_git_5y"] == 1           # imputed: dormant => max risk
    assert m["hhi_commits_git_5y"] == 10000
    assert m["comment"] == "no commits in 5y"
    # _full keeps the real distribution, untouched by the window imputation.
    assert m["bf_commits_git_full"] == 1
    assert m["hhi_commits_git_full"] == 6800     # (0.8² + 0.2²)·10000, NOT 10000


def test_git_metrics_bot_only_window_imputes_max_concentration():
    """Window commits exist but only from bots -> imputed, not blank.

    No active *human* maintainer in five years is itself maximal concentration
    risk, so the _5y axis is imputed (bf 1, HHI 10000) — distinguished from the
    dormant case by the `no human commits in 5y` comment. _full stays real.
    """
    from src.risk.build_concentration import git_metrics
    m = git_metrics([
        {"author_name": "Alice", "author_email": "alice@example.com",
         "year": "2019", "commits": "40"},
        {"author_name": "dependabot[bot]", "author_email": "dep@github.com",
         "year": "2023", "commits": "7"},
    ])
    assert m["commits_git_5y"] == 7              # window has (bot) commits
    assert m["active_contributors_git_5y"] == 0  # but no humans
    assert m["bf_commits_git_5y"] == 1           # imputed: no active human
    assert m["hhi_commits_git_5y"] == 10000
    assert m["comment"] == "no human commits in 5y"
    assert m["bf_commits_git_full"] != ""        # _full untouched (Alice real)


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
