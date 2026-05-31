#!/usr/bin/env python3
"""Fetch project URLs from PyPI JSON API for every package in results.csv.

The legacy `data/sources/pypi/raw/package-github-mapping.csv` was a one-shot BigQuery
extract pre-filtered to `github.com` URLs at SQL time. Anything hosted
elsewhere (GitLab, Heptapod, Codeberg, Launchpad, sourceforge) was lost.

This script queries `pypi.org/pypi/<name>/json` for every package and writes
the full set of URLs (`info.project_urls` + `info.home_page`) to
`data/sources/pypi/raw/package-urls.csv` -- to be classified by `build_git.py` by
host (github / gitlab / bitbucket / sourcehut / codeberg / custom).

Per-package responses are cached to `data/sources/pypi/raw/api-cache/<name>.json`.

Usage:
    uv run -m src.pypi.fetch_pypi_urls               # all packages in results.csv
    uv run -m src.pypi.fetch_pypi_urls --refresh     # bypass cache
    uv run -m src.pypi.fetch_pypi_urls --limit 50    # smoke test
"""

import argparse
import asyncio
import csv
import json
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

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RESULTS = DATA_DIR / "sources" / "pypi" / "results.csv"
CACHE_DIR = DATA_DIR / "sources" / "pypi" / "raw" / "api-cache"
OUTPUT = DATA_DIR / "sources" / "pypi" / "raw" / "package-urls.csv"

API_URL = "https://pypi.org/pypi/{name}/json"
HEADERS = {"User-Agent": "ose-model fetch_pypi_urls.py"}
CONCURRENCY = 10
TIMEOUT = 20


def load_targets() -> list[str]:
    if not RESULTS.exists():
        return []
    with open(RESULTS, encoding="utf-8") as f:
        return [r["package"] for r in csv.DictReader(f)]


async def fetch_one(
    session: aiohttp.ClientSession, name: str, refresh: bool
) -> dict:
    cache = CACHE_DIR / f"{name.replace('/', '_')}.json"
    if not refresh and cache.exists():
        try:
            return json.loads(cache.read_text())
        except json.JSONDecodeError:
            pass
    try:
        async with session.get(
            API_URL.format(name=name),
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as r:
            if r.status == 404:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache.write_text("{}")
                return {}
            if r.status != 200:
                return {}
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def extract_urls(data: dict) -> list[str]:
    """Pull every URL from `info.project_urls` + `info.home_page`."""
    info = data.get("info") or {}
    out: list[str] = []
    project_urls = info.get("project_urls") or {}
    if isinstance(project_urls, dict):
        for url in project_urls.values():
            if url and isinstance(url, str):
                out.append(url.strip())
    home = info.get("home_page")
    if home and isinstance(home, str):
        out.append(home.strip())
    # Dedupe but keep order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


async def main_async(args: argparse.Namespace) -> None:
    targets = load_targets()
    if args.limit:
        targets = targets[: args.limit]

    console.rule("[bold white]fetch_pypi_urls.py[/bold white]")
    console.print(f"  Targets : [cyan]{len(targets):,}[/cyan]")
    console.print(f"  Cache   : [cyan]{CACHE_DIR}[/cyan]")
    console.print(f"  Output  : [cyan]{OUTPUT}[/cyan]")
    console.print()

    sem = asyncio.Semaphore(CONCURRENCY)
    rows: list[dict] = []
    found = 0

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

            async def process(name: str) -> None:
                nonlocal found
                async with sem:
                    progress.update(task_id, description=f"[cyan]{name[:36]}[/cyan]")
                    data = await fetch_one(session, name, args.refresh)
                    urls = extract_urls(data)
                    if urls:
                        found += 1
                    for url in urls:
                        rows.append({"package": name, "url": url})
                    progress.advance(task_id)

            await asyncio.gather(*(process(n) for n in targets))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["package", "url"], quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    console.print()
    console.print(f"[green]✓[/green] {found:,}/{len(targets):,} packages returned URLs")
    console.print(f"[green]✓[/green] {len(rows):,} URL rows → {OUTPUT}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, help="Process at most N packages")
    p.add_argument("--refresh", action="store_true", help="Bypass cache, re-fetch")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
