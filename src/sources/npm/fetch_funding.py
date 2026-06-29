#!/usr/bin/env python3
"""Fetch the npm registry `funding` field for the top npm packages.

`npm fund` surfaces each package's package.json `funding` field — a registry-native
funding declaration (a GitHub Sponsors / OpenCollective / custom URL). It is an
ADDITIVE funding signal: a package can declare a funding channel here even when the
owner-level GitHub-Sponsors / FUNDING.yml / OpenCollective checks miss it (e.g. the
`chalk/*` family declare `github.com/chalk/<x>?sponsor=1`).

Scope: the top npm packages — the `top_eco_pkg` of every npm risk-scope repo (those
whose `top_eco` is `npm` in value.csv). Reads the public registry
`https://registry.npmjs.org/<pkg>/latest` and extracts `.funding` (string | {url} |
[{url}]). npm-only by nature — there is no equivalent field for cpp.

Writes data/sources/npm/funding.csv:
    repo, package, has_npm_funding, npm_funding_url, fetched_at, status

`status` distinguishes a genuine "no funding field" (`ok`) from a fetch miss
(`not_found` / `error`), so a blank `has_npm_funding` never silently stands in for
a failed lookup.

Gap-filling: a normal run only fetches repos missing from the file or older than
the shared funding TTL (`FUNDING_TTL_DAYS` = 365 days); an `error` row is never
fresh, so a transient failure is always retried. Re-running inside the window
fetches nothing and rewrites nothing (idempotent). Pass --force to refetch all.

Usage:
    uv run python -m src.sources.npm.fetch_funding            # all npm top repos
    uv run python -m src.sources.npm.fetch_funding --limit 20 # smoke test
    uv run python -m src.sources.npm.fetch_funding --force    # ignore the TTL cache
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
from src.common.repos import load_top_repos

console = Console()
ROOT = Path(__file__).resolve().parent.parent.parent.parent
VALUE_FILE = ROOT / "data" / "value" / "value.csv"
OUTPUT_FILE = ROOT / "data" / "sources" / "npm" / "funding.csv"
REGISTRY = "https://registry.npmjs.org"
FIELDS = ["repo", "package", "has_npm_funding", "npm_funding_url", "fetched_at", "status"]


def npm_top_packages() -> list[tuple[str, str]]:
    """(repo, top npm package) for every npm-primary risk-scope repo."""
    scope = {e.repo for e in load_top_repos()}
    out: list[tuple[str, str]] = []
    with VALUE_FILE.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gh = (r.get("github_repo") or "").strip().lower()
            if gh in scope and (r.get("top_eco") or "").strip() == "npm":
                pkg = (r.get("top_eco_pkg") or "").strip()
                if pkg:
                    out.append((gh, pkg))
    return out


def _funding_url(funding: object) -> str:
    """Extract a single URL from npm's polymorphic `funding` field."""
    if isinstance(funding, str):
        return funding
    if isinstance(funding, dict):
        return (funding.get("url") or "").strip()
    if isinstance(funding, list) and funding:
        first = funding[0]
        return (first.get("url") if isinstance(first, dict) else str(first)) or ""
    return ""


def fetch_one(pkg: str) -> tuple[str, str]:
    """Return (status, funding_url) for one package's latest version."""
    url = f"{REGISTRY}/{urllib.parse.quote(pkg, safe='')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return ("not_found" if e.code == 404 else "error", "")
    except Exception:
        return ("error", "")
    return ("ok", _funding_url(data.get("funding")))


def fetch(targets: list[tuple[str, str]], concurrency: int = 16) -> list[dict]:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = ex.map(lambda t: fetch_one(t[1]), targets)
        for (repo, pkg), (status, furl) in zip(targets, results):
            rows.append({
                "repo": repo, "package": pkg,
                "has_npm_funding": "True" if furl else "False",
                "npm_funding_url": furl,
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

    console.print(f"[bold]npm funding-field fetcher[/bold] — "
                  f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    targets = npm_top_packages()

    # Gap-fill: keep rows still fresh within the TTL, fetch only missing/stale repos.
    existing = _load_existing()
    fresh = set() if args.force else {
        repo for repo, row in existing.items() if row_is_fresh(row, status_key="status")
    }
    to_fetch = [(repo, pkg) for repo, pkg in targets if repo not in fresh]
    if args.limit and args.limit < len(to_fetch):
        to_fetch = to_fetch[:args.limit]
    console.print(f"npm top repos: {len(targets)}, {len(to_fetch)} to fetch, "
                  f"{len(targets) - len(to_fetch)} already fresh")

    rows = fetch(to_fetch, concurrency=args.concurrency) if to_fetch else []
    for row in rows:
        existing[row["repo"]] = row
    if rows:
        _write(existing)  # merge fetched over kept, write the union
    else:
        console.print("[dim]All fresh within TTL — nothing to fetch.[/dim]")

    declared = sum(1 for r in existing.values() if r.get("has_npm_funding") == "True")
    errors = sum(1 for r in existing.values() if (r.get("status") or "") != "ok")
    t = Table(title="[bold]npm funding[/bold]", header_style="bold dim", padding=(0, 1))
    t.add_column("metric")
    t.add_column("repos", justify="right")
    t.add_row("in scope", f"{len(targets):,}")
    t.add_row("fetched this run", f"{len(rows):,}")
    t.add_row("declare a funding field", f"{declared:,}")
    t.add_row("fetch errors", f"{errors:,}")
    console.print(t)
    console.print(f"[dim]→ {OUTPUT_FILE}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
