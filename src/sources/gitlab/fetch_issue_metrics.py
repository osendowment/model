"""Fetch issue counts (opened, closed) per GitLab repo per year.

GitLab-side counterpart of `src.sources.github.fetch_issue_metrics` — fills
the workload dimension's issue inputs (`nni_per_ac`, `issue_close_ratio`,
trend columns) for GitLab-hosted top repos, which the GitHub Search fetcher
cannot cover.

Method: the GitLab issues API has no `closed_at` filter, so per-year counts
cannot be asked for directly (unlike GitHub's `closed:Y..Y` qualifier).
Instead ONE exact scan per project enumerates
`/projects/:id/issues?scope=all&updated_after={first_year}-01-01` and buckets
each issue client-side: `created_at` year → opened_issues, `closed_at` year →
closed_issues. The `updated_after` bound is safe: an issue created OR closed
inside the window necessarily has `updated_at` inside it (both events bump
`updated_at`), so nothing in-window is missed while pre-window noise is
skipped. Counts are exact (enumerated), so a 0 is a measured zero.

Scope: top repos (valid class-A, archived included) with a `gl/…` repo_id,
resolved to (host, project_id) via data/sources/gitlab/repos.csv. A repo is
scanned when any of its (repo, year, metric) cells is missing, or when a cell's
`fetched_at` is older than the cache TTL (`TTL_DAYS`, from settings.json
`fetch_ttl_days`); --force rescans every repo. A cell with a blank `fetched_at`
is kept as-is — an unknown date is not an expired one.

Output (long format, same schema/contract as the GitHub fetcher):
    data/sources/gitlab/issues.csv — repo, repo_id, year, metric, value, fetched_at
    metric ∈ {opened_issues, closed_issues}; smart upsert keyed on
    (repo, year, metric); a failed scan writes NOTHING (absent ≠ 0).

Usage:
    uv run python -m src.sources.gitlab.fetch_issue_metrics
    uv run python -m src.sources.gitlab.fetch_issue_metrics --limit 3
    uv run python -m src.sources.gitlab.fetch_issue_metrics --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from rich.console import Console

from src.common.freshness import row_is_fresh
from src.common.params import YEARS, fetch_ttl_days
from src.common.repos import load_top_repos
from src.sources.gitlab.gitlab_client import GitLabLimiter, api_base, load_token_map

console = Console()

ROOT = Path(__file__).resolve().parent.parent.parent.parent
GITLAB_REPOS = ROOT / "data" / "sources" / "gitlab" / "repos.csv"
OUTPUT_FILE = ROOT / "data" / "sources" / "gitlab" / "issues.csv"

FIELDS = ["repo", "repo_id", "year", "metric", "value", "fetched_at"]
METRICS = ("opened_issues", "closed_issues")
PER_PAGE = 100
MAX_PAGES = 1000  # 100k issues — runaway guard, logged loudly if ever hit

# Cache TTL for issues.csv: how old a cell's `fetched_at` may get before its
# repo is rescanned. Issue counts for a CLOSED year never change, so the window
# is long and a warm re-run stays zero-network; the value lives in settings.json
# so every fetcher's policy is reviewable in one place.
TTL_DAYS = fetch_ttl_days("sources/gitlab/fetch_issue_metrics")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cell_is_stale(row: dict) -> bool:
    """True when an existing issues.csv cell is past TTL_DAYS.

    A BLANK `fetched_at` is never stale: "date unknown" is not "expired", and
    reading it as expired would rescan rows the presence-only gate kept.
    """
    if not (row.get("fetched_at") or "").strip():
        return False
    return not row_is_fresh(row, TTL_DAYS)


def load_gitlab_targets() -> list[dict]:
    """Top repos with a gl/ id, joined to (host, project_id) via gitlab/repos.csv."""
    by_repo_id: dict[str, dict] = {}
    if GITLAB_REPOS.exists():
        with open(GITLAB_REPOS, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rid = (r.get("repo_id") or "").strip()
                if rid and (r.get("project_id") or "").strip():
                    by_repo_id[rid] = r
    targets = []
    for e in load_top_repos(skip_archived=False):
        if not e.repo_id.startswith("gl/"):
            continue
        row = by_repo_id.get(e.repo_id)
        if row is None:
            console.print(f"[yellow]{e.repo} ({e.repo_id}): no gitlab/repos.csv row — "
                          f"run gitlab-projects first[/yellow]")
            continue
        targets.append({"repo": e.repo, "repo_id": e.repo_id,
                        "host": row["host"], "project_id": row["project_id"]})
    return targets


def load_existing() -> dict[tuple[str, int, str], dict]:
    if not OUTPUT_FILE.exists():
        return {}
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        return {(r["repo"], int(r["year"]), r["metric"]): r
                for r in csv.DictReader(f)}


def write_output(cells: dict[tuple[str, int, str], dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(r for _, r in sorted(cells.items()))
    os.replace(tmp, OUTPUT_FILE)


async def scan_project(limiter: GitLabLimiter, session: aiohttp.ClientSession,
                       target: dict) -> dict[str, dict[int, int]] | None:
    """Enumerate the project's issues updated since the window start and
    bucket by created/closed year. Returns {metric: {year: count}} with every
    window year present (measured zeros), or None on failure (nothing cached)."""
    host, pid = target["host"], target["project_id"]
    first = min(YEARS)
    opened = {y: 0 for y in YEARS}
    closed = {y: 0 for y in YEARS}
    url = (f"{api_base(host)}/projects/{pid}/issues"
           f"?scope=all&updated_after={first}-01-01T00:00:00Z"
           f"&per_page={PER_PAGE}&order_by=created_at&sort=asc")
    page = 1
    while page <= MAX_PAGES:
        try:
            resp = await limiter.get(session, host, f"{url}&page={page}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            console.print(f"[red]{target['repo']}: network error page {page}: {e}[/red]")
            return None
        async with resp:
            if resp.status != 200:
                console.print(f"[red]{target['repo']}: http {resp.status} page {page}[/red]")
                return None
            items = await resp.json()
        if not items:
            break
        for it in items:
            created = (it.get("created_at") or "")[:4]
            if created.isdigit() and int(created) in opened:
                opened[int(created)] += 1
            closed_at = (it.get("closed_at") or "")[:4]
            if closed_at.isdigit() and int(closed_at) in closed:
                closed[int(closed_at)] += 1
        if len(items) < PER_PAGE:
            break
        page += 1
    else:
        console.print(f"[red]{target['repo']}: exceeded {MAX_PAGES} pages — refusing "
                      f"to write partial counts[/red]")
        return None
    return {"opened_issues": opened, "closed_issues": closed}


async def run(targets: list[dict]) -> tuple[int, int]:
    cells = load_existing()
    token_map = load_token_map()
    limiter = GitLabLimiter(token_map)
    scanned = failed = 0
    async with aiohttp.ClientSession() as session:
        for t in targets:
            res = await scan_project(limiter, session, t)
            if res is None:
                failed += 1
                continue
            now = _now_iso()
            for metric, by_year in res.items():
                for year, count in by_year.items():
                    cells[(t["repo"], year, metric)] = {
                        "repo": t["repo"], "repo_id": t["repo_id"], "year": year,
                        "metric": metric, "value": count, "fetched_at": now,
                    }
            scanned += 1
            write_output(cells)  # flush per repo — a kill keeps completed scans
            console.print(f"  [green]{t['repo']}[/green] "
                          + "  ".join(f"{y}: +{res['opened_issues'][y]}/-{res['closed_issues'][y]}"
                                      for y in YEARS))
    return scanned, failed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help=f"Ignore the {TTL_DAYS}-day cache TTL: re-scan repos even "
                        f"if all their cells are present and fresh")
    args = p.parse_args()

    console.rule("[bold]gitlab/fetch_issue_metrics")
    targets = load_gitlab_targets()
    if not args.force:
        existing = load_existing()
        def complete(t: dict) -> bool:
            """Every window cell present AND still inside the TTL."""
            cells = [existing.get((t["repo"], y, m)) for y in YEARS for m in METRICS]
            return all(c is not None and not cell_is_stale(c) for c in cells)
        skipped = [t for t in targets if complete(t)]
        targets = [t for t in targets if not complete(t)]
        if skipped:
            console.print(f"  [dim]{len(skipped)} repo(s) already complete and fresh "
                          f"(TTL={TTL_DAYS}d) — scanning {len(targets)}; "
                          f"--force to rescan[/dim]")
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        console.print("[green]All GitLab issue cells present — nothing to do.[/green]")
        return

    scanned, failed = asyncio.run(run(targets))
    console.print(f"\n[green]scanned {scanned}[/green], [red]{failed} failed[/red] "
                  f"→ {OUTPUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
