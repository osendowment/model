from src.sources.github.fetch_sponsors import (
    _has_sponsor_signal,
    logins_for_repo,
    status_from_counts,
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


def test_has_sponsor_signal():
    # no signal → rechecked on the short window
    assert _has_sponsor_signal({"github_sponsors": "0", "owner_has_sponsors_listing": "False"}) is False
    assert _has_sponsor_signal({}) is False
    assert _has_sponsor_signal({"github_sponsors": "", "owner_has_sponsors_listing": ""}) is False
    # any signal → cached for the full TTL
    assert _has_sponsor_signal({"github_sponsors": "5", "owner_has_sponsors_listing": "False"}) is True
    assert _has_sponsor_signal({"github_sponsors": "0", "owner_has_sponsors_listing": "True"}) is True
