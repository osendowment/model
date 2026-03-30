"""
Fetch enriched package metadata from the npm registry (latest version endpoint).

For each package in the input CSV, fetches:
  description, keywords, repo_url, github_repo, homepage, license, fetched_at

Output: data/npm/nice-registry/metadata.csv
Append-only — packages already in the output are skipped unless --refresh is set.

Run:
    uv run src/npm/fetch_nice_registry.py
    uv run src/npm/fetch_nice_registry.py --limit 50
    uv run src/npm/fetch_nice_registry.py --concurrency 20
    uv run src/npm/fetch_nice_registry.py --refresh
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

log = logging.getLogger(__name__)

INPUT_CSV     = "data/npm/top-package-downloads.csv"
OUTPUT_CSV    = "data/npm/nice-registry/metadata.csv"
NPM_REGISTRY  = "https://registry.npmjs.org"
CONCURRENCY   = 10
RATE_PER_SEC  = 5.0
MAX_RETRIES   = 3
RETRY_BACKOFF = [2, 5, 15]
USER_AGENT    = "osendowment-model/1.0 (research; +https://endowment.dev)"
FLUSH_EVERY   = 50

OUTPUT_FIELDS = [
    "package", "description", "keywords", "repo_url",
    "github_repo", "homepage", "license", "fetched_at",
]

GITHUB_PAT = re.compile(
    r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)

console = Console()


# ── Rate limiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    """Token bucket — limits to `rate` requests per second."""
    def __init__(self, rate: float):
        self._rate = rate
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now  = asyncio.get_event_loop().time()
            wait = self._last + 1.0 / self._rate - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ── I/O helpers ────────────────────────────────────────────────────────────────

def load_packages(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [row["package"].strip() for row in csv.DictReader(f) if row.get("package")]


def load_done(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {row["package"] for row in csv.DictReader(f) if row.get("package")}


def flush_rows(path: str, rows: list[dict]) -> None:
    is_new = not os.path.exists(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore",
                                quoting=csv.QUOTE_ALL)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


# ── Parsing helpers ────────────────────────────────────────────────────────────

def parse_github_repo(url: str) -> str:
    if not url:
        return ""
    m = GITHUB_PAT.search(url)
    return m.group(1).rstrip("/") if m else ""


def parse_license(lic) -> str:
    if not lic:
        return ""
    if isinstance(lic, str):
        return lic
    if isinstance(lic, dict):
        return lic.get("type") or lic.get("name") or ""
    return str(lic)


# ── Fetch one package ──────────────────────────────────────────────────────────

async def fetch_one(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    rl: RateLimiter,
    package: str,
) -> dict | None:
    url = f"{NPM_REGISTRY}/{package}/latest"
    async with sem:
        for attempt in range(MAX_RETRIES):
            await rl.acquire()
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 404:
                        log.debug("%s: 404, skipping", package)
                        return {
                            "package":     package,
                            "description": "",
                            "keywords":    "",
                            "repo_url":    "",
                            "github_repo": "",
                            "homepage":    "",
                            "license":     "",
                            "fetched_at":  datetime.now(timezone.utc).isoformat(),
                        }
                    if r.status == 429:
                        wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                        log.debug("%s: 429, backing off %ds", package, wait)
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    data = await r.json(content_type=None)

                    repo = data.get("repository") or {}
                    if isinstance(repo, str):
                        repo_url = repo
                    else:
                        repo_url = repo.get("url") or ""

                    keywords_raw = data.get("keywords") or []
                    keywords = "|".join(k.strip() for k in keywords_raw if k.strip())

                    return {
                        "package":     package,
                        "description": (data.get("description") or "").strip(),
                        "keywords":    keywords,
                        "repo_url":    repo_url,
                        "github_repo": parse_github_repo(repo_url),
                        "homepage":    (data.get("homepage") or "").strip(),
                        "license":     parse_license(data.get("license")),
                        "fetched_at":  datetime.now(timezone.utc).isoformat(),
                    }

            except Exception as exc:
                log.warning("%s (attempt %d): %s", package, attempt + 1, exc)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

    log.error("%s: all retries exhausted", package)
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    packages = load_packages(args.input)
    done     = set() if args.refresh else load_done(args.output)

    to_fetch = [p for p in packages if p not in done]
    if args.limit:
        to_fetch = to_fetch[:args.limit]

    skipped = len(packages) - len(to_fetch) if not args.limit else len(done)

    console.print(f"  Input     : [cyan]{args.input}[/cyan]  ({len(packages):,} packages)")
    console.print(f"  Output    : [cyan]{args.output}[/cyan]")
    console.print(f"  To fetch  : [yellow]{len(to_fetch):,}[/yellow]")
    console.print(f"  Skipped   : [dim]{skipped:,} already done[/dim]")
    if args.limit:
        console.print(f"  Limit     : [yellow]{args.limit}[/yellow] (test mode)")
    if args.refresh:
        console.print(f"  Mode      : [yellow]--refresh (re-fetching all)[/yellow]")
    console.print()

    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    sem = asyncio.Semaphore(args.concurrency)
    rl  = RateLimiter(RATE_PER_SEC)

    pending:  list[dict] = []
    fetched   = 0
    errors    = 0
    t0        = time.perf_counter()

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        futs = [
            asyncio.ensure_future(fetch_one(session, sem, rl, pkg))
            for pkg in to_fetch
        ]

        with tqdm(total=len(futs), desc="fetching", unit="pkg") as bar:
            for fut in asyncio.as_completed(futs):
                result = await fut
                bar.update(1)

                if result is None:
                    errors += 1
                    continue

                pending.append(result)
                fetched += 1

                if len(pending) >= FLUSH_EVERY:
                    flush_rows(args.output, pending)
                    pending.clear()

        if pending:
            flush_rows(args.output, pending)

    elapsed = time.perf_counter() - t0
    rate    = fetched / elapsed if elapsed > 0 else 0

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Metric",           min_width=20)
    table.add_column("Value",            justify="right")
    table.add_row("Packages fetched",    f"[green]{fetched:,}[/green]")
    table.add_row("Errors / skipped",    f"[red]{errors:,}[/red]")
    table.add_row("Already done",        f"[dim]{skipped:,}[/dim]")
    table.add_row("Elapsed",             f"{elapsed:.1f}s")
    table.add_row("Rate",                f"{rate:.1f} pkg/s")
    table.add_row("Output",              f"[dim]{args.output}[/dim]")
    console.print()
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch enriched npm package metadata from the registry"
    )
    parser.add_argument("--input",       default=INPUT_CSV,   help="Input CSV with package names")
    parser.add_argument("--output",      default=OUTPUT_CSV,  help="Output CSV path")
    parser.add_argument("--limit",       type=int, default=None, help="Max packages to fetch (for testing)")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="Concurrent requests")
    parser.add_argument("--refresh",     action="store_true", help="Re-fetch already completed packages")
    parser.add_argument("--log-level",   default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s %(name)s: %(message)s")

    console.rule(
        f"[bold]npm nice-registry fetch[/bold]  "
        f"[dim]{datetime.now():%Y-%m-%d %H:%M:%S}[/dim]"
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
