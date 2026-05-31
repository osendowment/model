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

Pages are parsed in memory and discarded — the raw HTML is never persisted.
Output: `data/sources/repology/project-urls.csv` with columns
`project, candidate_url, platform`.

Usage:
    uv run -m src.sources.cpp.fetch_repology_urls
    uv run -m src.sources.cpp.fetch_repology_urls --classes A,B   # only A/B-class cpp projects
"""

import argparse
import asyncio
import csv
import re
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

from src.value.build_git_urls import classify

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
RESULTS = DATA_DIR / "sources" / "cpp" / "results.csv"
OUTPUT = DATA_DIR / "sources" / "repology" / "project-urls.csv"

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


async def fetch_one(session: aiohttp.ClientSession, project: str) -> str:
    """Fetch `project`'s Repology information page HTML. '' on 404/error.

    The HTML is parsed in memory and discarded — only the extracted git URLs
    (`project-urls.csv`) are stored, never the raw pages.
    """
    url = INFO_URL.format(name=project)
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                return ""
            return await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return ""


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
        if platform and canon not in seen:
            seen.add(canon)
            out.append((canon, platform))
    return out


async def main_async(args: argparse.Namespace) -> None:
    classes = tuple(c.strip().upper() for c in args.classes.split(",") if c.strip())
    targets = load_cpp_targets(classes)
    console.rule("[bold white]fetch_repology_urls.py[/bold white]")
    console.print(f"  Targets   : [cyan]{len(targets):,}[/cyan] cpp projects "
                  f"(classes={','.join(classes)}) without git URL")
    console.print(f"  Output    : [cyan]{OUTPUT}[/cyan]")
    console.print()

    sem = asyncio.Semaphore(CONCURRENCY)
    rows: list[dict] = []
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
            task_id = progress.add_task("starting…", total=len(targets))

            async def process(project: str) -> None:
                nonlocal found_any_git
                async with sem:
                    progress.update(task_id, description=f"[cyan]{project[:36]}[/cyan]")
                    html = await fetch_one(session, project)
                    git_urls = extract_git_urls(html)
                    if git_urls:
                        found_any_git += 1
                    for url, platform in git_urls:
                        rows.append({
                            "project": project,
                            "candidate_url": url,
                            "platform": platform,
                        })
                    if git_urls:
                        best = git_urls[0]
                        progress.console.print(
                            f"  [green]{project:<32}[/green] → "
                            f"[{best[1]:>9}] {best[0][:60]}"
                        )
                    progress.advance(task_id)

            await asyncio.gather(*(process(p) for p in targets))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["project", "candidate_url", "platform"],
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)

    console.print()
    console.print(f"[green]✓[/green] {found_any_git:,}/{len(targets):,} projects yielded a git URL")
    console.print(f"[green]✓[/green] {len(rows):,} candidate URLs → {OUTPUT}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--classes", default="A,B,C",
                   help="Comma-separated value_class filter (default A,B,C — D excluded)")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
