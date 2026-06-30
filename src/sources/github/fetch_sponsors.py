"""Fetch GitHub Sponsors counts per risk-scope repo.

Writes data/sources/github/sponsors.csv:
    repo, repo_id, github_sponsors, owner_has_sponsors_listing, sponsors_status, fetched_at

`github_sponsors` (inbound) = public sponsorships *received* by the repo OWNER
only. Sponsors are attributed to a repo solely when the sponsored account owns
it: a `github:` login in the repo's FUNDING.yml that is NOT the owner (a
co-maintainer) is *not* counted — their sponsors fund that person's whole
portfolio, not this one repo, so crediting them here over-states the repo's
funding. The FUNDING.yml link still marks the repo as having a funding channel
(`has_funding_link`, handled by build_funding); this fetcher just measures the
owner's actual sponsor count. Outbound sponsoring is a separate signal — see
src.github.fetch_sponsorships.
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

from src.common.freshness import funding_ttl_for, row_is_fresh
from src.common.repos import load_top_repos
from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = ["repo", "repo_id", "gh_sponsors", "has_gh_sponsors",
          "sponsors_status", "fetched_at"]

SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) {
    hasSponsorsListing sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount }
  }
  organization(login: $login) {
    hasSponsorsListing sponsorshipsAsMaintainer(first: 1, includePrivate: false) { totalCount }
  }
}
"""


def logins_for_repo(repo: str) -> list[str]:
    """The login whose inbound sponsors count for this repo: its OWNER, only.

    Sponsors are attributed to a repo solely when the sponsored account owns it.
    A co-maintainer named in FUNDING.yml does not own the repo, and their personal
    sponsors fund their whole portfolio rather than this project — so we do not
    count them here (the FUNDING.yml link is still a funding-channel signal,
    surfaced separately via has_funding_link)."""
    return [repo.split("/", 1)[0].lower()]


def status_from_counts(counts: list[int], any_error: bool) -> str:
    """"error" if any queried login failed, else "ok"."""
    return "error" if any_error else "ok"


def _has_sponsor_signal(row: dict) -> bool:
    """True if a stored row already carries a sponsor signal — a non-zero public
    sponsor count or GitHub Sponsors enabled (an active listing). Rows without one
    are rechecked on the shorter funding window (they may gain sponsors)."""
    try:
        count = int((row.get("gh_sponsors") or "0").strip() or "0")
    except ValueError:
        count = 0
    enabled = (row.get("has_gh_sponsors") or "").strip() == "True"
    return count > 0 or enabled


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


async def fetch_one(session, limiter, repo: str) -> dict:
    # Only the repo OWNER's sponsors count (see logins_for_repo / module docstring).
    (owner,) = logins_for_repo(repo)
    count, enabled, ok = await fetch_sponsors_for_login(session, limiter, owner)
    return {
        "repo": repo,
        "gh_sponsors": str(count),
        "has_gh_sponsors": "True" if enabled else "False",
        "sponsors_status": status_from_counts([count], not ok),
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


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    repo_ids = _load_map(GH_REPOS_FILE, "repo", "repo_id")
    # A re-run is a no-op within the TTL (idempotent); an "error" sponsors_status
    # is never fresh, so failed fetches are always retried. A repo with no sponsor
    # signal yet (0 sponsors AND no owner listing) is rechecked on the shorter
    # window — it is the one likely to gain sponsors and cheap to re-query.
    fresh = set() if force else {
        r for r, row in existing.items()
        if row_is_fresh(row, funding_ttl_for(_has_sponsor_signal(row)),
                        status_key="sponsors_status")
    }
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    console.print(f"[bold]sponsors[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, repo)
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
