"""
Fetch Repology package lists for debian_13 (current stable) and homebrew.

Repology is a cross-distro package-metadata aggregator. Its canonical
"project" name is the join key we need to cross-reference Debian sources
against Homebrew formulas. Single endpoint: /api/v1/projects/{cursor}/?inrepo=<repo>.

Run:
    uv run src/repology/fetch_repology_data.py                   # both repos
    uv run src/repology/fetch_repology_data.py --repo debian_13  # just Debian

Output:
    data/sources/repology/packages.csv   project, repo, srcname, binname, visiblename,
                                 version, status, categories, licenses

Respects Repology's ~1 rps fair-use limit. Full run ≈ 3 min.
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime

import requests
from rich.console import Console
from rich.table import Table

# ── config ────────────────────────────────────────────────────────────────────

OUT_PATH = "data/sources/repology/packages.csv"
API = "https://repology.org/api/v1/projects"
DEFAULT_REPOS = ["debian_13", "homebrew"]
USER_AGENT = "osendowment-model/1.0 (research; +https://endowment.dev)"
TIMEOUT = 60
REQUEST_DELAY = 1.2  # be polite — Repology asks for ≤1 rps
PAGE_SIZE = 200
MAX_RETRIES = 4
RETRY_BACKOFF = [5, 15, 30, 60]

KEEP_FIELDS = [
    "project", "repo", "srcname", "binname", "visiblename",
    "version", "status", "categories", "licenses",
]

console = Console()


def atomic_write(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _get_json(url: str) -> dict | list:
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            if r.status_code in (429, 502, 503, 504):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                console.print(f"  [yellow]  {r.status_code} — waiting {wait}s[/yellow]")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            last = e
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            console.print(f"  [yellow]  {type(e).__name__} — waiting {wait}s[/yellow]")
            time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES} attempts: {last}")


# ── paginated fetch ───────────────────────────────────────────────────────────

def fetch_repo(repo: str) -> list[dict]:
    """Walk cursor-paginated projects for one Repology repo.

    Each page is /projects/{cursor}/?inrepo=<repo>. The cursor is inclusive,
    so we dedupe by project name across pages.
    """
    console.rule(f"[bold cyan]Repology — fetch_repo({repo})")
    rows: list[dict] = []
    seen: set[str] = set()
    cursor: str | None = None
    page = 0

    while True:
        url = f"{API}/{cursor}/?inrepo={repo}" if cursor else f"{API}/?inrepo={repo}"
        t0 = time.perf_counter()
        data = _get_json(url)
        elapsed = time.perf_counter() - t0
        page += 1
        if not isinstance(data, dict) or not data:
            console.print(f"  page {page}: empty — done.")
            break

        new_projects = [p for p in sorted(data) if p not in seen]
        for project in new_projects:
            seen.add(project)
            for pkg in data[project]:
                # Filter to entries from the target repo only. The /projects/
                # endpoint returns ALL repos for each project; we only want
                # this repo's rows (we'll fetch homebrew separately in its
                # own pass, and join on project name).
                if pkg.get("repo") != repo:
                    continue
                rows.append({
                    "project":     project,
                    "repo":        pkg.get("repo", ""),
                    "srcname":     pkg.get("srcname", "") or "",
                    "binname":     pkg.get("binname", "") or "",
                    "visiblename": pkg.get("visiblename", "") or "",
                    "version":     pkg.get("version", "") or "",
                    "status":      pkg.get("status", "") or "",
                    "categories":  "|".join(pkg.get("categories") or []),
                    "licenses":    "|".join(pkg.get("licenses") or []),
                })

        console.print(
            f"  page {page:>3}  cursor=[dim]{cursor or '<start>'}[/dim]  "
            f"got {len(data)} projects, {len(new_projects)} new  "
            f"({elapsed:.1f}s) — running total: {len(seen):,} projects, {len(rows):,} rows"
        )

        # Pagination: last project name in the sorted response becomes next cursor.
        # If page was < PAGE_SIZE or no new projects, we're done.
        if len(data) < PAGE_SIZE or not new_projects:
            break
        cursor = max(data)
        time.sleep(REQUEST_DELAY)

    console.rule()
    tbl = Table(show_header=False, box=None, padding=(0, 2))
    tbl.add_column(style="dim"); tbl.add_column(justify="right")
    tbl.add_row(f"Projects ({repo})",  f"{len(seen):,}")
    tbl.add_row(f"Package rows ({repo})", f"{len(rows):,}")
    tbl.add_row("Pages fetched",       f"{page:,}")
    console.print(tbl)
    return rows


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", action="append", default=None,
                   help=f"Repo to fetch (repeatable). Default: {DEFAULT_REPOS}")
    args = p.parse_args()
    repos = args.repo or DEFAULT_REPOS

    console.rule("[bold]repology — fetch_repology_data")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Repos   : [cyan]{', '.join(repos)}[/cyan]\n")

    all_rows: list[dict] = []
    t0 = time.perf_counter()
    for repo in repos:
        all_rows.extend(fetch_repo(repo))

    all_rows.sort(key=lambda r: (r["project"], r["repo"], r["visiblename"]))
    atomic_write(OUT_PATH, all_rows, KEEP_FIELDS)
    console.print(f"\n  → wrote {len(all_rows):,} rows to [cyan]{OUT_PATH}[/cyan]")
    console.print(f"  [bold green]Done in {time.perf_counter() - t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
