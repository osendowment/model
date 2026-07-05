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
