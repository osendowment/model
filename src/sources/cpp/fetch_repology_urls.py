#!/usr/bin/env python3
"""Fetch upstream URL candidates from Repology for cpp projects missing git.

Repology's per-project HTML page (`https://repology.org/project/<name>/information`)
aggregates VCS / homepage / issue-tracker links across every distro that
packages the project. The JSON API doesn't carry these URLs -- they only
appear in the rendered HTML.

For each cpp project that doesn't already have a `git` URL in
`data/sources/cpp/results.csv`, this script fetches the information HTML, pulls
every `href="..."` out of it, and runs them through the classifier in
`src.build_git`. Only URLs recognised as Git endpoints are kept.

The HTML is parsed in memory and discarded — never persisted. Instead the
parsed result is cached in a CSV so re-runs skip already-checked projects:

    `data/sources/repology/project-urls.csv`
    columns: project, candidate_url, platform, status, fetched_at

A project that yields git URLs gets one `status=ok` row per URL. A project
that was checked but yielded nothing gets a single sentinel row (blank URL)
with `status=none` (HTTP 200, no upstream git link) or `not_found` (HTTP 4xx)
— this is what lets the cache skip them next run. `status=error` rows (network
/ timeout) are recorded for audit but always retried, so a transient failure
is never mistaken for "checked, nothing there".

The skip lasts for `TTL_DAYS` (declared in settings.json — upstream VCS links
move rarely, so the window is long). A settled project is re-fetched once its
newest `fetched_at` passes the TTL; a project missing from the cache is always
fetched. Rows written before the `fetched_at` column existed carry no date and
keep their original cache-forever treatment. `--refresh` ignores the TTL.

Usage:
    uv run -m src.sources.cpp.fetch_repology_urls
    uv run -m src.sources.cpp.fetch_repology_urls --classes A,B   # only A/B-class cpp projects
    uv run -m src.sources.cpp.fetch_repology_urls --limit 20      # fetch first 20 uncached (testing)
    uv run -m src.sources.cpp.fetch_repology_urls --refresh       # re-fetch even cached projects
"""

import argparse
import asyncio
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.common.freshness import row_is_fresh
from src.common.params import fetch_ttl_days
from src.value.build_git_urls import classify

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
RESULTS = DATA_DIR / "sources" / "cpp" / "results.csv"
CACHE = DATA_DIR / "sources" / "repology" / "project-urls.csv"
FIELDNAMES = ["project", "candidate_url", "platform", "status", "fetched_at"]
# A project with any of these statuses has been definitively checked — re-runs
# skip it. 'error' is intentionally absent: transient failures must be retried.
DONE_STATUSES = frozenset({"ok", "none", "not_found"})
# How long that skip lasts. Cache TTL in days for project-urls.csv, from
# settings.json — the one place every fetcher's TTL is declared.
TTL_DAYS = fetch_ttl_days("sources/cpp/fetch_repology_urls")

INFO_URL = "https://repology.org/project/{name}/information"
HREF_RE = re.compile(r'href="([^"]+)"')
# Pull only the "Repository links" / "Homepage links" / "Downloads" sections.
# Skip "All package recipes" (PKGBUILDs that point at random GitHub repos)
# and other distro-specific noise.
SECTION_RE = re.compile(
    r'<section id="(?:Repository_links|Homepage_links|Downloads)">'
    r'(.*?)</section>',
    re.DOTALL,
)
HEADERS = {"User-Agent": "ose-model fetch_repology_urls.py (https://endowment.dev)"}

CONCURRENCY = 4   # Be polite to repology.org
TIMEOUT = 30


def load_cpp_targets(classes: tuple[str, ...]) -> list[str]:
    """Cpp projects from results.csv that don't yet have a git URL."""
    out: list[str] = []
    with open(RESULTS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("git") or "").strip():
                continue
            if r.get("value_class") not in classes:
                continue
            # Skip pseudo-packages — their names won't match Repology projects.
            if r["package"].startswith(("debian:", "homebrew:")):
                continue
            out.append(r["package"])
    return out


def load_cache(path: Path) -> tuple[dict[str, list[dict]], set[str]]:
    """Load the URL cache → (rows_by_project, done_projects).

    `done_projects` are those already definitively checked (`status` in
    DONE_STATUSES); re-runs skip them. Rows with `status=error`, or a legacy
    file with no `status` column, are handled gracefully — legacy rows are all
    real URLs so they count as `ok`, while errors are left out of `done` so
    they get retried.
    """
    rows_by_project: dict[str, list[dict]] = {}
    done: set[str] = set()
    if not path.exists():
        return rows_by_project, done
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            project = r["project"]
            status = (r.get("status") or "ok").strip()  # legacy rows → ok
            rows_by_project.setdefault(project, []).append({
                "project": project,
                "candidate_url": r.get("candidate_url", ""),
                "platform": r.get("platform", ""),
                "status": status,
                "fetched_at": r.get("fetched_at", ""),
            })
            if status in DONE_STATUSES:
                done.add(project)
    return rows_by_project, done


def cache_is_fresh(rows: list[dict]) -> bool:
    """True if a settled project's cached rows are still inside the TTL.

    One dated row inside the window keeps the whole project fresh. Rows with no
    `fetched_at` (written before the column existed) are undatable rather than
    stale, so they keep the cache-forever treatment they have always had — only
    a row that records WHEN it was fetched can age out.
    """
    dated = [r for r in rows if (r.get("fetched_at") or "").strip()]
    if not dated:
        return True
    return any(row_is_fresh(r, TTL_DAYS) for r in dated)


async def fetch_one(session: aiohttp.ClientSession, project: str) -> tuple[str, str]:
    """Fetch `project`'s Repology information page. Returns (html, fetch_status).

    fetch_status is 'ok' (HTTP 200, html returned), 'not_found' (any non-200),
    or 'error' (network failure / timeout). The HTML is never persisted.
    """
    url = INFO_URL.format(name=project)
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                return "", "not_found"
            return await r.text(), "ok"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "", "error"


def extract_git_urls(html: str) -> list[tuple[str, str]]:
    """Extract git URLs from the upstream sections only.

    Restricts to `Repository_links`, `Homepage_links`, `Downloads` -- skipping
    `All_package_recipes` which pollutes with distro PKGBUILD links.
    """
    if not html:
        return []
    sections = SECTION_RE.findall(html)
    if not sections:
        return []
    combined = "\n".join(sections)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for href in HREF_RE.findall(combined):
        platform, canon = classify(href)
        # Repology pages occasionally carry a truncated host with no TLD, e.g.
        # "https://gitlab.gnome/gnome/libgweather.git". Such a host is never
        # real, and it would demand its own GitLab nickname — one that would
        # collide with the valid host it was truncated from. Drop it; the
        # well-formed URL is always present alongside.
        host = re.sub(r"^\w+://", "", canon).split("/", 1)[0]
        if host and "." not in host.rstrip("."):
            continue
        if platform and canon not in seen:
            seen.add(canon)
            out.append((canon, platform))
    return out


def rows_for(project: str, git_urls: list[tuple[str, str]], fetch_status: str, date: str) -> list[dict]:
    """Build the cache rows for one fetched project.

    One `ok` row per git URL, or a single sentinel row recording why nothing
    was found (`none` = page had no git link, else the fetch_status).
    """
    if git_urls:
        return [{"project": project, "candidate_url": url, "platform": platform,
                 "status": "ok", "fetched_at": date} for url, platform in git_urls]
    status = "none" if fetch_status == "ok" else fetch_status
    return [{"project": project, "candidate_url": "", "platform": "",
             "status": status, "fetched_at": date}]


async def main_async(args: argparse.Namespace) -> None:
    classes = tuple(c.strip().upper() for c in args.classes.split(",") if c.strip())
    date = datetime.now(timezone.utc).date().isoformat()

    rows_by_project, done = load_cache(CACHE)
    targets = load_cpp_targets(classes)
    if args.limit:
        targets = targets[: args.limit]
    # Fetch a target when it has never been settled, or when what we cached for
    # it has aged past the TTL. `--refresh` fetches every target regardless.
    to_fetch = list(targets) if args.refresh else [
        t for t in targets
        if t not in done or not cache_is_fresh(rows_by_project[t])
    ]
    fetch_set = set(to_fetch)

    console.rule("[bold white]fetch_repology_urls.py[/bold white]")
    console.print(f"  Targets   : [cyan]{len(targets):,}[/cyan] cpp projects "
                  f"(classes={','.join(classes)}) without git URL")
    console.print(f"  Cached    : [cyan]{len(targets) - len(to_fetch):,}[/cyan] skipped "
                  f"(checked, still fresh){' — ignored (--refresh)' if args.refresh else ''}")
    console.print(f"  Fetching  : [cyan]{len(to_fetch):,}[/cyan] this run")
    console.print(f"  Cache     : [cyan]{CACHE}[/cyan]  TTL=[cyan]{TTL_DAYS}d[/cyan]")
    console.print()

    sem = asyncio.Semaphore(CONCURRENCY)
    new_rows: list[dict] = []
    found_any_git = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=24),
        MofNCompleteColumn(),
        "•",
        TimeElapsedColumn(),
        console=console,
    )

    async with aiohttp.ClientSession() as session:
        with progress:
            task_id = progress.add_task("starting…", total=len(to_fetch))

            async def process(project: str) -> None:
                nonlocal found_any_git
                async with sem:
                    progress.update(task_id, description=f"[cyan]{project[:36]}[/cyan]")
                    html, fetch_status = await fetch_one(session, project)
                    git_urls = extract_git_urls(html)
                    new_rows.extend(rows_for(project, git_urls, fetch_status, date))
                    if git_urls:
                        found_any_git += 1
                        best = git_urls[0]
                        progress.console.print(
                            f"  [green]{project:<32}[/green] → "
                            f"[{best[1]:>9}] {best[0][:60]}"
                        )
                    progress.advance(task_id)

            await asyncio.gather(*(process(p) for p in to_fetch))

    # Carry forward every cached row we did not just re-fetch, then add the new
    # ones. Sort by (project, url) so the committed cache has stable diffs.
    keep_rows = [row for p, rows in rows_by_project.items() if p not in fetch_set for row in rows]
    final_rows = keep_rows + new_rows
    final_rows.sort(key=lambda r: (r["project"], r["candidate_url"]))

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(final_rows)

    n_url_rows = sum(1 for r in final_rows if r["candidate_url"])
    n_projects_with_git = len({r["project"] for r in final_rows if r["status"] == "ok"})
    console.print()
    console.print(f"[green]✓[/green] fetched {len(to_fetch):,} this run, "
                  f"{found_any_git:,} yielded a git URL")
    console.print(f"[green]✓[/green] cache now: {n_projects_with_git:,} projects with git "
                  f"({n_url_rows:,} candidate URLs) → {CACHE}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--classes", default="A,B,C",
                   help="Comma-separated value_class filter (default A,B,C — D excluded)")
    p.add_argument("--limit", type=int, default=0,
                   help="Only process the first N targets (0 = all). For testing.")
    p.add_argument("--refresh", action="store_true",
                   help=f"Re-fetch every target, ignoring the {TTL_DAYS}-day cache TTL")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
