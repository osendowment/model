#!/usr/bin/env python3
"""Check EOL (end-of-life) status for value-pipeline projects via GitHub `archived` flag.

Simple method, best coverage: GitHub's `archived` boolean is the only explicit EOL
signal that works uniformly across npm/PyPI/crates/cpp libraries. ~95% of A+B repos
already have this in data/github/search/top-repos.csv from prior fetches; remaining
repos are fetched from the GitHub API on demand.

Output: data/eol.csv with columns: repo, value_class, archived, pushed_at,
days_since_push, is_eol, source.

`is_eol = archived` only. `pushed_at` is reported but not used as a gate — many
mature libraries are stable, not abandoned.

Usage:
    uv run python -m src.check_eol                  # all classes, no API fetch
    uv run python -m src.check_eol --class AB       # only A+B
    uv run python -m src.check_eol --class AB --fetch-missing
    uv run python -m src.check_eol --limit 50 --fetch-missing
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from tqdm import tqdm

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VALUE_FILE = DATA_DIR / "value-data.csv"
TOP_REPOS_FILE = DATA_DIR / "github" / "search" / "top-repos.csv"
OUTPUT_FILE = DATA_DIR / "eol.csv"

FIELDS = ["repo", "value_class", "archived", "pushed_at", "days_since_push", "is_eol", "source"]

GITHUB_API = "https://api.github.com"


def load_value_repos(class_filter: str | None) -> list[dict]:
    """Read value-data.csv and return one row per (package, github_repo).

    `class_filter` is a string of class letters to keep, e.g. "AB" → A and B only.
    None keeps all classes.
    """
    rows: list[dict] = []
    with open(VALUE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if class_filter and r["value_class"] not in class_filter:
                continue
            if not r["github_repo"]:
                continue
            rows.append({
                "package": r["package"],
                "ecosystem": r["ecosystem"],
                "repo": r["github_repo"],
                "value_class": r["value_class"],
            })
    return rows


def load_top_repos_index() -> dict[str, dict]:
    """Load top-repos.csv keyed by lowercased repo slug."""
    idx: dict[str, dict] = {}
    with open(TOP_REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            idx[r["repo"].lower()] = r
    return idx


def fetch_repo_from_api(repo: str, token: str | None) -> dict | None:
    """Fetch a single repo from GitHub API. Returns None on 404 or network error."""
    url = f"{GITHUB_API}/repos/{repo}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        log.warning("network error for %s: %s", repo, e)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.warning("status %s for %s", resp.status_code, repo)
        return None
    return resp.json()


def days_since(ts: str) -> int | None:
    """Return whole days between `ts` (ISO 8601) and now (UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def build_eol(rows: list[dict], top_idx: dict[str, dict], fetch_missing: bool) -> list[dict]:
    """Annotate value rows with archived + pushed_at, optionally fetching missing repos."""
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN") or (
        os.environ.get("GITHUB_TOKENS", "").split(",")[0] if os.environ.get("GITHUB_TOKENS") else None
    )
    if fetch_missing and not token:
        log.warning("--fetch-missing set but no GITHUB_TOKEN/GITHUB_TOKENS in env; using anonymous (60/hr)")

    # Cache API fetches across duplicate repos in the value list
    fetched: dict[str, dict | None] = {}
    out: list[dict] = []

    for row in tqdm(rows, desc="checking eol", unit="pkg"):
        slug = row["repo"].lower()
        meta = top_idx.get(slug)
        source = "top-repos"

        if meta is None and fetch_missing:
            if slug not in fetched:
                fetched[slug] = fetch_repo_from_api(row["repo"], token)
            api = fetched[slug]
            if api is not None:
                meta = {
                    "archived": str(bool(api.get("archived"))),
                    "pushed_at": api.get("pushed_at", ""),
                }
                source = "api"

        if meta is None:
            archived = ""
            pushed_at = ""
            source = "missing"
        else:
            archived = meta.get("archived", "")
            pushed_at = meta.get("pushed_at", "")

        is_archived = archived == "True"
        out.append({
            "repo": row["repo"],
            "value_class": row["value_class"],
            "archived": archived,
            "pushed_at": pushed_at,
            "days_since_push": days_since(pushed_at) if pushed_at else "",
            "is_eol": is_archived,
            "source": source,
        })
    return out


def write_eol(rows: list[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def display_summary(rows: list[dict]) -> None:
    """Print a rich summary: counts by class + source + a sample of EOL repos."""
    by_class: dict[str, dict[str, int]] = {}
    for r in rows:
        c = r["value_class"]
        b = by_class.setdefault(c, {"total": 0, "eol": 0, "missing": 0, "stale_2y": 0})
        b["total"] += 1
        if r["is_eol"]:
            b["eol"] += 1
        if r["source"] == "missing":
            b["missing"] += 1
        d = r["days_since_push"]
        if isinstance(d, int) and d > 730:
            b["stale_2y"] += 1

    t = Table(title="EOL coverage by value class", header_style="dim")
    t.add_column("class", style="cyan")
    t.add_column("total", justify="right")
    t.add_column("archived", justify="right", style="red")
    t.add_column("stale >2y", justify="right", style="yellow")
    t.add_column("missing", justify="right", style="dim")

    totals = {"total": 0, "eol": 0, "missing": 0, "stale_2y": 0}
    for c in sorted(by_class):
        b = by_class[c]
        for k in totals:
            totals[k] += b[k]
        t.add_row(c, str(b["total"]), str(b["eol"]), str(b["stale_2y"]), str(b["missing"]))
    t.add_row(
        "[b]Total[/b]",
        f"[b]{totals['total']}[/b]",
        f"[b]{totals['eol']}[/b]",
        f"[b]{totals['stale_2y']}[/b]",
        f"[b]{totals['missing']}[/b]",
    )
    console.print(t)

    eol_rows = [r for r in rows if r["is_eol"]]
    if eol_rows:
        s = Table(title=f"Archived repos ({len(eol_rows)})", header_style="dim")
        s.add_column("repo", style="cyan")
        s.add_column("class")
        s.add_column("pushed_at", style="dim")
        for r in sorted(eol_rows, key=lambda x: (x["value_class"], x["repo"]))[:30]:
            s.add_row(r["repo"], r["value_class"], r["pushed_at"][:10])
        console.print(s)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--class", dest="class_filter", default=None,
                   help="class letters to keep, e.g. 'AB' (default: all)")
    p.add_argument("--limit", type=int, default=None, help="limit number of rows")
    p.add_argument("--fetch-missing", action="store_true",
                   help="fetch repos missing from top-repos.csv via GitHub API")
    args = p.parse_args()

    started = datetime.now()
    console.print(f"[bold]check_eol[/bold]  class={args.class_filter or 'all'}  "
                  f"limit={args.limit or '∞'}  fetch_missing={args.fetch_missing}  "
                  f"started={started:%Y-%m-%d %H:%M:%S}")

    rows = load_value_repos(args.class_filter)
    if args.limit:
        rows = rows[:args.limit]
    log.info("loaded %d rows from value-data.csv", len(rows))

    top_idx = load_top_repos_index()
    log.info("loaded %d repos from top-repos.csv", len(top_idx))

    out = build_eol(rows, top_idx, args.fetch_missing)
    write_eol(out)

    display_summary(out)
    console.print(f"\n[green]wrote[/green] {OUTPUT_FILE.relative_to(DATA_DIR.parent)}  "
                  f"({len(out)} rows, {(datetime.now() - started).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
