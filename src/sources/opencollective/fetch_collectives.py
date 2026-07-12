"""Download the Open Collective ↔ GitHub index.

Paginates every OC `COLLECTIVE` account and keeps those that publish a GitHub
link (`repositoryUrl` or a `GITHUB` social link), normalised to a GitHub owner
[+ repo], OR a GitLab project link (normalised clone URL). This is the **reverse
map**: OC itself declares which repo/org a collective funds, so we can attribute
OC budgets to risk-scope repos that never declared a `.github/FUNDING.yml` (e.g.
socketio) — no guessing.

Consumed by `fetch_budgets` (slug discovery) and `build_funding` (per-repo OC
slug resolution): GitHub links via `load_index()` (owner/repo + owner maps),
GitLab links via `load_url_index()` (normalised-URL map).

Writes data/sources/opencollective/collectives.csv:
    slug, name, urls, github_owner, github_repo, github_url, repo_url, fetched_at
`urls` is the space-separated list of EVERY URL the collective declares
(`repositoryUrl`, social links, `website`) — the durable raw record, so a
future pass can re-derive the repo host (github / gitlab / codeberg /
self-hosted / …) offline without re-hitting the OC API. `github_*` / `repo_url`
are the links parsed out now for the current joins (`github_repo` is
`owner/repo` for repo-level GitHub links, empty for org-only; `repo_url` is a
normalized GitLab repo URL). Every collective that declares at least one URL is
stored — filtering by host happens on read, never at fetch.

Usage:
    uv run python -m src.sources.opencollective.fetch_collectives
"""
from __future__ import annotations

import csv
import datetime
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

from src.common.freshness import FUNDING_TTL_DAYS, file_is_fresh
from src.sources.floss_fund.directory import normalize_repo_url
from src.sources.gitlab.gitlab_client import is_gitlab_host

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "opencollective" / "collectives.csv"
API_URL = "https://api.opencollective.com/graphql/v2"
USER_AGENT = "Mozilla/5.0 (research; endowment.dev funding model)"
FIELDS = ["slug", "name", "urls", "github_owner", "github_repo", "github_url",
          "repo_url", "fetched_at"]
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


def _is_search_url(url: str | None) -> bool:
    """A github.com **search-results** URL (e.g. ``github.com/apache?q=grails-``)
    — a filtered listing, NOT a profile/repo claim. Treating it as an org link
    over-attributes the collective to the WHOLE org (the "Friends of Apache
    Grails" collective linked its Grails-scoped search here and leaked onto every
    apache/* repo). Reject it so neither the owner nor a repo is extracted."""
    u = (url or "").lower()
    return "github.com/" in u and ("?q=" in u or "&q=" in u)


def _gh_parse(url: str | None) -> tuple[str, str]:
    """(owner, repo) from a GitHub URL; repo is '' for an org-only URL. ('','') if not GitHub."""
    if _is_search_url(url):
        return "", ""
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


# GitLab top-level paths that are not a `group/project` (so not a repo).
_GL_RESERVED = {"users", "groups", "explore", "help", "dashboard", "projects",
                "admin", "search", "public", "-"}


def _gitlab_repo(url: str | None) -> str:
    """A normalized GitLab repo URL (`gitlab.com/group/project`) or '' if `url`
    does not name a GitLab project.

    Accepts gitlab.com and self-hosted instances (`is_gitlab_host`); requires a
    `group/project` path (subgroups allowed); strips any GitLab web suffix
    (`/-/tree/...`). Normalized via `normalize_repo_url` so it joins to a pipeline
    GitLab repo by the same key `build_funding` derives from `entry.git_url`."""
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u if "://" in u else "https://" + u)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if not host or not is_gitlab_host(host):
        return ""
    path = parsed.path.split("/-/", 1)[0].strip("/")
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() in _GL_RESERVED:
        return ""
    return normalize_repo_url(f"https://{host}/{path}")


def _gitlab_link(node: dict) -> str:
    """Best normalized GitLab repo URL for a node, or '' — the GitLab twin of
    `_github_link`, scanning the same candidate links (repositoryUrl, social
    links, website)."""
    socials = node.get("socialLinks") or []
    candidates = [node.get("repositoryUrl")]
    candidates += [s.get("url") for s in socials]
    candidates.append(node.get("website"))
    for url in candidates:
        norm = _gitlab_repo(url)
        if norm:
            return norm
    return ""


def _all_urls(node: dict) -> list[str]:
    """Every distinct URL a collective declares — `repositoryUrl`, then its social
    links, then `website` — in declaration order, deduped.

    This is the durable raw record: storing all of them means a future pass can
    detect a repo on any host (github / gitlab / codeberg / self-hosted / …)
    without re-fetching from the OC API, instead of us discarding a link at fetch
    just because today's parser doesn't recognise its host.
    """
    socials = node.get("socialLinks") or []
    raw = [node.get("repositoryUrl")]
    raw += [s.get("url") for s in socials]
    raw.append(node.get("website"))
    seen: set[str] = set()
    out: list[str] = []
    for u in raw:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_all(headers: dict, limit: int | None = None) -> list[dict]:
    """Paginate collectives → one row per collective that declares ≥1 URL.

    Stores every declared URL in `urls` (the durable raw record) plus the
    GitHub/GitLab repo link parsed out for the current joins. `limit` caps the
    number of collectives scanned (testing)."""
    rows: list[dict] = []
    offset, total = 0, None
    with Progress(TextColumn("[bold]OC collectives"), BarColumn(),
                  TaskProgressColumn(), console=console) as prog:
        task = prog.add_task("", total=None)
        while True:
            page = min(PAGE, limit - offset) if limit else PAGE
            resp = requests.post(API_URL, headers=headers, timeout=60, json={
                "query": QUERY, "variables": {"limit": page, "offset": offset}})
            resp.raise_for_status()
            acc = resp.json()["data"]["accounts"]
            if total is None:
                total = acc["totalCount"] if limit is None else min(acc["totalCount"], limit)
                prog.update(task, total=total)
            nodes = acc["nodes"]
            if not nodes:
                break
            for n in nodes:
                urls = _all_urls(n)
                if not urls:
                    continue  # nothing to store / re-derive later
                owner, repo, url = _github_link(n)
                # GitHub wins; only look for a GitLab repo link when there's no
                # GitHub one, so GitHub attribution is byte-for-byte unchanged.
                repo_url = "" if owner else _gitlab_link(n)
                rows.append({
                    "slug": n["slug"], "name": (n.get("name") or "").strip(),
                    "urls": " ".join(urls),
                    "github_owner": owner,
                    "github_repo": f"{owner}/{repo}" if repo else "",
                    "github_url": url,
                    "repo_url": repo_url})
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
            # Skip search-results links (github.com/<org>?q=…): a Grails-scoped
            # search must not seed an org-level claim on the whole apache org.
            # Guards existing rows written before _gh_parse learned to reject them.
            if _is_search_url(r.get("github_url")):
                continue
            repo = (r.get("github_repo") or "").strip().lower()
            owner = (r.get("github_owner") or "").strip().lower()
            if repo:
                by_repo.setdefault(repo, slug)
            elif owner:
                by_org.setdefault(owner, slug)
    return by_repo, by_org


def load_url_index(path: Path = OUTPUT_FILE) -> dict[str, str]:
    """Return ``{normalized_repo_url: slug}`` for collectives that link a GitLab
    project instead of GitHub (the `repo_url` column).

    The GitLab twin of `load_index`'s ``by_repo``: build_funding joins it by
    ``normalize_repo_url(entry.git_url)`` — the same key the FLOSS by-URL map
    uses. First slug wins on collision; a legacy file with no `repo_url` column
    yields an empty map (so the join is simply inert until the next refresh)."""
    by_url: dict[str, str] = {}
    if not path.exists():
        return by_url
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("slug") or "").strip()
            url = (r.get("repo_url") or "").strip().lower()
            if slug and url:
                by_url.setdefault(url, slug)
    return by_url


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--force", action="store_true",
                   help="Re-download even if collectives.csv already exists.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap collectives scanned (testing). Writes a "
                        "collectives.sample.csv so the real file is never truncated.")
    args = p.parse_args()

    # A --limit run is a partial fetch — write a sample file so a test never
    # clobbers the full collectives.csv, and skip the TTL freshness gate.
    out_file = OUTPUT_FILE if not args.limit else OUTPUT_FILE.with_name("collectives.sample.csv")

    # TTL gate: the full OC index (~38k accounts) rarely changes the matches we
    # care about, so within the funding TTL window a re-run is a no-op; it
    # refreshes only once the file is older than FUNDING_TTL_DAYS.
    if not args.force and not args.limit and file_is_fresh(OUTPUT_FILE, FUNDING_TTL_DAYS):
        console.print(f"[dim]{OUTPUT_FILE.relative_to(DATA_DIR.parent)} fresh "
                      f"(< {FUNDING_TTL_DAYS}d) — skipping download (pass --force to refresh).[/dim]")
        return

    load_dotenv()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    token = (os.environ.get("OPENCOLLECTIVE_PERSONAL_TOKEN")
             or os.environ.get("OC_PERSONAL_TOKEN"))
    if token:
        headers["Personal-Token"] = token
        console.print("[dim]Using OpenCollective Personal-Token (higher rate limit).[/dim]")

    rows = fetch_all(headers, limit=args.limit)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for r in rows:
        r["fetched_at"] = now

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["slug"]):
            w.writerow(r)
    n_gh_repo = sum(1 for r in rows if r["github_repo"])
    n_gh_org = sum(1 for r in rows if r["github_owner"] and not r["github_repo"])
    n_gitlab = sum(1 for r in rows if r.get("repo_url"))
    n_other = len(rows) - n_gh_repo - n_gh_org - n_gitlab
    console.print(f"[green]wrote {len(rows):,} collectives with URLs "
                  f"({n_gh_repo:,} GitHub repo, {n_gh_org:,} GitHub org, "
                  f"{n_gitlab:,} GitLab, {n_other:,} other/unparsed) → {out_file}[/green]")


if __name__ == "__main__":
    main()
