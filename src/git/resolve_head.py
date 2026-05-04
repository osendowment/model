"""Resolve HEAD sha for repos with no commits in 2021-2025.

Some eligible repos are dormant — their last commit is older than the
window covered by `commits-years.csv` (default 2021-2025). Without a
sha they can't be analysed by sha-pinned fetchers (scc, lizard, semgrep).

This module fills those gaps by fetching the repo's default-branch HEAD
sha via the GitHub API and inserting a `year=HEAD` row into
`commits-years.csv`. `resolve_snapshot_sha` walks backward from the
requested year and will pick up the HEAD row last (when nothing else
matches) — making dormant repos analysable while leaving active repos
untouched.

Usage:
    uv run python -m src.git.resolve_head           # all eligible repos with no last_sha
    uv run python -m src.git.resolve_head --force   # re-resolve all
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import os
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import Progress

from src.git.commits_years import load_sha_data, write_sha_data, SHA_FILE
from src.pipeline.repos import load_eligible_repos

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
) -> str | None:
    """Return the latest commit sha on the default branch via GitHub API.

    Returns None if the repo is missing / private / API error.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    # /commits returns commits on the default branch by default
    try:
        r = await client.get(f"{GITHUB_API}/repos/{repo}/commits",
                             params={"per_page": 1}, headers=headers, timeout=20)
    except httpx.HTTPError as e:
        log.warning("HTTP error for %s: %s", repo, e)
        return None
    if r.status_code != 200:
        log.warning("HTTP %d for %s: %s", r.status_code, repo, r.text[:120])
        return None
    body = r.json()
    if not body:
        return None
    return body[0].get("sha")


async def main_async(args):
    eligible = {e.repo for e in load_eligible_repos()}
    sha_data = load_sha_data(args.sha_file)

    # Repos with NO last_sha at any year
    have_sha: set[str] = set()
    for (repo, year), row in sha_data.items():
        if (row.get("last_sha") or "").strip():
            have_sha.add(repo)

    candidates = sorted(eligible - have_sha)
    if not args.force:
        # Skip repos already resolved (have a HEAD row)
        resolved = {repo for (repo, year) in sha_data.keys() if year == HEAD_YEAR
                    and (sha_data[(repo, year)].get("last_sha") or "").strip()}
        candidates = [c for c in candidates if c not in resolved]

    if not candidates:
        console.print("[green]All eligible repos already have a sha — nothing to resolve.[/green]")
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
                sha = await resolve_head_sha(client, repo, token)
                if not sha:
                    return repo, None
                return repo, sha

        with Progress(console=console) as progress:
            task = progress.add_task("Resolving", total=len(candidates))
            done = await asyncio.gather(*[go(i, r) for i, r in enumerate(candidates)])
            for _ in done:
                progress.advance(task)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    resolved = 0; failed = 0
    for repo, sha in done:
        if sha:
            sha_data[(repo, HEAD_YEAR)] = {
                "first_sha": sha,
                "last_sha": sha,
                "commits": "1",
                "fetched_at": now,
            }
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
