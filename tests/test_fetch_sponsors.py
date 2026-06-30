from src.sources.github.fetch_sponsors import (
    _has_sponsor_signal,
    logins_for_repo,
    status_from_counts,
)


def test_logins_owner_only():
    # Sponsors count only for the account that OWNS the repo — never co-maintainers.
    assert logins_for_repo("owner/repo") == ["owner"]
    assert logins_for_repo("Owner/Repo") == ["owner"]  # lower-cased


def test_status_ok_vs_error():
    assert status_from_counts([0, 3], any_error=False) == "ok"
    assert status_from_counts([0], any_error=True) == "error"


def test_has_sponsor_signal():
    # no signal → rechecked on the short window
    assert _has_sponsor_signal({"gh_sponsors": "0", "has_gh_sponsors": "False"}) is False
    assert _has_sponsor_signal({}) is False
    assert _has_sponsor_signal({"gh_sponsors": "", "has_gh_sponsors": ""}) is False
    # any signal (sponsors count OR Sponsors enabled) → cached for the full TTL
    assert _has_sponsor_signal({"gh_sponsors": "5", "has_gh_sponsors": "False"}) is True
    assert _has_sponsor_signal({"gh_sponsors": "0", "has_gh_sponsors": "True"}) is True
