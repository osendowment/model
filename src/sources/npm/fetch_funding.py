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

Usage:
    uv run python -m src.sources.npm.fetch_funding            # all npm top repos
    uv run python -m src.sources.npm.fetch_funding --limit 20 # smoke test
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only the first N packages")
    ap.add_argument("--concurrency", type=int, default=16)
    args = ap.parse_args()

    console.print(f"[bold]npm funding-field fetcher[/bold] — "
                  f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    targets = npm_top_packages()
    if args.limit:
        targets = targets[:args.limit]
    console.print(f"npm top repos: {len(targets)}")

    rows = fetch(targets, concurrency=args.concurrency)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    declared = sum(1 for r in rows if r["has_npm_funding"] == "True")
    errors = sum(1 for r in rows if r["status"] != "ok")
    t = Table(title="[bold]npm funding[/bold]", header_style="bold dim", padding=(0, 1))
    t.add_column("metric")
    t.add_column("repos", justify="right")
    t.add_row("fetched", f"{len(rows):,}")
    t.add_row("declare a funding field", f"{declared:,}")
    t.add_row("fetch errors", f"{errors:,}")
    console.print(t)
    console.print(f"[dim]→ {OUTPUT_FILE}[/dim]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
