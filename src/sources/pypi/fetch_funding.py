#!/usr/bin/env python3
"""Fetch declared funding channels from PyPI `project_urls`.

The PyPI analog of `npm fund` (see src.sources.npm.fetch_funding): a package can
declare a funding channel in its metadata `project_urls` — a GitHub Sponsors /
OpenCollective / Tidelift / custom donate URL under a free-form key (`Funding`,
`Donate`, `Sponsor`, …). It is an ADDITIVE funding signal: a package can name a
channel here even when the owner-level GitHub-Sponsors / FUNDING.yml / OpenCollective
checks miss it. We only need channel *presence + platform*, not the exact handle.

Scope: the top pypi packages — the `top_eco_pkg` of every pypi risk-scope repo
(`top_eco == pypi` in value.csv). Reads `https://pypi.org/pypi/<pkg>/json` and scans
`info.project_urls`: a URL is funding if it resolves to a known funding platform
(`platform_handle_from_url`) OR its key looks funding-ish.

Writes data/sources/pypi/funding.csv:
    repo, package, has_pypi_funding, pypi_funding_platforms, pypi_funding_url,
    fetched_at, status

`status` distinguishes "no funding url" (`ok`) from a fetch miss (`not_found` /
`error`).

Gap-filling: a normal run only fetches repos missing from the file or older than
the shared funding TTL (`FUNDING_TTL_DAYS` = 365 days); an `error` row is never
fresh, so a transient failure is always retried. Re-running inside the window
fetches nothing and rewrites nothing (idempotent). Pass --force to refetch all.

Usage:
    uv run python -m src.sources.pypi.fetch_funding            # all pypi top repos
    uv run python -m src.sources.pypi.fetch_funding --limit 20 # smoke test
    uv run python -m src.sources.pypi.fetch_funding --force    # ignore the TTL cache
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.freshness import row_is_fresh
from src.common.funding_platforms import platform_handle_from_url
from src.common.repos import load_top_repos

console = Console()
ROOT = Path(__file__).resolve().parent.parent.parent.parent
VALUE_FILE = ROOT / "data" / "value" / "value.csv"
OUTPUT_FILE = ROOT / "data" / "sources" / "pypi" / "funding.csv"
PYPI_JSON = "https://pypi.org/pypi"
FIELDS = ["repo", "package", "has_pypi_funding", "pypi_funding_platforms",
          "pypi_funding_url", "fetched_at", "status"]
_FUNDING_KEY_HINTS = ("fund", "sponsor", "donate", "tidelift", "patreon", "give")


def pypi_top_packages() -> list[tuple[str, str]]:
    """(repo, top pypi package) for every pypi-primary risk-scope repo."""
    scope = {e.repo for e in load_top_repos()}
    out: list[tuple[str, str]] = []
    with VALUE_FILE.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gh = (r.get("github_repo") or "").strip().lower()
            if gh in scope and (r.get("top_eco") or "").strip() == "pypi":
                pkg = (r.get("top_eco_pkg") or "").strip()
                if pkg:
                    out.append((gh, pkg))
    return out


def funding_channels(project_urls: dict) -> tuple[list[str], str]:
    """Return (sorted platforms, first funding url) from a project_urls dict.

    A URL counts as funding if it resolves to a known platform OR its key hints
    at funding. Unknown funding-keyed URLs are filed under `custom`.
    """
    platforms: set[str] = set()
    first = ""
    for key, url in (project_urls or {}).items():
        platform, _slug = platform_handle_from_url(url)
        if platform is None and not any(h in key.lower() for h in _FUNDING_KEY_HINTS):
            continue
        platforms.add(platform or "custom")
        first = first or (url or "").strip()
    return sorted(platforms), first


def fetch_one(pkg: str) -> tuple[str, list[str], str]:
    """Return (status, platforms, first_url) for one package."""
    url = f"{PYPI_JSON}/{urllib.parse.quote(pkg)}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return ("not_found" if e.code == 404 else "error", [], "")
    except Exception:
        return ("error", [], "")
    platforms, first = funding_channels(data.get("info", {}).get("project_urls"))
    return ("ok", platforms, first)


def fetch(targets: list[tuple[str, str]], concurrency: int = 16) -> list[dict]:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = ex.map(lambda t: fetch_one(t[1]), targets)
        for (repo, pkg), (status, platforms, first) in zip(targets, results):
            rows.append({
                "repo": repo, "package": pkg,
                "has_pypi_funding": "True" if platforms else "False",
                "pypi_funding_platforms": ",".join(platforms),
                "pypi_funding_url": first,
                "fetched_at": now, "status": status,
            })
    return rows


def _load_existing() -> dict[str, dict]:
    """Existing output rows keyed by `repo`."""
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with OUTPUT_FILE.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _write(rows: dict[str, dict]) -> None:
    """Write the union, sorted by `repo` for deterministic output."""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only the first N packages")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--force", action="store_true", help="refetch all, ignoring the TTL cache")
    args = ap.parse_args()

    console.print(f"[bold]PyPI funding-channel fetcher[/bold] — "
                  f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    targets = pypi_top_packages()

    # Gap-fill: keep rows still fresh within the TTL, fetch only missing/stale repos.
    existing = _load_existing()
    fresh = set() if args.force else {
        repo for repo, row in existing.items() if row_is_fresh(row, status_key="status")
    }
    to_fetch = [(repo, pkg) for repo, pkg in targets if repo not in fresh]
    if args.limit and args.limit < len(to_fetch):
        to_fetch = to_fetch[:args.limit]
    console.print(f"pypi top repos: {len(targets)}, {len(to_fetch)} to fetch, "
                  f"{len(targets) - len(to_fetch)} already fresh")

    rows = fetch(to_fetch, concurrency=args.concurrency) if to_fetch else []
    for row in rows:
        existing[row["repo"]] = row
    if rows:
        _write(existing)  # merge fetched over kept, write the union
    else:
        console.print("[dim]All fresh within TTL — nothing to fetch.[/dim]")

    declared = sum(1 for r in existing.values() if r.get("has_pypi_funding") == "True")
    errors = sum(1 for r in existing.values() if (r.get("status") or "") != "ok")
    t = Table(title="[bold]PyPI funding[/bold]", header_style="bold dim", padding=(0, 1))
    t.add_column("metric")
    t.add_column("repos", justify="right")
    t.add_row("in scope", f"{len(targets):,}")
    t.add_row("fetched this run", f"{len(rows):,}")
    t.add_row("declare a funding channel", f"{declared:,}")
    t.add_row("fetch errors", f"{errors:,}")
    console.print(t)
    console.print(f"[dim]→ {OUTPUT_FILE}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
