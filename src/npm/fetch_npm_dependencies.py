"""Fetch dependencies for top npm packages via the npm registry.

Reads package names from data/npm/top-package-downloads.csv, fetches each
package's latest version dependencies from the npm registry, and upserts
rows to data/npm/package-dependencies.csv.
"""

import argparse
import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

log = logging.getLogger(__name__)

console = Console()

INPUT_CSV = "data/npm/top-package-downloads.csv"
OUTPUT_CSV = "data/npm/package-dependencies.csv"
NPM_REGISTRY = "https://registry.npmjs.org"
CONCURRENCY = 5
MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 15]
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"


def _load_packages(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["package"].strip() for row in reader if row.get("package")]


def _load_fetched_packages(path: str) -> set[str]:
    """Return set of package names already present in output CSV."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["package"] for row in reader if row.get("package")}


async def _fetch_deps(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    package: str,
) -> tuple[str, list[tuple[str, str]], str | None]:
    """Return (package, [(dep_name, dep_version), ...], error_or_None)."""
    url = f"{NPM_REGISTRY}/{package}/latest"
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 404:
                        log.debug("%s: 404 not found", package)
                        return (package, [], None)
                    if resp.status == 429:
                        retry_after = resp.headers.get("retry-after")
                        wait = int(retry_after) if retry_after and retry_after.isdigit() else RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                        log.warning("%s: rate limited (retry-after=%s), waiting %ss", package, retry_after, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    deps = data.get("dependencies") or {}
                    pairs = [(name, ver) for name, ver in deps.items()]
                    log.debug("%s: %d deps", package, len(pairs))
                    return (package, pairs, None)
            except asyncio.TimeoutError:
                err = "timeout"
            except aiohttp.ClientError as exc:
                err = str(exc)
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.warning("%s: %s, retry %d in %ss", package, err, attempt + 1, wait)
                await asyncio.sleep(wait)
            else:
                log.warning("%s: failed after %d retries: %s", package, MAX_RETRIES, err)
                return (package, [], err)
    return (package, [], "semaphore error")


def _remove_package_rows(path: str, packages: set[str]) -> None:
    """Rewrite CSV removing all rows for the given packages (for upsert)."""
    if not os.path.exists(path):
        return
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or ["package", "dep_name", "dep_version", "fetched_at"]
        rows = [row for row in reader if row.get("package") not in packages]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _append_rows(path: str, rows: list[tuple[str, str, str, str]]) -> None:
    is_new = not os.path.exists(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["package", "dep_name", "dep_version", "fetched_at"])
        writer.writerows(rows)


async def run(packages: list[str], output: str, limit: int | None, refresh: bool, concurrency: int = CONCURRENCY) -> None:
    existing = _load_fetched_packages(output)

    if refresh:
        to_fetch = packages
        console.print(f"[yellow]--refresh[/yellow]: re-fetching all {len(packages)} packages")
    else:
        to_fetch = [p for p in packages if p not in existing]

    if limit:
        to_fetch = to_fetch[:limit]

    skipped = len(packages) - len(to_fetch) if not refresh and not limit else 0

    console.print(
        f"[bold]npm package dependencies[/bold]: "
        f"{len(packages)} total, "
        f"[yellow]{len(to_fetch)}[/yellow] to fetch, "
        f"[dim]{skipped} already done[/dim]"
    )

    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    start = time.monotonic()
    results: list[tuple[str, list[tuple[str, str]], str | None]] = []

    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("fetching...", total=len(to_fetch))

            FLUSH_EVERY = 50
            pending_pkgs: set[str] = set()
            pending_rows: list[tuple[str, str, str, str]] = []

            def flush() -> None:
                if refresh:
                    _remove_package_rows(output, pending_pkgs)
                _append_rows(output, pending_rows)
                pending_pkgs.clear()
                pending_rows.clear()

            coros = [_fetch_deps(session, sem, pkg) for pkg in to_fetch]
            for coro in asyncio.as_completed(coros):
                pkg, deps, _err = await coro
                results.append((pkg, deps, _err))
                fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                pending_pkgs.add(pkg)
                for dep_name, dep_ver in deps:
                    pending_rows.append((pkg, dep_name, dep_ver, fetched_at))
                progress.advance(task)

                if len(pending_pkgs) >= FLUSH_EVERY:
                    flush()

            if pending_pkgs:
                flush()

    elapsed = time.monotonic() - start
    total_deps = sum(len(deps) for _, deps, _ in results)
    errors = sum(1 for _, _, err in results if err)
    no_deps = sum(1 for _, deps, err in results if not deps and not err)

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Metric", min_width=22)
    table.add_column("Value", justify="right")
    table.add_row("Packages fetched", str(len(results)))
    table.add_row("Total dep rows written", f"[green]{total_deps}[/green]")
    table.add_row("Packages with no deps", f"[dim]{no_deps}[/dim]")
    table.add_row("Errors", f"[red]{errors}[/red]" if errors else "[dim]0[/dim]")
    table.add_row("Skipped (cached)", f"[dim]{skipped}[/dim]")
    table.add_row("Elapsed", f"[dim]{elapsed:.1f}s[/dim]")
    table.add_row("Output", f"[dim]{output}[/dim]")
    console.print()
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch npm package dependencies")
    parser.add_argument("--input", default=INPUT_CSV, help="Input CSV with package names")
    parser.add_argument("--output", default=OUTPUT_CSV, help="Output CSV path")
    parser.add_argument("--limit", type=int, default=None, help="Max packages to fetch (for testing)")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch packages already in output (upsert)")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="Max concurrent requests")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s: %(message)s")

    console.rule(f"[bold]npm Dependency Fetcher[/bold]  [dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]")
    console.print(f"  Input:       [cyan]{args.input}[/cyan]")
    console.print(f"  Output:      [cyan]{args.output}[/cyan]")
    console.print(f"  Concurrency: [cyan]{args.concurrency}[/cyan]")
    if args.limit:
        console.print(f"  Limit:       [yellow]{args.limit}[/yellow]")
    if args.refresh:
        console.print("  Mode:        [yellow]refresh (upsert)[/yellow]")
    console.print()

    packages = _load_packages(args.input)
    asyncio.run(run(packages, args.output, args.limit, args.refresh, args.concurrency))


if __name__ == "__main__":
    main()
