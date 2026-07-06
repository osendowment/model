"""Tests for src/build_people.py."""
from src import build_people as bp


def test_person_id_prefers_user_id_then_email_then_login():
    assert bp._person_id("github", "alice", "123", "") == "github/123"
    assert bp._person_id("github", "alice", "", "a@example.com") == "email:a@example.com"
    assert bp._person_id("github", "Alice", "", "") == "github:alice"


def test_add_github_contribution_creates_and_accumulates_roles():
    people = {}
    users_by_login = {"alice": {"user_id": "1", "name": "Alice A",
                                "email": "a@example.com", "html_url": "https://github.com/alice",
                                "company": "Acme", "bio": "hi"}}
    bp._add_github_contribution(people, "owner_repo_ids", "alice", "gh/1", users_by_login)
    bp._add_github_contribution(people, "key_contributor_repo_ids", "alice", "gh/2", users_by_login)

    assert len(people) == 1
    person = people["github/1"]
    assert person["owner_repo_ids"] == {"gh/1"}
    assert person["key_contributor_repo_ids"] == {"gh/2"}
    assert person["name"] == "Alice A"
    assert person["emails"] == {"a@example.com"}


def test_add_github_contribution_skips_bots():
    people = {}
    bp._add_github_contribution(people, "owner_repo_ids", "dependabot[bot]", "gh/1", {})
    assert people == {}


def test_add_github_contribution_falls_back_to_login_key_without_profile():
    people = {}
    bp._add_github_contribution(people, "owner_repo_ids", "ghost", "gh/1", {})
    assert "github:ghost" in people
    assert people["github:ghost"]["user_id"] == ""


def test_add_ecosystem_contribution_creates_row_keyed_by_uuid():
    people = {}
    bp._add_ecosystem_contribution(people, {
        "ecosystem": "npm", "login": "jdoe", "uuid": "jdoe", "name": "J Doe",
        "email": "j@example.com", "html_url": "https://npmjs.com/~jdoe",
        "repo_id": "gh/1",
    })
    assert "npm/jdoe" in people
    person = people["npm/jdoe"]
    assert person["platform"] == "npm"
    assert person["ecosystem_maintainer_repo_ids"] == {"gh/1"}


def test_finalize_row_unions_role_columns_into_repo_ids():
    person = bp._new_person("github/1", "github", "alice", "1")
    person["owner_repo_ids"] = {"gh/1"}
    person["key_contributor_repo_ids"] = {"gh/2"}
    person["emails"] = {"a@example.com"}

    row = bp._finalize_row(person)

    assert row["repo_ids"] == "gh/1|gh/2"
    assert row["repo_count"] == "2"
    assert row["owner_repo_ids"] == "gh/1"
    assert row["key_contributor_repo_ids"] == "gh/2"
    assert row["emails"] == "a@example.com"
    assert list(row.keys()) == bp.FIELDS


def test_target_repo_ids_reads_repo_id_column():
    rows = [{"repo_id": "gh/1", "repo": "a/a"}, {"repo_id": "gh/2", "repo": "b/b"}]
    assert bp._target_repo_ids(rows) == {"gh/1", "gh/2"}


def test_owner_pairs_filters_scope_and_org_owners():
    repos_rows = [
        {"repo_id": "gh/1", "owner_type": "User", "owner_login": "alice"},
        {"repo_id": "gh/2", "owner_type": "Organization", "owner_login": "acme"},
        {"repo_id": "gh/3", "owner_type": "User", "owner_login": "bob"},  # out of scope
    ]
    pairs = bp._owner_pairs(repos_rows, {"gh/1", "gh/2"})
    assert pairs == [("alice", "gh/1")]  # org owner and out-of-scope repo excluded


def test_org_logins_collects_every_organization_owner():
    repos_rows = [
        {"owner_type": "Organization", "owner_login": "Acme"},
        {"owner_type": "User", "owner_login": "alice"},
    ]
    assert bp._org_logins(repos_rows) == {"acme"}


def test_funding_yml_pairs_splits_and_drops_org_logins():
    rows = [{"repo_id": "gh/1", "github": "alice, acme, bob"}]
    pairs = bp._funding_yml_pairs(rows, {"gh/1"}, org_logins={"acme"})
    assert pairs == [("alice", "gh/1"), ("bob", "gh/1")]


def test_ecosystem_maintainer_rows_filters_to_target_scope():
    rows = [{"repo_id": "gh/1", "login": "a"}, {"repo_id": "gh/9", "login": "b"}]
    assert bp._ecosystem_maintainer_rows(rows, {"gh/1"}) == [{"repo_id": "gh/1", "login": "a"}]


def test_apply_curated_overrides_tags_existing_github_row_only():
    people = {"github/1": bp._new_person("github/1", "github", "hartwork", "1"),
              "npm/x": bp._new_person("npm/x", "npm", "hartwork", "x")}
    bp._apply_curated_overrides(people, [{"login": "hartwork", "reason": "FSFE profile"}])
    assert people["github/1"]["curated_override_reason"] == "FSFE profile"
    assert people["npm/x"]["curated_override_reason"] == ""  # non-github platform untouched


def test_sponsors_by_login_and_owner_sponsors_by_repo():
    ms_rows = [{"login": "Alice", "has_sponsors_listing": "True"}]
    assert bp._sponsors_by_login(ms_rows) == {"alice": "True"}
    s_rows = [{"repo_id": "gh/1", "gh_sponsors_enabled": "False"}]
    assert bp._owner_sponsors_by_repo(s_rows) == {"gh/1": "False"}


def test_users_by_login_keys_lowercased():
    rows = [{"login": "Alice", "name": "Alice A"}]
    assert bp._users_by_login(rows) == {"alice": {"login": "Alice", "name": "Alice A"}}


def _write(path, header, rows):
    import csv as _csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_build_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "PREVIEW_REPOS_FILE", tmp_path / "preview-repos.csv")
    monkeypatch.setattr(bp, "REPOS_FILE", tmp_path / "repos.csv")
    monkeypatch.setattr(bp, "USERS_FILE", tmp_path / "users.csv")
    monkeypatch.setattr(bp, "FUNDING_YML_FILE", tmp_path / "funding-yml.csv")
    monkeypatch.setattr(bp, "SPONSORS_FILE", tmp_path / "sponsors.csv")
    monkeypatch.setattr(bp, "MAINTAINER_SPONSORS_FILE", tmp_path / "maintainer-sponsors.csv")
    monkeypatch.setattr(bp, "ECOSYSTEM_MAINTAINERS_FILE", tmp_path / "eco-maintainers.csv")
    monkeypatch.setattr(bp, "MAINTAINER_OVERRIDES_FILE", tmp_path / "overrides.csv")

    _write(tmp_path / "preview-repos.csv", ["repo_id", "repo"], [["gh/1", "acme/widget"]])
    _write(tmp_path / "repos.csv", ["repo_id", "owner_login", "owner_type"],
           [["gh/1", "alice", "User"]])
    _write(tmp_path / "users.csv",
           ["login", "user_id", "name", "email", "html_url", "company", "bio"],
           [["alice", "1", "Alice A", "a@example.com", "https://github.com/alice", "", ""]])
    _write(tmp_path / "funding-yml.csv", ["repo_id", "github"], [["gh/1", "alice, bob"]])
    _write(tmp_path / "sponsors.csv", ["repo_id", "gh_sponsors_enabled"], [["gh/1", "True"]])
    _write(tmp_path / "maintainer-sponsors.csv", ["login", "has_sponsors_listing"], [])
    _write(tmp_path / "eco-maintainers.csv",
           ["ecosystem", "package", "repo_id", "login", "name", "email", "uuid", "html_url"],
           [["npm", "widget", "gh/1", "carol", "Carol C", "c@example.com", "carol",
             "https://npmjs.com/~carol"]])
    _write(tmp_path / "overrides.csv", ["login", "reason"], [["alice", "known fundable"]])

    monkeypatch.setattr(
        bp, "_key_contributor_pairs",
        lambda target_repo_ids, cum_share: [("alice", "gh/1")] if "gh/1" in target_repo_ids else [],
    )

    rows = bp.build()
    by_login = {r["login"]: r for r in rows}

    assert set(by_login) == {"alice", "bob", "carol"}

    alice = by_login["alice"]
    assert alice["platform"] == "github"
    assert alice["person_id"] == "github/1"
    assert alice["owner_repo_ids"] == "gh/1"
    assert alice["key_contributor_repo_ids"] == "gh/1"
    assert alice["funding_yml_maintainer_repo_ids"] == "gh/1"
    assert alice["repo_ids"] == "gh/1"
    assert alice["repo_count"] == "1"
    assert alice["has_sponsors_listing"] == "True"       # via owner sponsors.csv fallback
    assert alice["curated_override_reason"] == "known fundable"

    bob = by_login["bob"]
    assert bob["person_id"] == "github:bob"              # no users.csv entry -> login fallback
    assert bob["funding_yml_maintainer_repo_ids"] == "gh/1"
    assert bob["owner_repo_ids"] == ""

    carol = by_login["carol"]
    assert carol["platform"] == "npm"
    assert carol["person_id"] == "npm/carol"
    assert carol["ecosystem_maintainer_repo_ids"] == "gh/1"
