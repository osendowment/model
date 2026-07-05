"""Fetch personal GitHub Sponsors status for each top repo's bus-factor maintainers.

Writes data/sources/github/maintainer-sponsors.csv:
    user_id, login, has_sponsors_listing, status, fetched_at

`fetch_sponsors` asks whether a repo's *owner* has Sponsors enabled — for a repo
under a project org (`acornjs/acorn`) that owner is the org, which almost never
has a listing even though the maintainer who wrote the repo does. This fetcher
closes that gap: it takes every repo's **bus-factor contributors** (the people
who wrote ≥50% of it — `src.sources.github.bf_contributors`) and checks whether
*they personally* have a GitHub Sponsors listing. `build_funding` then treats "a
maintainer who carries this repo is fundable" as a funding-intent signal
(`bf_maintainer_fundable`).

The query/skip unit is the **login**: `in_scope_bf_logins` unions the bus-factor
logins into a distinct set, so each login is fetched once even when the person
carries several in-scope repos (dtolnay → serde/syn/quote…), and the TTL gate
re-keys on login. Rows carry the account's immutable numeric **user id**
(`databaseId`) as the stable identity — the file is sorted by it, it is the audit
key, and it collapses a person to one identity across repos (mirroring the
`gh/<n>` repo_id convention). `login` is stored for the join back from
`contributor-commits.csv` (which is login-keyed today) and survives as the join
key even if the account is later renamed.

`has_sponsors_listing`: True if the account has an active GitHub Sponsors
listing. `status`: "ok" when the login resolved (to a user or org) — including
a genuine "resolved, not fundable"; "error" when the GraphQL query failed, so a
False from a network failure is never mistaken for a real negative.

Usage:
    uv run python -m src.sources.github.fetch_maintainer_sponsors
    uv run python -m src.sources.github.fetch_maintainer_sponsors --limit 20
    uv run python -m src.sources.github.fetch_maintainer_sponsors --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.common.freshness import funding_ttl_for, row_is_fresh
from src.common.repos import load_top_repos
from src.sources.github.bf_contributors import CONTRIB_FILE, load_bf_contributors
from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "maintainer-sponsors.csv"
FIELDS = ["user_id", "login", "has_sponsors_listing", "status", "fetched_at"]

# databaseId is the stable numeric account id (mirrors the gh/<n> repo_id
# convention); a bus-factor contributor is a person, but query the org branch
# too so an org-account contributor resolves rather than erroring.
LISTING_QUERY = """
query($login: String!) {
  user(login: $login) { databaseId hasSponsorsListing }
  organization(login: $login) { databaseId hasSponsorsListing }
}
"""


def in_scope_bf_logins() -> list[str]:
    """Distinct bus-factor logins across the eligibility scope (top repos incl.
    archived), lowercased. One entry per person even across many repos."""
    scope = {e.repo_id for e in load_top_repos(skip_archived=False) if e.repo_id}
    bf = load_bf_contributors(CONTRIB_FILE)
    logins = {l for rid, members in bf.items() if rid in scope for l in members}
    return sorted(logins)


async def fetch_listing_for_login(session, limiter, login: str) -> tuple[str, bool, bool]:
    """(user_id, has_sponsors_listing, ok). ok=False only on a failed/deferred query.

    A login resolves to either a user OR an organization (the other is null), so
    the id/listing is whichever one resolved. Both null = a resolved "not found"
    (ok=True, no id, not fundable) — distinct from a query failure (ok=False).
    """
    if not login:
        return "", False, True
    try:
        result = await _graphql(session, limiter, LISTING_QUERY, {"login": login})
    except _Deferred:
        return "", False, False
    except RuntimeError as e:
        log.warning("graphql failed for %s: %s", login, e)
        return "", False, False
    data = result.get("data") or {}
    node = data.get("user") or data.get("organization") or {}
    db_id = node.get("databaseId")
    listing = bool(node.get("hasSponsorsListing"))
    return (str(db_id) if db_id else ""), listing, True


async def fetch_one(session, limiter, login: str) -> dict:
    user_id, listing, ok = await fetch_listing_for_login(session, limiter, login)
    return {
        "login": login,
        "user_id": user_id,
        "has_sponsors_listing": "True" if listing else "False",
        "status": "ok" if ok else "error",
    }


def _load_existing() -> dict[str, dict]:
    """{login: row} — login is the re-run/skip key (the query unit)."""
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                login = (row.get("login") or "").strip().lower()
                if login:
                    out[login] = row
    return out


def _has_listing(row: dict) -> bool:
    return (row.get("has_sponsors_listing") or "").strip() == "True"


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Sort by numeric user id (the stable identity); unresolved rows (no id) last.
    def _key(login: str) -> tuple[int, int, str]:
        uid = (rows[login].get("user_id") or "").strip()
        return (0, int(uid), login) if uid.isdigit() else (1, 0, login)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for login in sorted(rows, key=_key):
            w.writerow(rows[login])


async def batch(logins: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    # Idempotent within TTL; "error" status is never fresh (always retried); a
    # not-yet-fundable maintainer is rechecked on the shorter empty window — they
    # are the ones who may open Sponsors and cheap to re-query.
    fresh = set() if force else {
        login for login, row in existing.items()
        if row_is_fresh(row, funding_ttl_for(_has_listing(row)), status_key="status")
    }
    to_fetch = [l for l in logins if l not in fresh]
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    console.print(f"[bold]maintainer-sponsors[/bold]: {len(logins)} BF maintainers, "
                  f"{len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(login: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, login)
            except RuntimeError as e:
                log.warning("maintainer-sponsors failed for %s: %s", login, e)
                return {"login": login, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("maintainer-sponsors", total=len(to_fetch))
            for coro in asyncio.as_completed([one(l) for l in to_fetch]):
                res = await coro
                prog.advance(task)
                if "_error" in res:
                    continue
                res["fetched_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds")
                existing[res["login"]] = res
                _write(existing)
    console.print(f"[green]done[/green] → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    logins = in_scope_bf_logins()
    asyncio.run(batch(logins, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
