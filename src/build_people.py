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

from src.common.params import KEY_CONTRIBUTORS_CUM_SHARE
from src.sources.github.key_contributors import load_key_contributors
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


def _target_repo_ids(results_rows: list[dict]) -> set[str]:
    return {(r.get("repo_id") or "").strip() for r in results_rows
            if (r.get("repo_id") or "").strip()}


def _users_by_login(users_rows: list[dict]) -> dict[str, dict]:
    return {(r.get("login") or "").strip().lower(): r for r in users_rows
            if (r.get("login") or "").strip()}


def _org_logins(repos_rows: list[dict]) -> set[str]:
    """Every login known to be a GitHub Organization (any repo in repos.csv,
    not just the target scope — an org doesn't stop being an org outside it)."""
    return {(r.get("owner_login") or "").strip().lower() for r in repos_rows
            if (r.get("owner_type") or "").strip() == "Organization"
            and (r.get("owner_login") or "").strip()}


def _owner_pairs(repos_rows: list[dict], target_repo_ids: set[str]) -> list[tuple[str, str]]:
    """[(owner_login, repo_id), ...] for User-type owners of target repos.
    Organization-owned repos are skipped entirely — not a person."""
    out = []
    for row in repos_rows:
        repo_id = (row.get("repo_id") or "").strip()
        if repo_id not in target_repo_ids:
            continue
        if (row.get("owner_type") or "").strip() != "User":
            continue
        login = (row.get("owner_login") or "").strip()
        if login:
            out.append((login, repo_id))
    return out


def _key_contributor_pairs(
    target_repo_ids: set[str], cum_share: float,
) -> list[tuple[str, str]]:
    """[(login, repo_id), ...] from the key-contributor lookup (Task 1),
    using an independently configurable cum_share."""
    by_repo = load_key_contributors(cum_share)
    return [(login, repo_id) for repo_id, logins in by_repo.items()
            if repo_id in target_repo_ids for login in logins]


def _funding_yml_pairs(
    funding_yml_rows: list[dict], target_repo_ids: set[str], org_logins: set[str],
) -> list[tuple[str, str]]:
    """[(login, repo_id), ...] from FUNDING.yml's `github` column, dropping
    any name that resolves to a known Organization (FUNDING.yml sometimes
    names an org, and this role is for people)."""
    out = []
    for row in funding_yml_rows:
        repo_id = (row.get("repo_id") or "").strip()
        if repo_id not in target_repo_ids:
            continue
        names = [n.strip() for n in (row.get("github") or "").split(",") if n.strip()]
        for login in names:
            if login.lower() in org_logins:
                continue
            out.append((login, repo_id))
    return out


def _ecosystem_maintainer_rows(
    maintainer_rows: list[dict], target_repo_ids: set[str],
) -> list[dict]:
    return [r for r in maintainer_rows
            if (r.get("repo_id") or "").strip() in target_repo_ids]


def _apply_curated_overrides(people: dict[str, dict], overrides_rows: list[dict]) -> None:
    """Tag an existing GitHub-platform row with its curated reason.

    Not repo-scoped (maintainer-overrides.csv has no repo_id), so this only
    annotates a person who already surfaced via another role — it never
    creates a new row.
    """
    reasons_by_login = {(r.get("login") or "").strip().lower(): (r.get("reason") or "").strip()
                        for r in overrides_rows if (r.get("login") or "").strip()}
    for person in people.values():
        if person["platform"] != "github":
            continue
        reason = reasons_by_login.get(person["login"].lower())
        if reason:
            person["curated_override_reason"] = reason


def _sponsors_by_login(maintainer_sponsors_rows: list[dict]) -> dict[str, str]:
    return {(r.get("login") or "").strip().lower():
            (r.get("has_sponsors_listing") or "").strip()
            for r in maintainer_sponsors_rows if (r.get("login") or "").strip()}


def _owner_sponsors_by_repo(sponsors_rows: list[dict]) -> dict[str, str]:
    return {(r.get("repo_id") or "").strip(): (r.get("gh_sponsors_enabled") or "").strip()
            for r in sponsors_rows if (r.get("repo_id") or "").strip()}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build() -> list[dict[str, str]]:
    target_repo_ids = _target_repo_ids(_read_csv(RESULTS_FILE))
    repos_rows = _read_csv(REPOS_FILE)
    users_by_login = _users_by_login(_read_csv(USERS_FILE))
    org_logins = _org_logins(repos_rows)

    people: dict[str, dict] = {}

    for login, repo_id in _owner_pairs(repos_rows, target_repo_ids):
        _add_github_contribution(people, "owner_repo_ids", login, repo_id, users_by_login)

    for login, repo_id in _key_contributor_pairs(target_repo_ids, KEY_CONTRIBUTORS_CUM_SHARE):
        _add_github_contribution(people, "key_contributor_repo_ids", login, repo_id, users_by_login)

    funding_yml_rows = _read_csv(FUNDING_YML_FILE)
    for login, repo_id in _funding_yml_pairs(funding_yml_rows, target_repo_ids, org_logins):
        _add_github_contribution(people, "funding_yml_maintainer_repo_ids", login, repo_id,
                                 users_by_login)

    eco_rows = _ecosystem_maintainer_rows(_read_csv(ECOSYSTEM_MAINTAINERS_FILE), target_repo_ids)
    for row in eco_rows:
        _add_ecosystem_contribution(people, row)

    _apply_curated_overrides(people, _read_csv(MAINTAINER_OVERRIDES_FILE))

    sponsors_by_login = _sponsors_by_login(_read_csv(MAINTAINER_SPONSORS_FILE))
    owner_sponsors_by_repo = _owner_sponsors_by_repo(_read_csv(SPONSORS_FILE))
    for person in people.values():
        if person["platform"] != "github":
            continue
        listing = sponsors_by_login.get(person["login"].lower(), "")
        if not listing:
            for repo_id in person["owner_repo_ids"]:
                listing = owner_sponsors_by_repo.get(repo_id, "")
                if listing:
                    break
        person["has_sponsors_listing"] = listing

    return [_finalize_row(p) for p in people.values()]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    console.print("[bold]Building people.csv...[/bold]\n")
    rows = build()
    rows.sort(key=lambda r: (r["platform"], r["login"]))

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    table = Table(title="[bold]People coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Role", style="bold")
    table.add_column("People", justify="right")
    for col, label in zip(ROLE_COLUMNS,
                          ("Owners", "Key contributors", "FUNDING.yml maintainers",
                           "Ecosystem maintainers")):
        n = sum(1 for r in rows if r[col])
        table.add_row(label, f"{n:,}")
    table.add_row("[bold]Total people[/bold]", f"[bold]{len(rows):,}[/bold]")
    console.print(table)
    console.print(f"\n[dim]Wrote {len(rows):,} rows -> {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
