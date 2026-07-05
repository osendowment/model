"""Per-year commit SHA anchor for GitLab repos (mirrors git/commits_years.py).

For each valid GitLab project in data/sources/gitlab/projects.csv, resolve the
last commit SHA of each calendar year on the default branch — the year→SHA map
the risk builders use to pin complexity/workload metrics. Writes a standalone
data/sources/gitlab/commits-years.csv keyed on the unified repo_id.

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
PROJECTS_IN = REPO / "data" / "sources" / "gitlab" / "projects.csv"
COMMITS_OUT = REPO / "data" / "sources" / "gitlab" / "commits-years.csv"

YEARS = list(range(2021, dt.datetime.now(dt.UTC).year + 1))
MAX_CONCURRENT = 8

COMMITS_FIELDS = ["repo_id", "git_url", "project", "year", "first_sha",
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
        commits = int(total) if total and total.isdigit() else (len(data) if isinstance(data, list) else 0)
        last_sha = data[0]["id"] if isinstance(data, list) and data else ""
        return last_sha, commits


def _load_valid_projects() -> list[dict]:
    if not PROJECTS_IN.exists():
        return []
    out = []
    with open(PROJECTS_IN, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("valid") or "").lower() == "true" and r.get("repo_id"):
                host = r["host"]
                path = r["path_with_namespace"] or r["project"].split("/", 1)[1]
                out.append({"repo_id": r["repo_id"], "project": r["project"],
                            "git_url": clone_url(host, path),
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


def _load_existing_ids() -> set[str]:
    if not COMMITS_OUT.exists():
        return set()
    with open(COMMITS_OUT, encoding="utf-8") as f:
        return {r["repo_id"] for r in csv.DictReader(f) if r.get("repo_id")}


async def _fetch_all(projects: list[dict]) -> list[dict]:
    limiter = GitLabLimiter()
    rows: list[dict] = []
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with aiohttp.ClientSession() as session:
        async def _one(proj):
            async with sem:
                for year in YEARS:
                    sha, commits = await _fetch_year(
                        limiter, session, proj["host"], proj["path"], proj["branch"], year)
                    rows.append({"repo_id": proj["repo_id"], "git_url": proj["git_url"],
                                 "project": proj["project"], "year": year,
                                 "first_sha": "", "last_sha": sha,
                                 "commits": commits, "fetched_at": _now_iso()})
        await asyncio.gather(*[_one(p) for p in projects])
    return rows


def fetch_and_persist(limit: int | None = None, force: bool = False,
                      quiet: bool = False) -> dict:
    projects = _load_valid_projects()
    if not force:
        done = _load_existing_ids()
        projects = [p for p in projects if p["repo_id"] not in done]
    if limit:
        projects = projects[:limit]
    if not quiet:
        console.rule("[bold cyan]gitlab/commits_years")
        console.print(f"  projects to anchor: [bold]{len(projects)}[/bold]")
    t0 = time.monotonic()
    new_rows = asyncio.run(_fetch_all(projects)) if projects else []
    # Merge with existing (keep prior repo_ids), keyed on (repo_id, year).
    merged: dict[tuple[str, str], dict] = {}
    if COMMITS_OUT.exists() and not force:
        with open(COMMITS_OUT, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                merged[(r["repo_id"], r["year"])] = r
    for r in new_rows:
        merged[(r["repo_id"], str(r["year"]))] = r
    ordered = sorted(merged.values(), key=lambda r: (r["repo_id"], int(r["year"])))
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
