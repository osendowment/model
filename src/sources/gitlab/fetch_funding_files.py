#!/usr/bin/env python3
"""Check each GitLab top repo for in-repo funding declarations.

GitLab has no FUNDING.yml rendering or Sponsors product, but projects can
still DECLARE funding channels the same way GitHub projects do — by shipping
a funding file in the repository. This fetcher probes each GitLab top repo
(valid class-A on any GitLab instance, archived included) for the five known
file conventions on its default branch, over the public raw endpoint
(`https://{host}/{path}/-/raw/{branch}/{file}` — no token needed for public
projects):

    FUNDING.yml / .github/FUNDING.yml / .gitlab/FUNDING.yml
        — the GitHub FUNDING.yml convention, often kept by mirrors or
          multi-host projects. Parsed for its platform keys.
    funding.json
        — a FLOSS Fund manifest committed to the repo.
    .well-known/funding-manifest-urls
        — the floss.fund pointer file (manifest registered elsewhere).

Writes data/sources/gitlab/funding-files.csv (one row per repo):
    repo, repo_id, project,
    has_funding_yml         — any FUNDING.yml variant exists ("True"/"False")
    funding_yml_path        — which variant hit (first match), else ""
    has_funding_links       — the FUNDING.yml parsed to ≥1 platform key
    funding_link_platforms  — canonical platform keys found (comma-joined)
    has_funding_json_file   — funding.json or the .well-known pointer exists
    status                  — ok | error (any probe network-failed → error,
                              so "False" never masks an unreachable host)
    fetched_at              — UTC ISO 8601

`build_funding` joins this by repo_id as the GitLab twin of
github/funding-yml.csv: the flags feed `intent` and the platform list feeds
the unmeasured-channel score cap, identically to the GitHub columns.

TTL: rows with a positive signal keep the full funding TTL; empty results
recheck on the shorter window (same policy as the GitHub funding fetchers).

Usage:
    uv run python -m src.sources.gitlab.fetch_funding_files
    uv run python -m src.sources.gitlab.fetch_funding_files --limit 5
    uv run python -m src.sources.gitlab.fetch_funding_files --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import logging
import re
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.table import Table

from src.common.freshness import funding_ttl_for, row_is_fresh
from src.common.funding_platforms import FUNDING_PLATFORMS
from src.common.repos import load_top_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
GITLAB_REPOS_FILE = DATA_DIR / "sources" / "gitlab" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "sources" / "gitlab" / "funding-files.csv"

FIELDS = ["repo", "repo_id", "project",
          "has_funding_yml", "funding_yml_path",
          "has_funding_links", "funding_link_platforms",
          "has_funding_json_file", "status", "fetched_at"]

FUNDING_YML_PATHS = ["FUNDING.yml", ".github/FUNDING.yml", ".gitlab/FUNDING.yml"]
MANIFEST_PATHS = ["funding.json", ".well-known/funding-manifest-urls"]

CONCURRENCY = 10
TIMEOUT_S = 15

_PLATFORM_SET = set(FUNDING_PLATFORMS)
# A FUNDING.yml top-level key: `platform: value` (value non-empty). Lists
# (`github: [a, b]`) and single handles both match; a bare `platform:` with
# nothing after it declares nothing.
_YML_KEY_RE = re.compile(r"^([a-z_]+)\s*:\s*(\S.*)$")


def parse_funding_yml_platforms(text: str) -> list[str]:
    """Canonical platform keys declared in a FUNDING.yml body, sorted.

    Minimal line parser (no YAML dependency): a top-level `platform: value`
    line whose key is a known FUNDING_PLATFORMS entry counts; a key with a
    list on following lines (`custom:\\n  - https://…`) counts when at least
    one `- item` line follows before the next top-level key.
    """
    found: set[str] = set()
    pending: str | None = None  # key seen with no inline value (block list?)
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith((" ", "\t", "-")):
            if pending and line.lstrip().startswith("-"):
                found.add(pending)
                pending = None
            continue
        pending = None
        m = _YML_KEY_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            if key in _PLATFORM_SET and value and value not in ("[]", "~", "null"):
                found.add(key)
            continue
        bare = line.rstrip(":").strip()
        if line.endswith(":") and bare in _PLATFORM_SET:
            pending = bare
    return sorted(found)


def load_targets() -> list[dict]:
    """GitLab top repos joined with gitlab/repos.csv for project + branch."""
    branch_by_id: dict[str, dict] = {}
    if GITLAB_REPOS_FILE.exists():
        with open(GITLAB_REPOS_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rid = (r.get("repo_id") or "").strip()
                if rid:
                    branch_by_id[rid] = r
    targets = []
    for e in load_top_repos():
        rid = str(e.repo_id)
        if not rid.startswith("gl/"):
            continue
        g = branch_by_id.get(rid, {})
        project = (g.get("project") or "").strip()
        if not project:
            log.warning("no gitlab/repos.csv row for %s (%s) — skipped", e.repo, rid)
            continue
        targets.append({"repo": e.repo, "repo_id": rid, "project": project,
                        "branch": (g.get("default_branch") or "").strip() or "master"})
    return targets


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[int, str]:
    """(status_code, body) — body only for a 200 text response."""
    async with session.get(url, allow_redirects=True) as resp:
        if resp.status == 200 and "text/html" not in resp.headers.get("Content-Type", ""):
            return resp.status, await resp.text()
        return resp.status, ""


async def check_repo(session: aiohttp.ClientSession, sem: asyncio.Semaphore,
                     target: dict) -> dict:
    """Probe one repo's funding files; a network error on ANY probe → status
    error (a "False" must mean checked-and-absent, never unreachable)."""
    base = f"https://{target['project']}/-/raw/{target['branch']}"
    yml_path, platforms = "", []
    manifest = False
    status = "ok"
    async with sem:
        try:
            for path in FUNDING_YML_PATHS:
                code, body = await _get(session, f"{base}/{path}")
                if code == 200 and body.strip():
                    yml_path = path
                    platforms = parse_funding_yml_platforms(body)
                    break
            for path in MANIFEST_PATHS:
                code, body = await _get(session, f"{base}/{path}")
                if code == 200 and body.strip():
                    manifest = True
                    break
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("funding-file probe failed for %s: %s", target["project"], exc)
            status = "error"
    return {
        "repo": target["repo"],
        "repo_id": target["repo_id"],
        "project": target["project"],
        "has_funding_yml": "True" if yml_path else "False",
        "funding_yml_path": yml_path,
        "has_funding_links": "True" if platforms else "False",
        "funding_link_platforms": ",".join(platforms),
        "has_funding_json_file": "True" if manifest else "False",
        "status": status,
        "fetched_at": datetime.datetime.now(datetime.UTC)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _has_signal(row: dict) -> bool:
    return "True" in (row.get("has_funding_yml", ""),
                      row.get("has_funding_json_file", ""))


def load_existing(path: Path = OUTPUT_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["repo_id"]: r for r in csv.DictReader(f) if (r.get("repo_id") or "").strip()}


async def fetch_all(targets: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return list(await asyncio.gather(
            *(check_repo(session, sem, t) for t in targets)))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None, metavar="N")
    p.add_argument("--force", action="store_true", help="ignore the TTL and recheck all")
    args = p.parse_args()

    targets = load_targets()
    existing = load_existing()
    if not args.force:
        fresh = [t for t in targets
                 if (row := existing.get(t["repo_id"]))
                 and row_is_fresh(row, funding_ttl_for(_has_signal(row)),
                                  status_key="status")]
        targets = [t for t in targets if t["repo_id"] not in {f["repo_id"] for f in fresh}]
        if fresh:
            console.print(f"[dim]Skipping {len(fresh)} fresh row(s). --force to recheck.[/dim]")
    if args.limit:
        targets = targets[:args.limit]

    console.print(f"[bold]Probing funding files for {len(targets)} GitLab repo(s)...[/bold]\n")
    rows = asyncio.run(fetch_all(targets)) if targets else []

    merged = load_existing()
    for r in rows:
        merged[r["repo_id"]] = r
    out = sorted(merged.values(), key=lambda r: r["repo"])
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    table = Table(title="[bold]GitLab funding files[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Signal", style="bold")
    table.add_column("Repos", justify="right")
    for label, col in (("FUNDING.yml present", "has_funding_yml"),
                       ("… with parsed platforms", "has_funding_links"),
                       ("funding.json / manifest pointer", "has_funding_json_file"),
                       ("probe errors", "status")):
        n = (sum(1 for r in out if r.get(col) == "error") if col == "status"
             else sum(1 for r in out if r.get(col) == "True"))
        table.add_row(label, str(n))
    console.print(table)
    console.print(f"\n[dim]{len(out)} repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
