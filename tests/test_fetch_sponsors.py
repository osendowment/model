from src.github.fetch_sponsors import (
    logins_for_repo,
    status_from_counts,
    combine_results,
)


def test_logins_owner_plus_yml():
    yml = {"owner/repo": "alice,bob"}  # funding_yml_github map
    assert logins_for_repo("owner/repo", yml) == ["owner", "alice", "bob"]


def test_logins_owner_only_when_no_yml():
    assert logins_for_repo("owner/repo", {}) == ["owner"]


def test_logins_dedupe_owner_in_yml():
    yml = {"owner/repo": "owner,carol"}
    assert logins_for_repo("owner/repo", yml) == ["owner", "carol"]


def test_status_ok_vs_error():
    assert status_from_counts([0, 3], any_error=False) == "ok"
    assert status_from_counts([0], any_error=True) == "error"


def test_combine_results_inbound_sums_outbound_is_owner():
    # (received, sponsoring, ok); owner first, then a co-maintainer
    res = [(5, 39, True), (3, 7, True)]
    inbound, outbound, any_error = combine_results(res)
    assert inbound == 8        # 5 + 3 received across logins
    assert outbound == 39      # owner's sponsoring only (not the co-maintainer's 7)
    assert any_error is False


def test_combine_results_flags_error():
    assert combine_results([(0, 0, False)]) == (0, 0, True)


def test_combine_results_empty():
    assert combine_results([]) == (0, 0, False)
