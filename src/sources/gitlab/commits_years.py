"""Per-year commit SHA anchor for GitLab repos (mirrors git/commits_years.py).

For each risk-scope GitLab project, resolve the last commit SHA of each calendar
year on the default branch — the year→SHA map the risk builders use to pin
complexity/workload metrics. Rows land in the **shared**
``data/sources/git/commits-years.csv`` beside the GitHub rows (same unified
schema: repo, repo_id, git_url, year, first_sha, last_sha, commits, fetched_at)
so the sha-pinned code-metrics fetcher (`fetch_sha_metrics`) reads one file for
both hosts. Scope is the GitLab members of `load_top_repos()` (risk scope), not
every valid project, and each row's `repo` is that entry's canonical slug so it
joins downstream exactly like the value.csv / builder key.

Rows are upserted by (repo, repo_id, year): every existing GitHub row is
preserved verbatim (a GitHub mirror that shares a slug with a GitLab repo keeps
its own gh/… row — the two are distinct by repo_id), and re-running only
rewrites the GitLab (repo_id, year) cells it fetched.

Usage:
    uv run python -m src.sources.gitlab.commits_years --limit 3
    uv run python -m src.sources.gitlab.commits_years --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
import time
from pathlib import Path

import aiohttp
from rich.console import Console

from src.sources.gitlab.gitlab_client import GitLabLimiter, api_base, clone_url, encode_project_path

log = logging.getLogger(__name__)
console = Console()

REPO = Path(__file__).resolve().parents[3]
PROJECTS_IN = REPO / "data" / "sources" / "gitlab" / "repos.csv"
# Shared with GitHub — the single file fetch_sha_metrics / build_complexity read.
COMMITS_OUT = REPO / "data" / "sources" / "git" / "commits-years.csv"

YEARS = list(range(2021, dt.datetime.now(dt.UTC).year + 1))
MAX_CONCURRENT = 8

# Unified schema, byte-compatible with src/sources/git/commits_years.py's
# SHA_FIELDS so GitHub and GitLab rows coexist in one file.
COMMITS_FIELDS = ["repo", "repo_id", "git_url", "year", "first_sha",
                  "last_sha", "commits", "fetched_at"]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _fetch_year(limiter, session, host: str, path: str, branch: str,
                      year: int) -> tuple[str, int]:
    """Return (last_sha, commits) for `year` on `branch`. Blank/0 if no commits.

    One call: commits in [year-01-01, year-12-31], newest-first, per_page=1.
    Item [0] is the year's last commit; the `X-Total` header is the count.
    """
    since = f"{year}-01-01T00:00:00Z"
    until = f"{year}-12-31T23:59:59Z"
    ref = branch or "HEAD"
    url = (f"{api_base(host)}/projects/{encode_project_path(path)}/repository/commits"
           f"?ref_name={ref}&since={since}&until={until}&per_page=1")
    try:
        resp = await limiter.get(session, host, url)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "", 0
    async with resp:
        if resp.status != 200:
            return "", 0
        data = await resp.json()
        total = resp.headers.get("X-Total")
        # GitLab omits X-Total on result sets >10,000 commits — in that rare
        # case we fall back to len(data) (== 1, since per_page=1), which
        # under-reports the true commit count for that year. `last_sha` (the
        # anchor SHAs downstream fetchers pin to) is unaffected either way.
        commits = int(total) if total and total.isdigit() else (len(data) if isinstance(data, list) else 0)
        last_sha = data[0]["id"] if isinstance(data, list) and data else ""
        return last_sha, commits


def _load_scope() -> dict[str, dict]:
    """{gl/ repo_id: {"repo": canonical slug, "git_url": clone URL}} for the
    GitLab members of the risk scope (`load_top_repos`).

    Keying on `load_top_repos` (not every valid project) means we only anchor
    the ~26 GitLab repos the risk pipeline actually scores, and the `repo` we
    write is the exact slug the builders / sha-metrics join on.
    """
    from src.common.repos import load_top_repos
    out: dict[str, dict] = {}
    for e in load_top_repos():
        rid = str(e.repo_id)
        if rid.startswith("gl/"):
            out[rid] = {"repo": e.repo, "git_url": e.git_url}
    return out


def _load_valid_projects() -> list[dict]:
    """Risk-scope GitLab projects joined to their repos.csv host/path/branch.

    Only projects whose gl/ repo_id is in the risk scope are returned; each row
    carries the canonical `repo` slug + `git_url` from value.csv (via the scope
    map) plus the `host`/`path`/`branch` needed to hit the per-instance API.
    """
    if not PROJECTS_IN.exists():
        return []
    scope = _load_scope()
    out = []
    with open(PROJECTS_IN, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rid = (r.get("repo_id") or "").strip()
            if rid not in scope:
                continue
            host = r["host"]
            path = r["path_with_namespace"] or r["project"].split("/", 1)[1]
            out.append({"repo_id": rid, "repo": scope[rid]["repo"],
                        "git_url": scope[rid]["git_url"] or clone_url(host, path),
                        "host": host, "path": path,
                        "branch": r.get("default_branch") or "HEAD"})
    return out


def _atomic_write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMMITS_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _load_existing_pairs() -> set[tuple[str, str]]:
    """{(repo_id, year_str)} already anchored in COMMITS_OUT."""
    if not COMMITS_OUT.exists():
        return set()
    with open(COMMITS_OUT, encoding="utf-8") as f:
        return {(r["repo_id"], r["year"]) for r in csv.DictReader(f) if r.get("repo_id")}


def _pairs_to_fetch(projects: list[dict], existing_pairs: set[tuple[str, str]],
                    force: bool) -> list[tuple[dict, int]]:
    """(project, year) pairs still needing a fetch.

    A pair is skipped only if (repo_id, str(year)) already exists — so a
    newly-added YEAR (e.g. a new calendar year rolling in) is still picked
    up for repos that were already anchored in prior years, instead of the
    whole repo being permanently skipped once it has any row at all.
    Mirrors src/sources/git/commits_years.py's per-(repo, year) worklist.
    """
    out = []
    for proj in projects:
        for year in YEARS:
            if force or (proj["repo_id"], str(year)) not in existing_pairs:
                out.append((proj, year))
    return out


async def _fetch_all(pairs: list[tuple[dict, int]]) -> list[dict]:
    limiter = GitLabLimiter()
    rows: list[dict] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with aiohttp.ClientSession() as session:
        async def _one(proj: dict, year: int):
            async with sem:
                sha, commits = await _fetch_year(
                    limiter, session, proj["host"], proj["path"], proj["branch"], year)
                rows.append({"repo": proj["repo"], "repo_id": proj["repo_id"],
                             "git_url": proj["git_url"], "year": year,
                             "first_sha": "", "last_sha": sha,
                             "commits": commits, "fetched_at": _now_iso()})
        await asyncio.gather(*[_one(p, y) for p, y in pairs])
    return rows


def fetch_and_persist(limit: int | None = None, force: bool = False,
                      quiet: bool = False) -> dict:
    projects = _load_valid_projects()
    if limit:
        projects = projects[:limit]
    existing_pairs: set[tuple[str, str]] = set() if force else _load_existing_pairs()
    pairs = _pairs_to_fetch(projects, existing_pairs, force)
    if not quiet:
        console.rule("[bold cyan]gitlab/commits_years")
        console.print(f"  projects: [bold]{len(projects)}[/bold] · "
                      f"pairs to fetch: [bold]{len(pairs)}[/bold]")
    t0 = time.monotonic()
    new_rows = asyncio.run(_fetch_all(pairs)) if pairs else []
    # Merge into the shared file keyed on (repo, repo_id, year). We ALWAYS read
    # existing rows (even with --force) so every GitHub row — and any GitLab row
    # we didn't refetch — is preserved verbatim; --force only re-fetches GitLab
    # (repo_id, year) cells. The three-part key keeps a GitHub mirror and a
    # GitLab repo that happen to share a slug (e.g. gnome/glib) as distinct rows.
    merged: dict[tuple[str, str, str], dict] = {}
    if COMMITS_OUT.exists():
        with open(COMMITS_OUT, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                key = (r.get("repo", ""), r.get("repo_id", ""), r.get("year", ""))
                merged[key] = {k: r.get(k, "") for k in COMMITS_FIELDS}
    for r in new_rows:
        merged[(r["repo"], r["repo_id"], str(r["year"]))] = r

    def _year_key(v: str) -> int:
        # `year` can be the "HEAD" pseudo-row that resolve_head writes; sort it
        # last within a repo rather than crash int("HEAD").
        s = str(v)
        return int(s) if s.lstrip("-").isdigit() else 10**9
    ordered = sorted(
        merged.values(),
        key=lambda r: (r.get("repo", ""), _year_key(r.get("year", "")), r.get("repo_id", "")),
    )
    _atomic_write(COMMITS_OUT, ordered)
    elapsed = time.monotonic() - t0
    if not quiet:
        console.print(f"  wrote {len(ordered)} rows · [dim]{elapsed:.1f}s[/dim]")
    return {"projects": len(projects), "rows": len(ordered), "elapsed_s": elapsed}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    fetch_and_persist(limit=args.limit, force=args.force)


if __name__ == "__main__":
    main()
