"""Fetch outbound GitHub sponsoring counts per repo-owner account.

For each distinct owner of a risk-scope repo, query `sponsorshipsAsSponsor` —
how many accounts the owner sponsors (the org's "Sponsoring N"). This is a
backer / corporate-funding signal, tracked separately from inbound sponsors
(src.github.fetch_sponsors). Outbound sponsoring is an account-level property,
so the table is keyed by `login`, not by repo.

Writes data/sources/github/sponsorships.csv:
    login, sponsoring_count, sponsoring_status, fetched_at

`sponsoring_status`: "ok" (login resolved) or "error" (query failed — so a 0
from a failure is distinguishable from a genuine 0).

Gap-filling: a normal run only fetches owner logins missing from the file or
older than the TTL; pass --force to refetch all.

Usage:
    uv run python -m src.github.fetch_sponsorships
    uv run python -m src.github.fetch_sponsorships --limit 20
    uv run python -m src.github.fetch_sponsorships --force
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

from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql
from src.common.repos import load_risk_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "sponsorships.csv"
FIELDS = ["login", "sponsoring_count", "sponsoring_status", "fetched_at"]
TTL_DAYS = 90

SPONSORING_QUERY = """
query($login: String!) {
  user(login: $login) { sponsorshipsAsSponsor(first: 1) { totalCount } }
  organization(login: $login) { sponsorshipsAsSponsor(first: 1) { totalCount } }
}
"""


def sponsoring_count(data: dict) -> int:
    """Outbound sponsoring totalCount from a GraphQL `data` payload (user|org)."""
    node = (data or {}).get("user") or (data or {}).get("organization") or {}
    return int((node.get("sponsorshipsAsSponsor") or {}).get("totalCount") or 0)


def owner_logins() -> list[str]:
    """Distinct lower-cased owner logins across the risk-scope repos."""
    return sorted({e.repo.split("/", 1)[0].lower() for e in load_risk_repos() if e.repo})


async def fetch_one(session, limiter, login: str) -> dict:
    try:
        result = await _graphql(session, limiter, SPONSORING_QUERY, {"login": login})
    except _Deferred:
        return {"login": login, "sponsoring_status": "error"}
    except RuntimeError as e:
        log.warning("graphql failed for %s: %s", login, e)
        return {"login": login, "sponsoring_status": "error"}
    return {
        "login": login,
        "sponsoring_count": str(sponsoring_count(result.get("data") or {})),
        "sponsoring_status": "ok",
    }


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["login"]] = row
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for login in sorted(rows):
            w.writerow(rows[login])


def _is_fresh(row: dict, ttl_days: int) -> bool:
    # A failed fetch is never fresh — always retry errored rows.
    if (row.get("sponsoring_status") or "").strip() == "error":
        return False
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


async def batch(logins: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    fresh = set() if force else {l for l, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    to_fetch = [l for l in logins if l not in fresh]  # gap-fill: only missing/stale
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    console.print(f"[bold]sponsorships[/bold]: {len(logins)} owner logins, "
                  f"{len(to_fetch)} to fetch, {len(logins)-len(to_fetch)} already filled")
    if not to_fetch:
        console.print("[dim]No gaps to fill.[/dim]")
        return

    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(login: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, login)
            except RuntimeError as e:
                log.warning("sponsorships failed for %s: %s", login, e)
                return {"login": login, "sponsoring_status": "error"}

    async with aiohttp.ClientSession(headers={"Accept": "application/vnd.github+json"}) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("sponsorships", total=len(to_fetch))
            for coro in asyncio.as_completed([one(l) for l in to_fetch]):
                res = await coro
                prog.advance(task)
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
    asyncio.run(batch(owner_logins(), args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
