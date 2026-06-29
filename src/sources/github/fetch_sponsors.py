"""Fetch GitHub Sponsors counts per risk-scope repo.

Writes data/sources/github/sponsors.csv:
    repo, repo_id, github_sponsors, owner_has_sponsors_listing, sponsors_status, fetched_at

`github_sponsors` (inbound) = public sponsorships *received* by the repo owner
plus every `github:` login in the repo's FUNDING.yml (read from
data/sources/github/funding-yml.csv — run fetch_funding_yml first). Outbound
sponsoring is a separate signal — see src.github.fetch_sponsorships.
`owner_has_sponsors_listing` = the repo OWNER has an active GitHub Sponsors
profile (`hasSponsorsListing`), i.e. is set up to receive sponsors even when the
public count is 0 — a sustainability-intent signal in its own right. Scoped to
the owner only (not FUNDING.yml co-logins), since intent attaches to the repo's
owning entity.
`sponsors_status`: "ok" if every queried login resolved, "error" if any GraphQL
query failed (so a 0 from a failure is not mistaken for a genuine 0).

Usage:
    uv run python -m src.sources.github.fetch_sponsors
    uv run python -m src.sources.github.fetch_sponsors --limit 20
    uv run python -m src.sources.github.fetch_sponsors --force
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

from src.common.repos import load_top_repos
from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = ["repo", "repo_id", "github_sponsors", "owner_has_sponsors_listing",
          "sponsors_status", "fetched_at"]
TTL_DAYS = 90

SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) { hasSponsorsListing sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount } }
  organization(login: $login) { hasSponsorsListing sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount } }
}
"""


def logins_for_repo(repo: str, yml_github: dict[str, str]) -> list[str]:
    """Owner login + FUNDING.yml `github:` logins (deduped, owner first)."""
    owner = repo.split("/", 1)[0].lower()
    extra = [u.strip().lower() for u in (yml_github.get(repo, "") or "").split(",") if u.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for u in [owner, *extra]:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def status_from_counts(counts: list[int], any_error: bool) -> str:
    """"error" if any queried login failed, else "ok"."""
    return "error" if any_error else "ok"


async def fetch_sponsors_for_login(session, limiter, login: str) -> tuple[int, bool, bool]:
    """(public sponsor count, has_sponsors_listing, ok). ok=False on a failed/deferred query.

    A login resolves to either a user OR an organization (the other is null), so
    the count is their sum (one is 0) and the listing is either one's.
    """
    if not login:
        return 0, False, True
    try:
        result = await _graphql(session, limiter, SPONSORS_QUERY, {"login": login})
    except _Deferred:
        return 0, False, False
    except RuntimeError as e:
        log.warning("graphql failed for %s: %s", login, e)
        return 0, False, False
    data = result.get("data") or {}
    user = data.get("user") or {}
    org = data.get("organization") or {}
    count = (int((user.get("sponsorshipsAsMaintainer") or {}).get("totalCount") or 0)
             + int((org.get("sponsorshipsAsMaintainer") or {}).get("totalCount") or 0))
    listing = bool(user.get("hasSponsorsListing") or org.get("hasSponsorsListing"))
    return count, listing, True


async def fetch_one(session, limiter, repo: str, yml_github: dict[str, str]) -> dict:
    logins = logins_for_repo(repo, yml_github)
    results = await asyncio.gather(*(fetch_sponsors_for_login(session, limiter, l) for l in logins))
    total = sum(c for c, _, _ in results)
    any_error = any(not ok for _, _, ok in results)
    # logins_for_repo puts the repo OWNER first; the listing signal is owner-scoped.
    owner_listing = results[0][1] if results else False
    return {
        "repo": repo,
        "github_sponsors": str(total),
        "owner_has_sponsors_listing": "True" if owner_listing else "False",
        "sponsors_status": status_from_counts([c for c, _, _ in results], any_error),
    }


def _load_map(path: Path, key: str, val: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = (row.get(key) or "").strip().lower()
                if k:
                    out[k] = (row.get(val) or "").strip()
    return out


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def _is_fresh(row: dict, ttl_days: int) -> bool:
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    repo_ids = _load_map(GH_REPOS_FILE, "repo", "repo_id")
    yml_github = _load_map(FUNDING_YML_FILE, "repo", "github")
    fresh = set() if force else {r for r, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        import random
        to_fetch = random.sample(to_fetch, limit)
    console.print(f"[bold]sponsors[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, repo, yml_github)
            except RuntimeError as e:
                log.warning("sponsors failed for %s: %s", repo, e)
                return {"repo": repo, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("sponsors", total=len(to_fetch))
            for coro in asyncio.as_completed([one(r) for r in to_fetch]):
                res = await coro
                prog.advance(task)
                if "_error" in res:
                    continue
                res["repo_id"] = repo_ids.get(res["repo"], "")
                res["fetched_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds")
                existing[res["repo"]] = res
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
    repos = sorted({e.repo for e in load_top_repos() if e.repo})
    asyncio.run(batch(repos, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
