"""Resolve each value-scope repo to its CANONICAL GitHub identity.

GitHub silently redirects a renamed/moved repo (`facebook/react` →
`react/react`), but our registry-derived slugs can lag the move. This fetcher
asks GitHub for the ground truth — `repository.nameWithOwner` (the current
canonical `owner/name`) and `databaseId` (the immutable numeric repo id, stable
across every rename) — for every `github_repo` in `data/value/value.csv`.

Writes `data/sources/github/canonical-repos.csv`:
    repo, canonical_repo, repo_id, status, fetched_at

`repo` = the slug we asked for; `canonical_repo` = GitHub's current
`nameWithOwner` (lower-cased). When they differ the repo was renamed/moved. A
`status` of `not_found` means GitHub couldn't resolve the slug (deleted/private)
— `canonical_repo` is left equal to `repo` and `repo_id` blank, so a normaliser
can leave it untouched. `repo_id` is the join key that survives renames.

TTL-gated (`FUNDING_TTL_DAYS`); a re-run inside the window is a no-op. Fetched in
aliased GraphQL batches, so ~10k repos cost ~200 requests.

Usage:
    uv run python -m src.sources.github.fetch_canonical
    uv run python -m src.sources.github.fetch_canonical --limit 50 --force
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

from src.common.freshness import row_is_fresh
from src.common.repos import to_repo_id
from src.sources.github.github_client import _AsyncRateLimiter, _Deferred, _graphql

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "canonical-repos.csv"
FIELDS = ["repo", "canonical_repo", "repo_id", "status", "fetched_at"]
BATCH_SIZE = 50


def load_value_repos() -> list[str]:
    """Unique `github_repo` slugs from value.csv (lower-cased, `owner/name`)."""
    out: set[str] = set()
    if VALUE_FILE.exists():
        with open(VALUE_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("github_repo") or "").strip().lower()
                if slug and "/" in slug:
                    out.add(slug)
    return sorted(out)


def build_query(batch: list[str]) -> tuple[str, dict]:
    """Aliased GraphQL query for a batch of `owner/name` (variables → injection-safe)."""
    decls, selections, variables = [], [], {}
    for i, repo in enumerate(batch):
        owner, _, name = repo.partition("/")
        decls.append(f"$o{i}:String!,$n{i}:String!")
        selections.append(
            f"r{i}: repository(owner:$o{i}, name:$n{i}) {{ nameWithOwner databaseId }}"
        )
        variables[f"o{i}"] = owner
        variables[f"n{i}"] = name
    return "query(" + ",".join(decls) + "){\n  " + "\n  ".join(selections) + "\n}", variables


def rows_from_response(batch: list[str], body: dict) -> dict[str, dict]:
    """{repo: row}. NOT_FOUND → status=not_found (canonical=repo, no id). A null
    alias without NOT_FOUND is a transient failure — skipped, not written."""
    payload = body.get("data")
    errors = body.get("errors") or []
    if payload is None:
        raise RuntimeError(f"GraphQL returned no data: {errors[:2]}")
    not_found = {e["path"][0] for e in errors
                 if e.get("type") == "NOT_FOUND" and e.get("path")}
    rows: dict[str, dict] = {}
    for i, repo in enumerate(batch):
        alias = f"r{i}"
        node = payload.get(alias)
        if node is not None:
            rows[repo] = {
                "repo": repo,
                "canonical_repo": (node.get("nameWithOwner") or repo).lower(),
                "repo_id": to_repo_id(node.get("databaseId")),
                "status": "ok",
            }
        elif alias in not_found:
            rows[repo] = {"repo": repo, "canonical_repo": repo,
                          "repo_id": "", "status": "not_found"}
        else:
            log.warning("canonical: no data for %s (errors: %s)", repo, errors[:1])
    return rows


async def fetch_batch(session, limiter, batch: list[str]) -> dict[str, dict]:
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


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    # A `not_found` row is never fresh (retry deleted/renamed repos); ok rows use the TTL.
    fresh = set() if force else {
        r for r, row in existing.items()
        if row_is_fresh(row, status_key="status", error_values=("not_found", "error"))
    }
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        to_fetch = to_fetch[:limit]
    chunks = [to_fetch[i:i + BATCH_SIZE] for i in range(0, len(to_fetch), BATCH_SIZE)]
    console.print(f"[bold]canonical[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch "
                  f"in {len(chunks)} batch(es)")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(chunk):
        async with sem:
            try:
                return chunk, await fetch_batch(session, limiter, chunk)
            except RuntimeError as e:
                log.warning("canonical batch failed (%d repos): %s", len(chunk), e)
                return chunk, {}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("canonical", total=len(to_fetch))
            for coro in asyncio.as_completed([one(c) for c in chunks]):
                chunk, rows = await coro
                now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                for repo, row in rows.items():
                    row["fetched_at"] = now
                    existing[repo] = row
                prog.advance(task, advance=len(chunk))
                _write(existing)
    renamed = sum(1 for r in existing.values() if r["status"] == "ok"
                  and r["canonical_repo"] != r["repo"])
    console.print(f"[green]done[/green] → {OUTPUT_FILE}  ([bold]{renamed}[/bold] renamed)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(batch(load_value_repos(), args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
