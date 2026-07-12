import csv

from src.sources.github import fetch_sponsors
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
    assert _has_sponsor_signal({"gh_sponsorships_in": "0", "gh_sponsors_enabled": "False"}) is False
    assert _has_sponsor_signal({}) is False
    assert _has_sponsor_signal({"gh_sponsorships_in": "", "gh_sponsors_enabled": ""}) is False
    # any signal (sponsors count OR Sponsors enabled) → cached for the full TTL
    assert _has_sponsor_signal({"gh_sponsorships_in": "5", "gh_sponsors_enabled": "False"}) is True
    assert _has_sponsor_signal({"gh_sponsorships_in": "0", "gh_sponsors_enabled": "True"}) is True


def test_repo_id_map_resolves_renamed_slug(tmp_path, monkeypatch):
    # A repo renamed on GitHub: repos.csv holds repo=old, full_name=new, repo_id=gh/1.
    # The fetcher stamps repo_id for the CURRENT canonical slug (=new); a map keyed
    # only on `repo` leaves `new/name` unresolvable → blank repo_id → the id-keyed
    # downstream join drops the row and the sponsors signal is lost. _repo_id_map
    # must key on BOTH `repo` and the rename-resolved `full_name`.
    repos_csv = tmp_path / "repos.csv"
    with open(repos_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "full_name", "repo_id"])
        w.writeheader()
        w.writerow({"repo": "old/name", "full_name": "new/name", "repo_id": "gh/1"})
    monkeypatch.setattr(fetch_sponsors, "GH_REPOS_FILE", repos_csv)

    m = fetch_sponsors._repo_id_map()
    assert m["new/name"] == "gh/1"  # current canonical slug resolves (rename-proof)
    assert m["old/name"] == "gh/1"  # stale slug still resolves too
