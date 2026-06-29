"""Fetch declared funding channels per risk-scope repo, via GitHub GraphQL.

Source: `repository.fundingLinks` — GitHub's *resolved* funding links, i.e.
exactly what the "Sponsor this project" sidebar widget shows. This resolves a
repo's FUNDING.yml wherever it lives (`.github/`, repo root, `docs/`, or an
org-level `.github` default), so it catches channels a raw
`.github/FUNDING.yml` GET would miss. Example: tukaani-project/xz declares
`liberapay.com/Larhzu` through the widget with no FUNDING.yml in the repo tree
at all — the old contents-API fetch recorded it as unfunded.

Writes data/sources/github/funding-yml.csv:
    repo, repo_id, has_funding_link, funding_link_platforms,
    <one column per platform in FUNDING_PLATFORMS>, fetched_at

`has_funding_link` = the repo declares at least one funding link (the columns
are named for the GraphQL `fundingLinks` source, not a raw FUNDING.yml file).
`funding_link_platforms` = the canonical platform keys present (github, patreon, …).
Each platform column holds that platform's handle(s) (comma-joined for a list):
the `github` column is the lower-cased, deduped sponsor logins (so the sponsors
fetcher can count co-maintainer sponsorships); `custom` keeps the full URL.

GitHub's GraphQL `FundingPlatform` enum covers every platform in
FUNDING_PLATFORMS except `otechie` (a dead platform GitHub dropped) — its
column stays in the schema but is never populated here.

TTL-controlled: re-runs only fetch repos missing or older than the shared
funding TTL (FUNDING_TTL_DAYS = 365), so a re-run inside the window is a no-op.
Repos are fetched in GraphQL batches (one aliased query per BATCH_SIZE repos),
which is far cheaper than the previous one-REST-call-per-repo approach.

Usage:
    uv run python -m src.sources.github.fetch_funding_yml
    uv run python -m src.sources.github.fetch_funding_yml --limit 20
    uv run python -m src.sources.github.fetch_funding_yml --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import re
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

from src.common.freshness import row_is_fresh
from src.common.funding_platforms import FUNDING_PLATFORMS
from src.common.repos import load_top_repos
from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = (
    ["repo", "repo_id", "has_funding_link", "funding_link_platforms"]
    + FUNDING_PLATFORMS
    + ["fetched_at"]
)
BATCH_SIZE = 50

_PLATFORM_SET = set(FUNDING_PLATFORMS)


# ── pure transforms (GraphQL fundingLinks → funding-yml.csv row) ──────────────

def platform_key(enum_name: str) -> str:
    """Map a GraphQL `FundingPlatform` enum value to our column key.

    The mapping is just lower-casing (`LIBERAPAY` → `liberapay`). An unknown
    value (e.g. a platform GitHub adds in the future) falls back to `custom`
    so the link is never silently dropped.
    """
    key = (enum_name or "").strip().lower()
    return key if key in _PLATFORM_SET else "custom"


def handle_from_link(platform: str, url: str) -> str:
    """Extract a platform handle from a funding link URL.

    For known platforms this is the last path segment
    (`github.com/yyx990803` → `yyx990803`, `opencollective.com/vuejs` →
    `vuejs`, `thanks.dev/u/gh/prettier` → `prettier`). `custom` keeps the full
    URL (it is a free-form address — preserve it for auditability). A URL with
    no path (bare host) falls back to the full URL.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if platform == "custom":
        return u
    path = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u, flags=re.IGNORECASE)
    path = path.split("/", 1)[1] if "/" in path else ""
    path = re.split(r"[?#]", path, maxsplit=1)[0].strip("/")
    if not path:
        return u
    return path.rsplit("/", 1)[-1]


def build_row(repo: str, funding_links: list[dict]) -> dict:
    """Build one funding-yml.csv row from a repo's GraphQL fundingLinks list.

    `funding_links` is `[{"platform": "<ENUM>", "url": "<url>"}, ...]`. Handles
    are grouped per canonical platform, in first-seen order, deduped. `github`
    logins are additionally lower-cased (the sponsors fetcher comma-splits and
    lower-cases this column to count co-maintainer sponsorships).
    """
    by_platform: dict[str, list[str]] = {}
    for link in funding_links:
        key = platform_key(link.get("platform", ""))
        handle = handle_from_link(key, link.get("url", ""))
        if not handle:
            continue
        if key == "github":
            handle = handle.lower()
        bucket = by_platform.setdefault(key, [])
        if handle not in bucket:
            bucket.append(handle)
    row = {
        "repo": repo,
        "has_funding_link": "True" if by_platform else "False",
        "funding_link_platforms": ",".join(by_platform),
    }
    for p in FUNDING_PLATFORMS:
        row[p] = ",".join(by_platform.get(p, []))
    return row


def build_query(batch: list[str]) -> tuple[str, dict]:
    """Build an aliased GraphQL query + variables for a batch of `owner/name`.

    Owner/name go through GraphQL variables (not string interpolation) so the
    query is injection-safe regardless of repo slug contents.
    """
    decls: list[str] = []
    selections: list[str] = []
    variables: dict[str, str] = {}
    for i, repo in enumerate(batch):
        owner, _, name = repo.partition("/")
        decls.append(f"$o{i}:String!,$n{i}:String!")
        selections.append(
            f"r{i}: repository(owner:$o{i}, name:$n{i}) "
            f"{{ fundingLinks {{ platform url }} }}"
        )
        variables[f"o{i}"] = owner
        variables[f"n{i}"] = name
    query = "query(" + ",".join(decls) + "){\n  " + "\n  ".join(selections) + "\n}"
    return query, variables


def rows_from_response(batch: list[str], body: dict) -> dict[str, dict]:
    """Turn a GraphQL response body into {repo: row} for the batch.

    A repo whose alias errored with `NOT_FOUND` (deleted/renamed) is recorded
    as "checked, no funding" (`has_funding_link=False`). A null alias *without*
    a NOT_FOUND error is a fetch failure — it is skipped, not written as False,
    so a transient error never masquerades as "no funding" (auditability).
    """
    payload = body.get("data")
    errors = body.get("errors") or []
    if payload is None:
        raise RuntimeError(f"GraphQL returned no data: {errors[:2]}")
    not_found = {
        e["path"][0]
        for e in errors
        if e.get("type") == "NOT_FOUND" and e.get("path")
    }
    rows: dict[str, dict] = {}
    for i, repo in enumerate(batch):
        alias = f"r{i}"
        node = payload.get(alias)
        if node is not None:
            rows[repo] = build_row(repo, node.get("fundingLinks") or [])
        elif alias in not_found:
            rows[repo] = build_row(repo, [])
        else:
            log.warning("funding-yml: no data for %s (errors: %s)", repo, errors[:1])
    return rows


# ── fetch / batch orchestration ──────────────────────────────────────────────

async def fetch_batch(session, limiter, batch: list[str]) -> dict[str, dict]:
    """Fetch one batch of repos' funding links. Raises RuntimeError on failure."""
    query, variables = build_query(batch)
    try:
        body = await _graphql(session, limiter, query, variables)
    except _Deferred as e:
        raise RuntimeError("transient network error") from e
    return rows_from_response(batch, body)


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _load_repo_id_map() -> dict[str, str]:
    out: dict[str, str] = {}
    if GH_REPOS_FILE.exists():
        with open(GH_REPOS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("repo") or "").strip().lower()
                rid = (row.get("repo_id") or "").strip()
                if slug and rid:
                    out[slug] = rid
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
    repo_ids = _load_repo_id_map()
    fresh = set() if force else {r for r, row in existing.items() if row_is_fresh(row)}
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    chunks = [to_fetch[i:i + BATCH_SIZE] for i in range(0, len(to_fetch), BATCH_SIZE)]
    console.print(
        f"[bold]funding-yml[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch "
        f"in {len(chunks)} batch(es) of ≤{BATCH_SIZE}"
    )
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(chunk: list[str]) -> tuple[list[str], dict[str, dict]]:
        async with sem:
            try:
                return chunk, await fetch_batch(session, limiter, chunk)
            except RuntimeError as e:
                log.warning("funding-yml batch failed (%d repos): %s", len(chunk), e)
                return chunk, {}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("funding-yml", total=len(to_fetch))
            for coro in asyncio.as_completed([one(c) for c in chunks]):
                chunk, rows = await coro
                now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                for repo, row in rows.items():
                    row["repo_id"] = repo_ids.get(repo, "")
                    row["fetched_at"] = now
                    existing[repo] = row
                prog.advance(task, advance=len(chunk))
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
