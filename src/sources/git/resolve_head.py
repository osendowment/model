"""Resolve HEAD sha for repos with no commits in 2021-2025.

Some risk-scope repos are dormant — their last commit is older than the
window covered by `commits-years.csv` (default 2021-2025). Without a
sha they can't be analysed by sha-pinned fetchers (scc, lizard, semgrep).

This module fills those gaps by fetching the repo's latest default-branch
commit (sha + date) via the GitHub API and inserting it into
`commits-years.csv` under its **real commit year** (e.g. 2020) — so the
LOC/complexity snapshot is dated rather than an opaque "HEAD". A `year=HEAD`
alias row (same sha) is also written: the sha-pinned tarball fetchers
(lizard/cognitive/semgrep) special-case it, and `resolve_snapshot_sha` falls
back to it for repos older than its 10-year walk-back. Active repos are
untouched. There is always a snapshot sha when the repo has any commit.

Usage:
    uv run python -m src.sources.git.resolve_head           # all risk-scope repos with no last_sha
    uv run python -m src.sources.git.resolve_head --force   # re-resolve all
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os

import httpx
from rich.console import Console
from rich.progress import Progress

from src.sources.git.commits_years import (
    DEFAULT_YEARS, load_sha_data, write_sha_data, SHA_FILE,
)
from src.common.params import LAST_COMPLETE_YEAR
from src.common.repos import load_risk_repos

console = Console()
log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HEAD_YEAR = "HEAD"


def _tokens() -> list[str]:
    """Round-robin GitHub tokens — same convention as other fetchers."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    csv = os.environ.get("GITHUB_TOKENS", "").strip()
    if csv:
        return [t.strip() for t in csv.split(",") if t.strip()]
    one = os.environ.get("GITHUB_AUTH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not one:
        import subprocess
        try:
            one = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        except Exception:
            one = None
    return [one] if one else []


async def resolve_head_sha(
    client: httpx.AsyncClient, repo: str, token: str
) -> tuple[str, int] | None:
    """Return ``(sha, year)`` for the latest default-branch commit via the API.

    ``year`` is the committer year of that commit (e.g. 2020 for a repo whose
    last activity predates the 2021-2025 window), so the snapshot can be
    recorded under its real year instead of an opaque "HEAD". ``year`` is 0 if
    the date can't be parsed. Returns None if the repo is missing / private /
    API error.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    # /commits returns commits on the default branch by default. Cap at the end
    # of the last COMPLETE year (exclude the in-progress current year) so the
    # snapshot is reproducible and lands on a year scc/lizard already analysed —
    # not a fresh in-progress-year commit with no code metrics yet.
    until = f"{LAST_COMPLETE_YEAR + 1}-01-01T00:00:00Z"
    try:
        r = await client.get(f"{GITHUB_API}/repos/{repo}/commits",
                             params={"per_page": 1, "until": until},
                             headers=headers, timeout=20)
    except httpx.HTTPError as e:
        log.warning("HTTP error for %s: %s", repo, e)
        return None
    if r.status_code != 200:
        log.warning("HTTP %d for %s: %s", r.status_code, repo, r.text[:120])
        return None
    body = r.json()
    if not body:
        return None
    sha = body[0].get("sha")
    if not sha:
        return None
    commit = body[0].get("commit") or {}
    date = ((commit.get("committer") or {}).get("date")
            or (commit.get("author") or {}).get("date") or "")
    try:
        year = int(date[:4])
    except (ValueError, TypeError):
        year = 0
    return sha, year


async def main_async(args):
    eligible = {e.repo for e in load_risk_repos()}
    sha_data = load_sha_data(args.sha_file)

    # "Done" = the repo has a real sha in a settings window year (active repo).
    # Dormant repos have only fallback rows (dated pre-window and/or HEAD), so
    # they remain candidates and are (re)resolved into a dated snapshot capped at
    # the last complete year — cheap (~tens of repos) and idempotent in result
    # (the same pre-window commit resolves each run).
    window = {str(y) for y in DEFAULT_YEARS}
    have_window: set[str] = set()
    for (repo, year), row in sha_data.items():
        if year in window and (row.get("last_sha") or "").strip():
            have_window.add(repo)

    candidates = sorted(eligible if args.force else eligible - have_window)

    if not candidates:
        console.print("[green]All risk-scope repos already have a dated snapshot sha — nothing to resolve.[/green]")
        return

    tokens = _tokens()
    if not tokens:
        raise RuntimeError("No GitHub token. Set GITHUB_AUTH_TOKEN, GITHUB_TOKENS, or run gh auth login.")

    console.print(f"Resolving HEAD for [bold]{len(candidates)}[/bold] dormant repos...")

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(http2=True, follow_redirects=True) as client:
        async def go(idx: int, repo: str):
            async with sem:
                token = tokens[idx % len(tokens)]
                res = await resolve_head_sha(client, repo, token)
                if not res:
                    return repo, None, 0
                sha, year = res
                return repo, sha, year

        with Progress(console=console) as progress:
            task = progress.add_task("Resolving", total=len(candidates))
            done = await asyncio.gather(*[go(i, r) for i, r in enumerate(candidates)])
            for _ in done:
                progress.advance(task)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    resolved = 0; failed = 0
    for repo, sha, year in done:
        if sha:
            row = {"first_sha": sha, "last_sha": sha, "commits": "1", "fetched_at": now}
            # Record the snapshot under its REAL commit year (e.g. 2020) so the
            # LOC/complexity walk reads a dated fallback instead of an opaque
            # "HEAD". Keep the "HEAD" alias too: the sha-pinned tarball fetchers
            # (lizard/cognitive/semgrep) special-case it as the dormant snapshot.
            if year:
                sha_data[(repo, str(year))] = dict(row)
            sha_data[(repo, HEAD_YEAR)] = dict(row)
            resolved += 1
        else:
            failed += 1

    write_sha_data(args.sha_file, sha_data)
    console.print(f"[green]Resolved {resolved} HEAD shas → {args.sha_file}[/green]")
    if failed:
        console.print(f"[yellow]Failed for {failed} repos (likely private/deleted).[/yellow]")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sha-file", default=SHA_FILE)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--force", action="store_true",
                        help="Re-resolve HEAD even if already resolved")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
