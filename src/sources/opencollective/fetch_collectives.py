"""Download the Open Collective ↔ GitHub index.

Paginates every OC `COLLECTIVE` account and keeps those that publish a GitHub
link (`repositoryUrl` or a `GITHUB` social link), normalised to a GitHub owner
[+ repo]. This is the **reverse map**: OC itself declares which repo/org a
collective funds, so we can attribute OC budgets to risk-scope repos that never
declared a `.github/FUNDING.yml` (e.g. socketio) — no guessing.

Consumed by `fetch_budgets` (slug discovery) and `build_funding` (per-repo OC
slug resolution) via `load_index()`.

Writes data/sources/opencollective/collectives.csv:
    slug, name, github_owner, github_repo, github_url, fetched_at
(`github_repo` is `owner/repo` for repo-level links, empty for org-only links.)

Usage:
    uv run python -m src.sources.opencollective.fetch_collectives
"""
from __future__ import annotations

import csv
import datetime
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "opencollective" / "collectives.csv"
API_URL = "https://api.opencollective.com/graphql/v2"
USER_AGENT = "Mozilla/5.0 (research; endowment.dev funding model)"
FIELDS = ["slug", "name", "github_owner", "github_repo", "github_url", "fetched_at"]
PAGE = 1000

QUERY = """
query($limit:Int!,$offset:Int!){
  accounts(type: COLLECTIVE, limit:$limit, offset:$offset){
    totalCount
    nodes { slug name repositoryUrl website socialLinks { type url } }
  }
}"""

# GitHub paths that are not a user/org (skip these "owners").
_RESERVED = {"sponsors", "orgs", "apps", "marketplace", "about", "topics", "collections"}
_GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?", re.IGNORECASE)


def _gh_parse(url: str | None) -> tuple[str, str]:
    """(owner, repo) from a GitHub URL; repo is '' for an org-only URL. ('','') if not GitHub."""
    m = _GH_RE.search((url or "").strip())
    if not m:
        return "", ""
    owner = m.group(1).lower()
    if owner in _RESERVED:
        return "", ""
    repo = re.sub(r"\.git$", "", (m.group(2) or "").lower())
    return owner, repo


def _github_link(node: dict) -> tuple[str, str, str]:
    """Best GitHub (owner, repo, url) for a node.

    Prefers the explicit `repositoryUrl`, then a `GITHUB`-typed social link, then
    any other social link or the `website` that points at github.com — some
    collectives file their repo under a `WEBSITE`-typed social link or the
    `website` field rather than `GITHUB` (e.g. `debug` → debug-js/debug raised
    $13.9k, `rxjs` → ReactiveX/rxjs). `_gh_parse` keeps only github.com URLs, so
    a non-GitHub website here is skipped, not guessed at.
    """
    socials = node.get("socialLinks") or []
    candidates = [node.get("repositoryUrl")]
    candidates += [s.get("url") for s in socials if s.get("type") == "GITHUB"]
    candidates += [s.get("url") for s in socials if s.get("type") != "GITHUB"]
    candidates.append(node.get("website"))
    for url in candidates:
        owner, repo = _gh_parse(url)
        if owner:
            return owner, repo, (url or "").strip()
    return "", "", ""


def fetch_all(headers: dict) -> list[dict]:
    """Paginate all collectives, returning rows for the GitHub-linked ones."""
    rows: list[dict] = []
    offset, total = 0, None
    with Progress(TextColumn("[bold]OC collectives"), BarColumn(),
                  TaskProgressColumn(), console=console) as prog:
        task = prog.add_task("", total=None)
        while True:
            resp = requests.post(API_URL, headers=headers, timeout=60, json={
                "query": QUERY, "variables": {"limit": PAGE, "offset": offset}})
            resp.raise_for_status()
            acc = resp.json()["data"]["accounts"]
            if total is None:
                total = acc["totalCount"]
                prog.update(task, total=total)
            nodes = acc["nodes"]
            if not nodes:
                break
            for n in nodes:
                owner, repo, url = _github_link(n)
                if owner:
                    rows.append({
                        "slug": n["slug"], "name": (n.get("name") or "").strip(),
                        "github_owner": owner,
                        "github_repo": f"{owner}/{repo}" if repo else "",
                        "github_url": url})
            offset += len(nodes)
            prog.update(task, completed=min(offset, total))
            if offset >= total:
                break
    return rows


def load_index(path: Path = OUTPUT_FILE) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``({owner/repo: slug}, {owner: slug})`` from collectives.csv.

    The two maps are **kept distinct** — a repo-level collective (its OC link
    names a specific `owner/repo`) only seeds ``by_repo``; an org-only collective
    (link names just the `owner`) only seeds ``by_org``. The attribution differs:
    a repo-level budget goes fully to that repo, an org-level budget is split
    across the org's top repos (see build_funding). First slug wins on collision.
    """
    by_repo: dict[str, str] = {}
    by_org: dict[str, str] = {}
    if not path.exists():
        return by_repo, by_org
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("slug") or "").strip()
            if not slug:
                continue
            repo = (r.get("github_repo") or "").strip().lower()
            owner = (r.get("github_owner") or "").strip().lower()
            if repo:
                by_repo.setdefault(repo, slug)
            elif owner:
                by_org.setdefault(owner, slug)
    return by_repo, by_org


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--force", action="store_true",
                   help="Re-download even if collectives.csv already exists.")
    args = p.parse_args()

    # Fetch-once: the full OC index (~38k accounts) rarely changes the matches we
    # care about, so in the pipeline it only runs when the file is missing.
    if OUTPUT_FILE.exists() and not args.force:
        console.print(f"[dim]{OUTPUT_FILE.relative_to(DATA_DIR.parent)} already present "
                      f"— skipping download (pass --force to refresh).[/dim]")
        return

    load_dotenv()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    token = (os.environ.get("OPENCOLLECTIVE_PERSONAL_TOKEN")
             or os.environ.get("OC_PERSONAL_TOKEN"))
    if token:
        headers["Personal-Token"] = token
        console.print("[dim]Using OpenCollective Personal-Token (higher rate limit).[/dim]")

    rows = fetch_all(headers)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        r["fetched_at"] = now

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["slug"]):
            w.writerow(r)
    n_repo = sum(1 for r in rows if r["github_repo"])
    console.print(f"[green]wrote {len(rows):,} GitHub-linked collectives "
                  f"({n_repo:,} repo-level, {len(rows) - n_repo:,} org-only) → {OUTPUT_FILE}[/green]")


if __name__ == "__main__":
    main()
