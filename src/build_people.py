#!/usr/bin/env python3
"""Build data/preview/people.csv — owners and key contributors of the
pipeline's final eligible-repo list, for outreach review.

Reads only already-fetched data (see docs/superpowers/specs/
2026-07-06-people-preview-design.md for the full design). One row per
(platform, login-or-user_id) — a person who appears on multiple platforms
(e.g. a GitHub owner who is also an npm registry maintainer) gets one row
per platform, not merged. Within one platform, one identity is one row even
across multiple target repos and roles; each role's contribution accumulates
into that role's own `*_repo_ids` column.

Roles (all pre-existing data except ecosystem_maintainer, a new extraction —
see src.sources.ecosystems.fetch_maintainers):
    owner_repo_ids                    data/sources/github/repos.csv
    key_contributor_repo_ids          src.sources.github.key_contributors
    funding_yml_maintainer_repo_ids   data/sources/github/funding-yml.csv
    ecosystem_maintainer_repo_ids     data/sources/ecosystems/maintainers.csv
    curated_override_reason          data/eligibility/maintainer-overrides.csv
                                      (tags an existing row only; not repo-scoped)

Bot logins (src.sources.github.models.is_bot) never get a GitHub-platform
row — filtered at the point a role would otherwise create one.

Usage:
    uv run python -m src.build_people
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.sources.github.models import is_bot

log = logging.getLogger(__name__)
console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_FILE = DATA_DIR / "results.csv"
REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
USERS_FILE = DATA_DIR / "sources" / "github" / "users.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
MAINTAINER_SPONSORS_FILE = DATA_DIR / "sources" / "github" / "maintainer-sponsors.csv"
ECOSYSTEM_MAINTAINERS_FILE = DATA_DIR / "sources" / "ecosystems" / "maintainers.csv"
MAINTAINER_OVERRIDES_FILE = DATA_DIR / "eligibility" / "maintainer-overrides.csv"
OUTPUT_FILE = DATA_DIR / "preview" / "people.csv"

ROLE_COLUMNS = [
    "owner_repo_ids", "key_contributor_repo_ids",
    "funding_yml_maintainer_repo_ids", "ecosystem_maintainer_repo_ids",
]

FIELDS = [
    "person_id", "platform", "login", "user_id", "name", "emails",
    "profile_url", "company", "bio",
    *ROLE_COLUMNS,
    "curated_override_reason",
    "repo_ids", "repo_count", "has_sponsors_listing",
]


def _person_id(platform: str, login: str, user_id: str, email: str) -> str:
    """`<platform>/<user_id>` when a numeric/opaque platform id is known, else
    `email:<addr>`, else `<platform>:<login>` as the last-resort fallback."""
    if user_id:
        return f"{platform}/{user_id}"
    if email:
        return f"email:{email.strip().lower()}"
    return f"{platform}:{login.strip().lower()}"


def _new_person(person_id: str, platform: str, login: str, user_id: str) -> dict:
    return {
        "person_id": person_id, "platform": platform, "login": login,
        "user_id": user_id, "name": "", "emails": set(), "profile_url": "",
        "company": "", "bio": "",
        "owner_repo_ids": set(), "key_contributor_repo_ids": set(),
        "funding_yml_maintainer_repo_ids": set(),
        "ecosystem_maintainer_repo_ids": set(),
        "curated_override_reason": "", "has_sponsors_listing": "",
    }


def _add_github_contribution(
    people: dict[str, dict], role_column: str, login: str, repo_id: str,
    users_by_login: dict[str, dict],
) -> None:
    """Record one (login, repo_id) contribution to a GitHub-platform role.

    Looks up `login` in the pre-loaded `users.csv` map for a numeric
    `user_id` and profile fields; a login absent from that map (never an
    owner elsewhere, so never GitHub-API-fetched) still gets a row, keyed by
    `github:<login>` instead of `github/<id>`.
    """
    login = login.strip().lower()
    if not login or is_bot(login):
        return
    profile = users_by_login.get(login, {})
    user_id = (profile.get("user_id") or "").strip()
    email = (profile.get("email") or "").strip()
    person_id = _person_id("github", login, user_id, email)
    person = people.get(person_id)
    if person is None:
        person = _new_person(person_id, "github", login, user_id)
        people[person_id] = person
    if not person["name"]:
        person["name"] = (profile.get("name") or "").strip()
    if email:
        person["emails"].add(email.lower())
    if not person["profile_url"]:
        person["profile_url"] = (profile.get("html_url") or "").strip()
    if not person["company"]:
        person["company"] = (profile.get("company") or "").strip()
    if not person["bio"]:
        person["bio"] = (profile.get("bio") or "").strip()
    person[role_column].add(repo_id)


def _add_ecosystem_contribution(people: dict[str, dict], row: dict) -> None:
    """Record one ecosyste.ms registry-maintainer row (platform = ecosystem)."""
    login = (row.get("login") or "").strip()
    if not login:
        return
    platform = row["ecosystem"]
    user_id = (row.get("uuid") or "").strip()
    email = (row.get("email") or "").strip()
    person_id = _person_id(platform, login, user_id, email)
    person = people.get(person_id)
    if person is None:
        person = _new_person(person_id, platform, login, user_id)
        people[person_id] = person
    if not person["name"]:
        person["name"] = (row.get("name") or "").strip()
    if email:
        person["emails"].add(email.lower())
    if not person["profile_url"]:
        person["profile_url"] = (row.get("html_url") or "").strip()
    repo_id = (row.get("repo_id") or "").strip()
    if repo_id:
        person["ecosystem_maintainer_repo_ids"].add(repo_id)


def _finalize_row(person: dict) -> dict[str, str]:
    """Convert one in-progress person dict into its final CSV row, unioning
    the four role columns into the `repo_ids`/`repo_count` rollup.

    Builds keys in the exact same order as `FIELDS` (person/profile fields,
    then the four role columns, then the rollup) so `list(row.keys()) ==
    FIELDS` — `csv.DictWriter` doesn't require this, but keeping the two in
    lockstep avoids a column silently drifting out of sync with the schema.
    """
    role_repo_ids: set[str] = set()
    for col in ROLE_COLUMNS:
        role_repo_ids |= person[col]
    row = {
        "person_id": person["person_id"], "platform": person["platform"],
        "login": person["login"], "user_id": person["user_id"],
        "name": person["name"], "emails": "|".join(sorted(person["emails"])),
        "profile_url": person["profile_url"], "company": person["company"],
        "bio": person["bio"],
    }
    for col in ROLE_COLUMNS:
        row[col] = "|".join(sorted(person[col]))
    row["curated_override_reason"] = person["curated_override_reason"]
    row["repo_ids"] = "|".join(sorted(role_repo_ids))
    row["repo_count"] = str(len(role_repo_ids))
    row["has_sponsors_listing"] = person["has_sponsors_listing"]
    return row
