#!/usr/bin/env python3
"""Check EOL for npm packages via the npm registry `deprecated` field.

Method: a package is EOL if its `dist-tags.latest` version has a non-empty
`deprecated` string. This is exactly what `npm deprecate <pkg> "<msg>"`
sets and what `npm install` warns about — the canonical maintainer-set
deprecation signal for npm.

Uses the abbreviated metadata endpoint (`application/vnd.npm.install-v1+json`)
which is much smaller than the full one but still includes `deprecated`.

Reads:
    data/npm/results.csv — packages we care about

Writes:
    data/npm/eol.csv

Usage:
    uv run python -m src.npm.check_eol
    uv run python -m src.npm.check_eol --limit 100
    uv run python -m src.npm.check_eol --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from datetime import datetime
from pathlib import Path

import aiohttp
from rich.console import Console
from tqdm.asyncio import tqdm_asyncio

from src.eol_common import display_summary, now_iso, write_eol

logging.basicConfig(level="INFO")
log = logging.getLogger(__name__)

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RESULTS_FILE = DATA_DIR / "npm" / "results.csv"
OUTPUT_FILE = DATA_DIR / "npm" / "eol.csv"

REGISTRY = "https://registry.npmjs.org"
ABBREV_HEADER = "application/vnd.npm.install-v1+json"
EOL_METHOD = "npm_deprecated"


def load_packages(limit: int | None) -> list[str]:
    pkgs: list[str] = []
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pkgs.append(r["package"])
            if limit and len(pkgs) >= limit:
                break
    return pkgs


async def fetch_one(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                    package: str) -> dict:
    """Return one EOL row for `package`."""
    checked_at = now_iso()
    # Scoped names like @babel/types must be %2F-encoded
    encoded = package.replace("/", "%2F")
    url = f"{REGISTRY}/{encoded}"
    async with sem:
        try:
            async with session.get(url, headers={"Accept": ABBREV_HEADER}, timeout=20) as r:
                if r.status == 404:
                    return _row(package, False, "", "not_found", checked_at)
                if r.status != 200:
                    return _row(package, False, f"http {r.status}", "error", checked_at)
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return _row(package, False, str(e)[:80], "error", checked_at)

    latest = (data.get("dist-tags") or {}).get("latest")
    if not latest:
        return _row(package, False, "no latest tag", "error", checked_at)
    versions = data.get("versions") or {}
    dep_msg = (versions.get(latest) or {}).get("deprecated") or ""
    is_eol = bool(dep_msg)
    return _row(package, is_eol, dep_msg if is_eol else "", "registry", checked_at)


def _row(package: str, is_eol: bool, reason: str, source: str, checked_at: str) -> dict:
    return {
        "package": package,
        "is_eol": is_eol,
        "eol_method": EOL_METHOD,
        "eol_reason": reason,
        "source": source,
        "eol_checked_at": checked_at,
    }


async def run(packages: list[str], concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=20)
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [fetch_one(session, sem, p) for p in packages]
        return await tqdm_asyncio.gather(*tasks, desc="npm eol", unit="pkg")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=30)
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]npm.check_eol[/bold]  limit={args.limit or '∞'}  "
                  f"concurrency={args.concurrency}  started={started:%Y-%m-%d %H:%M:%S}")

    pkgs = load_packages(args.limit)
    log.info("loaded %d packages from results.csv", len(pkgs))

    rows = asyncio.run(run(pkgs, args.concurrency))
    write_eol(OUTPUT_FILE, rows)

    display_summary(console, "npm", rows)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(rows)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
