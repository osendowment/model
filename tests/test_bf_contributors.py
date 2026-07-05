"""Tests for src/sources/github/bf_contributors.py — bus-factor membership.

The membership must match build_concentration's `bf_commits_gh_alltime`: the
fewest top non-bot contributors whose commits cumulatively reach 50%.
"""
from src.sources.github.bf_contributors import _bf_member_logins, load_bf_contributors


def _rows(*triples):
    """(login, contributions, account_type) → contributor-commits dict rows."""
    return [{"login": l, "contributions": str(c), "account_type": t}
            for l, c, t in triples]


def test_single_dominant_contributor_is_the_bf_set():
    # one contributor with >50% → bus factor 1, that login only
    rows = _rows(("alice", 90, "User"), ("bob", 10, "User"))
    assert _bf_member_logins(rows) == ["alice"]


def test_cumulative_reaches_half():
    # 40 + 35 = 75% ≥ 50% at the second contributor → BF set is the top two,
    # in contribution order; the third (25) is outside it.
    rows = _rows(("a", 40, "User"), ("b", 35, "User"), ("c", 25, "User"))
    assert _bf_member_logins(rows) == ["a", "b"]


def test_bots_excluded_from_membership():
    # a bot with the most commits is not a fundable person and must not appear;
    # the human `alice` alone carries >50% of the non-bot commits.
    rows = _rows(("dependabot[bot]", 500, "Bot"),
                 ("alice", 90, "User"), ("bob", 10, "User"))
    assert _bf_member_logins(rows) == ["alice"]


def test_logins_lowercased():
    rows = _rows(("MarijnH", 100, "User"))
    assert _bf_member_logins(rows) == ["marijnh"]


def test_no_positive_commits_is_empty():
    assert _bf_member_logins(_rows(("a", 0, "User"))) == []
    assert _bf_member_logins([]) == []


def test_anonymous_contributor_occupies_slot_but_is_not_returned():
    # An empty-login (anonymous) contributor stays in the bus-factor population
    # — matching build_concentration's count — so with 60% of commits it takes
    # the single BF slot; but it is uncheckable, so the returned list is empty
    # rather than crediting a blank login.
    rows = _rows(("", 60, "User"), ("real", 40, "User"))
    assert _bf_member_logins(rows) == []


def test_load_groups_by_repo_id(tmp_path):
    p = tmp_path / "contributor-commits.csv"
    p.write_text(
        "repo,repo_id,login,contributions,account_type\n"
        "o/a,gh/1,alice,90,User\n"
        "o/a,gh/1,bob,10,User\n"
        "o/b,gh/2,carol,60,User\n"
        "o/b,gh/2,dave,40,User\n"
        "o/c,gh/3,onlybot[bot],50,Bot\n",   # all-bot repo → no BF set → absent
        encoding="utf-8",
    )
    got = load_bf_contributors(p)
    assert got == {"gh/1": ["alice"], "gh/2": ["carol"]}


def test_load_missing_file_is_empty(tmp_path):
    assert load_bf_contributors(tmp_path / "absent.csv") == {}
