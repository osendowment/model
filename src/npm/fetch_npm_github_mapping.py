"""Fetch GitHub repo mappings for npm packages via the npm registry.

Reads package names from data/npm/top-package-downloads.csv, fetches each
package's repository metadata from the npm registry, and saves the
package → GitHub repo mapping to data/npm/package-github-mapping.csv.
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import time
from datetime import datetime

import aiohttp
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

log = logging.getLogger(__name__)

console = Console()

INPUT_CSV = "data/npm/top-package-downloads.csv"
OUTPUT_CSV = "data/npm/package-github-mapping.csv"
NPM_REGISTRY = "https://registry.npmjs.org"
CONCURRENCY = 5
MAX_RETRIES = 4
RETRY_BACKOFF = [1, 2, 5, 10]  # seconds between retries

GITHUB_PATTERNS = [
    # git+https://github.com/owner/repo.git
    re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"),
]


def _parse_github_repo(url: str) -> str | None:
    """Extract 'owner/repo' from a repository URL, or None if not GitHub."""
    if not url:
        return None
    for pat in GITHUB_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1).rstrip("/")
    return None


def _load_packages(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["package"].strip() for row in reader if row.get("package")]


def _load_existing(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["package"] for row in reader if row.get("package")}


async def _fetch_repo(session: aiohttp.ClientSession, sem: asyncio.Semaphore, package: str) -> tuple[str, str, str]:
    """Return (package, github_repo, repo_url). github_repo is '' if not found."""
    async with sem:
        url = f"{NPM_REGISTRY}/{package}/latest"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 404:
                    log.debug("%s: 404", package)
                    return (package, "", "")
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                repo = data.get("repository") or {}
                if isinstance(repo, str):
                    repo_url = repo
                else:
                    repo_url = repo.get("url", "")
                github_repo = _parse_github_repo(repo_url) or ""
                log.debug("%s → %s (%s)", package, github_repo, repo_url)
                return (package, github_repo, repo_url)
        except Exception as exc:
            log.warning("%s: %s", package, exc)
            return (package, "", "")


async def run(packages: list[str], output: str, limit: int | None) -> None:
    existing = _load_existing(output)
    to_fetch = [p for p in packages if p not in existing]

    if limit:
        to_fetch = to_fetch[:limit]

    skipped = len(packages) - len(to_fetch) if not limit else 0
    console.print(f"[bold]npm → GitHub mapping[/bold]: "
                  f"{len(packages)} packages, "
                  f"[yellow]{len(to_fetch)}[/yellow] to fetch, "
                  f"[dim]{skipped} already done[/dim]")

    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    results: list[tuple[str, str, str]] = []

    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("fetching...", total=len(to_fetch))

            FLUSH_EVERY = 100
            pending: list[tuple[str, str, str]] = []

            def flush(rows: list[tuple[str, str, str]]) -> None:
                is_new = not os.path.exists(output)
                os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
                with open(output, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    if is_new:
                        writer.writerow(["package", "github_repo", "repo_url"])
                    writer.writerows(rows)

            coros = [_fetch_repo(session, sem, pkg) for pkg in to_fetch]
            for coro in asyncio.as_completed(coros):
                result = await coro
                results.append(result)
                pending.append(result)
                progress.advance(task)

                if len(pending) >= FLUSH_EVERY:
                    flush(pending)
                    pending.clear()

            if pending:
                flush(pending)

    found = sum(1 for _, gh, _ in results if gh)
    not_found = len(results) - found

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Metric", min_width=18)
    table.add_column("Value", justify="right")
    table.add_row("Packages fetched", str(len(results)))
    table.add_row("GitHub repos found", f"[green]{found}[/green]")
    table.add_row("No GitHub repo", f"[dim]{not_found}[/dim]")
    table.add_row("Skipped (cached)", f"[dim]{skipped}[/dim]")
    table.add_row("Output", f"[dim]{output}[/dim]")
    console.print()
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch npm package → GitHub repo mappings")
    parser.add_argument("--input", default=INPUT_CSV, help="Input CSV with package names")
    parser.add_argument("--output", default=OUTPUT_CSV, help="Output CSV path")
    parser.add_argument("--limit", type=int, default=None, help="Max packages to fetch (for testing)")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s %(name)s: %(message)s")

    console.rule(f"[bold]npm GitHub Mapping[/bold]  [dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")
    console.print(f"  Input:  [cyan]{args.input}[/cyan]")
    console.print(f"  Output: [cyan]{args.output}[/cyan]")
    if args.limit:
        console.print(f"  Limit:  [yellow]{args.limit}[/yellow]")
    console.print()

    packages = _load_packages(args.input)
    asyncio.run(run(packages, args.output, args.limit))


if __name__ == "__main__":
    main()
