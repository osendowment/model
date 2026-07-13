"""
Fetch Repology package lists for debian_13 (current stable) and homebrew.

Repology is a cross-distro package-metadata aggregator. Its canonical
"project" name is the join key we need to cross-reference Debian sources
against Homebrew formulas. Single endpoint: /api/v1/projects/{cursor}/?inrepo=<repo>.

Run:
    uv run python -m src.sources.repology.fetch_repology_data                   # both repos
    uv run python -m src.sources.repology.fetch_repology_data --repo debian_13  # just Debian
    uv run python -m src.sources.repology.fetch_repology_data --refresh         # ignore the TTL
    uv run python -m src.sources.repology.fetch_repology_data --offline         # never touch the network

Output:
    data/sources/repology/packages.csv   project, repo, srcname, binname, visiblename,
                                 version, status, categories, licenses, fetched_at

Consumed by `src.sources.cpp.process_data` and `src.sources.cpp.check_eol` (both
live pipeline steps), so this runs as a `net=True` pipeline step. A full run is
≈ 3 min at Repology's ~1 rps fair-use limit — far too slow to repeat on every
pipeline run — so the whole file is TTL-gated: a warm re-run makes ZERO network
calls. Distro package sets move slowly; TTL_DAYS balances that against the file
going stale enough to misjoin the C/C++ pipeline.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from rich.console import Console
from rich.table import Table

from src.common.freshness import file_is_fresh

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

# Whole-file TTL. Distro package sets move slowly, but the cross-distro join key
# going 6+ weeks stale silently degrades the C/C++ pipeline, so refresh monthly.
# --refresh (propagated by the pipeline runner) overrides.
TTL_DAYS = 30

KEEP_FIELDS = [
    "project", "repo", "srcname", "binname", "visiblename",
    "version", "status", "categories", "licenses", "fetched_at",
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

def fetch_repo(repo: str, fetched_at: str) -> list[dict]:
    """Walk cursor-paginated projects for one Repology repo.

    Each page is /projects/{cursor}/?inrepo=<repo>. The cursor is inclusive,
    so we dedupe by project name across pages. Every row is stamped with
    `fetched_at` — the audit date of the run that produced it.
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
                    "fetched_at":  fetched_at,
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


# ── existing-file merge ───────────────────────────────────────────────────────

def load_existing(path: str) -> list[dict]:
    """Rows already on disk (empty when the file is absent)."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", action="append", default=None,
                   help=f"Repo to fetch (repeatable). Default: {DEFAULT_REPOS}")
    p.add_argument("--refresh", action="store_true",
                   help=f"Force refetch, ignoring the {TTL_DAYS}-day output TTL")
    p.add_argument("--offline", action="store_true",
                   help="Skip all network fetches (keep whatever is cached)")
    args = p.parse_args()
    repos = args.repo or DEFAULT_REPOS

    console.rule("[bold]repology — fetch_repology_data")
    console.print(f"  Started : [cyan]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/cyan]")
    console.print(f"  Repos   : [cyan]{', '.join(repos)}[/cyan]")
    console.print(f"  Output  : [cyan]{OUT_PATH}[/cyan]\n")

    # ── TTL gate: a warm run makes ZERO network calls ─────────────────────────
    # The full fetch is ~3 min at Repology's ~1 rps fair-use limit, so it must
    # not run on every pipeline invocation.
    if args.offline:
        if os.path.exists(OUT_PATH):
            console.print(f"  [dim]skipped (--offline) — using cached {OUT_PATH}[/dim]")
        else:
            console.print(f"  [yellow]--offline and {OUT_PATH} is missing — "
                          f"downstream cpp steps will have no Repology join key[/yellow]")
        return
    if not args.refresh and file_is_fresh(OUT_PATH, TTL_DAYS):
        age_days = (time.time() - os.path.getmtime(OUT_PATH)) / 86400
        console.print(f"  [dim]output fresh ({age_days:.1f}d < {TTL_DAYS}d) — "
                      f"skipping; --refresh to force[/dim]")
        return

    t0 = time.perf_counter()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    fetched: dict[str, list[dict]] = {}
    for repo in repos:
        fetched[repo] = fetch_repo(repo, fetched_at)

    # ── auditability guard: never let a failed/empty fetch truncate the file ──
    # A hard network failure already raises out of _get_json before we write.
    # This catches the quieter case: a 200 that yields zero rows (API shape
    # changed, repo renamed upstream). Writing that would silently blank the
    # cross-distro join key the C/C++ pipeline depends on.
    empty = [r for r, rows in fetched.items() if not rows]
    if empty:
        console.print(f"\n  [bold red]ABORT[/bold red]: fetched 0 rows for {', '.join(empty)} — "
                      f"refusing to overwrite {OUT_PATH} with a truncated file.")
        console.print("  [dim]The existing file is left untouched. Re-run with --refresh "
                      "once the upstream repo name / API shape is confirmed.[/dim]")
        sys.exit(1)

    # Rows for repos we did NOT fetch this run are carried over verbatim, so a
    # subset run (`--repo debian_13`) refreshes its own rows instead of wiping
    # the others out of the file.
    carried = [r for r in load_existing(OUT_PATH) if r.get("repo") not in fetched]
    all_rows = [r for rows in fetched.values() for r in rows] + carried

    all_rows.sort(key=lambda r: (r["project"], r["repo"], r["visiblename"]))
    atomic_write(OUT_PATH, all_rows, KEEP_FIELDS)

    console.print(f"\n  → wrote {len(all_rows):,} rows to [cyan]{OUT_PATH}[/cyan]")
    for repo, rows in fetched.items():
        console.print(f"     [dim]{repo}: {len(rows):,} rows (fetched {fetched_at})[/dim]")
    if carried:
        console.print(f"     [dim]carried over {len(carried):,} rows for repos not fetched this run[/dim]")
    console.print(f"  [bold green]Done in {time.perf_counter() - t0:.1f}s[/bold green]")


if __name__ == "__main__":
    main()
