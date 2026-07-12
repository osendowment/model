"""Resolve renamed GitHub repos declared by Open Collective collectives.

A collective's GitHub link is free text on the OC side: when the linked repo
is later renamed or moved to another org (byron/gitoxide →
GitoxideLabs/gitoxide), the slug-keyed reverse map in collectives.csv silently
stops matching the pipeline repo, and build_funding drops the collective's
budget signal.

This resolver follows GitHub's rename redirects for CANDIDATE rows only — a
declared repo that no longer matches any pipeline repo slug exactly, but whose
bare repo name or owner matches one (the realistic rename shapes: repo moved
to a new owner, or renamed in place). Each candidate costs one GitHub API
call; the result — including definitive misses (404/410) — is persisted with
a `checked_at` date so re-runs stay cheap. Transient errors are recorded as
status=error and retried on the next run regardless of TTL.

Writes data/sources/opencollective/renames.csv:
    declared_repo, resolved_repo, repo_id, status, checked_at
`resolved_repo`/`repo_id` are filled only when GitHub redirects the declared
slug to a DIFFERENT repo (status=renamed). status=same means the slug still
resolves to itself; status=missing means the declared repo is gone.

Consumed by fetch_collectives.load_index(), which overlays
{resolved_repo → collective slug} onto the reverse map.

Usage:
    uv run python -m src.sources.opencollective.resolve_renames [--limit N] [--force]
"""
from __future__ import annotations

import csv
import datetime
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from src.common.freshness import FUNDING_TTL_DAYS
from src.sources.github.github_client import load_tokens
from src.sources.opencollective.fetch_collectives import OUTPUT_FILE as COLLECTIVES_FILE

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
GITHUB_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "sources" / "opencollective" / "renames.csv"
FIELDS = ["declared_repo", "resolved_repo", "repo_id", "status", "checked_at"]
API_URL = "https://api.github.com/repos/"


def find_candidates(declared: set[str], pipeline: set[str]) -> list[str]:
    """Declared repo slugs worth one resolution call each.

    A candidate is NOT an exact pipeline match (nothing to fix) but shares its
    bare repo name or its owner with a pipeline repo — the two shapes a rename
    realistically takes. Everything else points outside the pipeline scope, so
    resolving it could never change an attribution.
    """
    names = {slug.split("/", 1)[1] for slug in pipeline if "/" in slug}
    owners = {slug.split("/", 1)[0] for slug in pipeline if "/" in slug}
    out = []
    for d in sorted(declared):
        if d in pipeline or "/" not in d:
            continue
        owner, _, name = d.partition("/")
        if name in names or owner in owners:
            out.append(d)
    return out


def resolve(declared: str, headers: dict) -> dict:
    """One GitHub API call: where does `declared` live now?"""
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    row = {"declared_repo": declared, "resolved_repo": "", "repo_id": "",
           "status": "error", "checked_at": now}
    try:
        resp = requests.get(API_URL + declared, headers=headers, timeout=30)
        if resp.status_code in (404, 410):
            row["status"] = "missing"
            return row
        resp.raise_for_status()
        data = resp.json()
        full = (data.get("full_name") or "").lower()
        row["repo_id"] = f"gh/{data['id']}"
        if full == declared:
            row["status"] = "same"
        else:
            row.update(resolved_repo=full, status="renamed")
    except requests.RequestException as exc:
        console.print(f"  [red]{declared}: {exc}[/red]")
    return row


def _stale(row: dict, cutoff: datetime.datetime) -> bool:
    if (row.get("status") or "") == "error":
        return True  # transient failure — retry regardless of TTL
    try:
        return datetime.datetime.fromisoformat(row["checked_at"]) < cutoff
    except (KeyError, TypeError, ValueError):
        return True


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, help="cap on resolution calls (testing)")
    p.add_argument("--force", action="store_true", help="ignore TTL — recheck everything")
    args = p.parse_args()

    load_dotenv()
    console.print("[bold]opencollective/resolve_renames[/bold]  "
                  f"TTL=[dim]{FUNDING_TTL_DAYS}d[/dim]  force=[dim]{args.force}[/dim]  "
                  f"limit=[dim]{args.limit}[/dim]")

    declared = set()
    with open(COLLECTIVES_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = (r.get("github_repo") or "").strip().lower()
            if repo:
                declared.add(repo)
    # Canonical CURRENT slugs: repos.csv keeps old slugs as cache keys after a
    # rename (byron/gitoxide row → full_name GitoxideLabs/gitoxide), so the
    # `repo` column would wrongly disqualify a renamed declared slug from
    # candidacy — `full_name` is what the repo is called now.
    pipeline = set()
    with open(GITHUB_REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = ((r.get("full_name") or "").strip()
                    or (r.get("repo") or "").strip()).lower()
            if slug:
                pipeline.add(slug)

    candidates = find_candidates(declared, pipeline)

    cached: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            cached = {r["declared_repo"]: r for r in csv.DictReader(f)}

    cutoff = (datetime.datetime.now(datetime.UTC)
              - datetime.timedelta(days=FUNDING_TTL_DAYS))
    stale = [c for c in candidates
             if args.force or c not in cached or _stale(cached[c], cutoff)]
    to_fetch = stale[:args.limit] if args.limit else stale
    console.print(f"  declared repo-level: {len(declared):,} · candidates: "
                  f"{len(candidates):,} · cached fresh: "
                  f"{len(candidates) - len(stale):,} · to resolve: {len(to_fetch):,}")

    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "endowment.dev funding model (research)"}
    tokens = load_tokens()
    if tokens:
        headers["Authorization"] = f"Bearer {tokens[0]}"

    counts: dict[str, int] = {}
    with Progress(TextColumn("[bold]resolve"), BarColumn(), TaskProgressColumn(),
                  console=console) as prog:
        task = prog.add_task("", total=len(to_fetch))
        for declared_repo in to_fetch:
            row = resolve(declared_repo, headers)
            cached[declared_repo] = row
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            prog.advance(task)
            time.sleep(0.05)

    # Keep only rows that are still candidates — collectives or pipeline scope
    # may have shifted since an earlier run; stale non-candidates are noise.
    rows = [cached[c] for c in candidates if c in cached]
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["declared_repo"]))

    renamed = sum(1 for r in rows if r["status"] == "renamed")
    console.print(f"[green]wrote {len(rows):,} rows ({renamed:,} renamed) → "
                  f"{OUTPUT_FILE.relative_to(DATA_DIR.parent)}[/green]"
                  + (f"  [dim]this run: {counts}[/dim]" if counts else ""))


if __name__ == "__main__":
    main()
