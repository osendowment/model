#!/usr/bin/env python3
"""Check EOL for PyPI packages via the `Inactive` Trove classifier.

Method: a package is EOL if its latest release on PyPI carries the
classifier `Development Status :: 7 - Inactive`. This is the long-standing,
maintainer-set deprecation marker on PyPI — set explicitly in `setup.cfg`
or `pyproject.toml`. PyPI exposes it via the JSON API.

There is no separate per-project deprecation flag on PyPI; classifiers are
the canonical way maintainers declare a package's lifecycle.

Reads:
    data/pypi/results.csv

Writes:
    data/pypi/eol.csv

Usage:
    uv run python -m src.pypi.check_eol
    uv run python -m src.pypi.check_eol --limit 100
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
RESULTS_FILE = DATA_DIR / "pypi" / "results.csv"
OUTPUT_FILE = DATA_DIR / "pypi" / "eol.csv"

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
INACTIVE_CLASSIFIER = "Development Status :: 7 - Inactive"
EOL_METHOD = "pypi_inactive"


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
    checked_at = now_iso()
    url = PYPI_JSON.format(name=package)
    async with sem:
        try:
            async with session.get(url, timeout=20) as r:
                if r.status == 404:
                    return _row(package, False, "", "not_found", checked_at)
                if r.status != 200:
                    return _row(package, False, f"http {r.status}", "error", checked_at)
                data = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return _row(package, False, str(e)[:80], "error", checked_at)

    classifiers = (data.get("info") or {}).get("classifiers") or []
    is_eol = INACTIVE_CLASSIFIER in classifiers
    return _row(package, is_eol, INACTIVE_CLASSIFIER if is_eol else "",
                "registry", checked_at)


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
        return await tqdm_asyncio.gather(*tasks, desc="pypi eol", unit="pkg")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=20)
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]pypi.check_eol[/bold]  limit={args.limit or '∞'}  "
                  f"concurrency={args.concurrency}  started={started:%Y-%m-%d %H:%M:%S}")

    pkgs = load_packages(args.limit)
    log.info("loaded %d packages from results.csv", len(pkgs))

    rows = asyncio.run(run(pkgs, args.concurrency))
    write_eol(OUTPUT_FILE, rows)

    display_summary(console, "pypi", rows)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(rows)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
