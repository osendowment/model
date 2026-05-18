"""Fetch funding signals (GitHub Sponsors, FUNDING.yml, funding.json) per risk-scope repo.

Writes data/funding-data.csv with one row per repo. TTL-controlled refresh
so re-runs only fetch repos missing from the file or older than the TTL
(see CONCENTRATION_TTL_DAYS pattern in batch_runner).

Three signals per repo, fetched in parallel:

1. **GitHub Sponsors count** — via GraphQL `sponsorshipsAsMaintainer.totalCount`.
   We query the repo owner login (User or Organization) and *also* every
   GitHub username listed under the `github:` key of the project's
   `.github/FUNDING.yml`. Counts are summed. Public sponsors only — private
   sponsorships are invisible to non-maintainers.
2. **FUNDING.yml** — `.github/FUNDING.yml` via REST Contents API. Existence
   plus parsed list of platforms (github, patreon, opencollective, …).
3. **funding.json** — root or `.well-known/funding.json` via raw URL. The
   FLOSS/fund manifest format.

`funding_class` (A/B/C/D):
- A: 0 sources AND 0 sponsors
- B: 1 source OR ≤5 sponsors
- C: 2–3 sources OR 6–50 sponsors
- D: ≥4 sources OR ≥50 sponsors OR has funding.json

Usage:
    uv run python -m src.github.fetch_funding
    uv run python -m src.github.fetch_funding --limit 20
    uv run python -m src.github.fetch_funding --force
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime
import logging
import time
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn,
    BarColumn, TaskProgressColumn, TimeElapsedColumn,
)

from src.github.github_client import (
    GITHUB_API,
    _AsyncRateLimiter,
    _Deferred,
    _graphql,
)
from src.pipeline.common.repos import load_risk_repos

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "funding-data.csv"
GH_REPOS_FILE = DATA_DIR / "github" / "repos.csv"
FIELDS = [
    "repo", "repo_id",
    "github_sponsors",
    "has_funding_yml",
    "funding_yml_platforms",
    "has_funding_json",
    "funding_sources",
    "funding_class",
    "fetched_at",
]
TTL_DAYS = 90

# GraphQL query — single login, returns User OR Organization sponsor count
SPONSORS_QUERY = """
query($login: String!) {
  user(login: $login) {
    sponsorshipsAsMaintainer(first: 1, includePrivate: false) {
      totalCount
    }
  }
  organization(login: $login) {
    sponsorshipsAsMaintainer(first: 1, includePrivate: false) {
      totalCount
    }
  }
}
"""


# ── parsers ──────────────────────────────────────────────────────────────────

def parse_funding_yml(text: str) -> dict[str, list[str] | str]:
    """Minimal FUNDING.yml parser — flat key/value, GitHub's format only.

    Returns {platform: value(s)}. Comments and indented lines ignored.
    Examples handled:
        github: username
        github: [user1, user2]
        custom: ['https://example.com', 'https://other.com']
    """
    result: dict[str, list[str] | str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line or line.startswith(" ") or line.startswith("\t"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            continue
        if val.startswith("[") and val.endswith("]"):
            items = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
            cleaned = [v for v in items if v]
            if cleaned:
                result[key] = cleaned
        else:
            result[key] = val.strip("'\"")
    return result


# ── per-repo fetchers ────────────────────────────────────────────────────────

async def fetch_funding_yml(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter, repo: str,
) -> tuple[bool, dict]:
    """Return (exists, parsed_dict). parsed_dict empty if not present."""
    url = f"{GITHUB_API}/repos/{repo}/contents/.github/FUNDING.yml"
    try:
        resp = await limiter.get(session, url)
    except _Deferred:
        return False, {}
    async with resp:
        if resp.status != 200:
            return False, {}
        try:
            payload = await resp.json()
        except Exception:
            return False, {}
        try:
            text = base64.b64decode(payload.get("content", "")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return True, {}
        return True, parse_funding_yml(text)


async def fetch_funding_json(
    session: aiohttp.ClientSession, repo: str,
) -> bool:
    """Check root or .well-known funding.json (FLOSS/fund manifest).

    Uses raw.githubusercontent.com so it doesn't burn API rate limit.
    """
    for path in ("funding.json", ".well-known/funding.json"):
        url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    body = (await resp.text()).strip()
                    if body and body.startswith("{"):
                        return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    return False


async def fetch_sponsors_for_login(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter, login: str,
) -> int:
    """Return public sponsor count for a User or Organization login. 0 if none."""
    if not login:
        return 0
    try:
        result = await _graphql(session, limiter, SPONSORS_QUERY, {"login": login})
    except _Deferred:
        return 0
    except RuntimeError as e:
        log.warning("graphql failed for %s: %s", login, e)
        return 0
    data = result.get("data") or {}
    user = data.get("user") or {}
    org = data.get("organization") or {}
    user_count = ((user.get("sponsorshipsAsMaintainer") or {}).get("totalCount") or 0)
    org_count = ((org.get("sponsorshipsAsMaintainer") or {}).get("totalCount") or 0)
    return int(user_count) + int(org_count)


async def fetch_funding_for_repo(
    session: aiohttp.ClientSession, limiter: _AsyncRateLimiter, repo: str,
) -> dict:
    """Fetch all three signals for one repo. Returns a dict ready for CSV."""
    owner = repo.split("/", 1)[0]

    yml_task = asyncio.create_task(fetch_funding_yml(session, limiter, repo))
    json_task = asyncio.create_task(fetch_funding_json(session, repo))
    has_yml, yml = await yml_task
    has_json = await json_task

    # Logins to query for sponsor counts: repo owner + every github user listed
    # in FUNDING.yml's `github:` key.
    logins: list[str] = [owner]
    yml_github = yml.get("github") if isinstance(yml, dict) else None
    if isinstance(yml_github, list):
        logins.extend(str(u).strip().lower() for u in yml_github if u)
    elif isinstance(yml_github, str):
        logins.append(yml_github.strip().lower())
    seen: set[str] = set()
    unique_logins = []
    for u in logins:
        ul = (u or "").lower()
        if ul and ul not in seen:
            seen.add(ul)
            unique_logins.append(ul)

    sponsor_counts = await asyncio.gather(*(
        fetch_sponsors_for_login(session, limiter, login)
        for login in unique_logins
    ))
    sponsors_total = sum(sponsor_counts)

    # Distinct funding platforms from FUNDING.yml + presence of funding.json
    yml_platforms: list[str] = list(yml.keys()) if has_yml else []
    funding_sources = len(yml_platforms) + (1 if has_json else 0)

    return {
        "repo": repo,
        "github_sponsors": sponsors_total,
        "has_funding_yml": "True" if has_yml else "False",
        "funding_yml_platforms": ",".join(yml_platforms),
        "has_funding_json": "True" if has_json else "False",
        "funding_sources": funding_sources,
    }


# ── classification ───────────────────────────────────────────────────────────

def funding_class(github_sponsors: int, funding_sources: int, has_funding_json: bool) -> str:
    """Classify funding risk.

    A (critical): 0 sources AND 0 sponsors
    B (high):     1 source OR ≤5 sponsors
    C (moderate): 2–3 sources OR 6–50 sponsors
    D (healthy):  ≥4 sources OR ≥50 sponsors OR has funding.json
    """
    if has_funding_json:
        return "D"
    if github_sponsors >= 50 or funding_sources >= 4:
        return "D"
    if github_sponsors >= 6 or funding_sources >= 2:
        return "C"
    if github_sponsors >= 1 or funding_sources >= 1:
        return "B"
    return "A"


# ── CSV I/O ──────────────────────────────────────────────────────────────────

def _load_existing() -> dict[str, dict[str, str]]:
    """Read funding-data.csv keyed by repo."""
    out: dict[str, dict[str, str]] = {}
    if not OUTPUT_FILE.exists():
        return out
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["repo"]] = row
    return out


def _load_repo_id_map() -> dict[str, str]:
    """{repo_lowercased: numeric_repo_id} from data/github/repos.csv."""
    if not GH_REPOS_FILE.exists():
        return {}
    out: dict[str, str] = {}
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            rid = (row.get("repo_id") or "").strip()
            if slug and rid:
                out[slug] = rid
    return out


def _write(rows: dict[str, dict[str, str]]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def _is_fresh(row: dict[str, str], ttl_days: int) -> bool:
    ts = (row.get("fetched_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt >= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ttl_days)


# ── main batch ───────────────────────────────────────────────────────────────

async def batch_fetch(repos: list[str], force: bool = False, limit: int | None = None,
                      concurrency: int = 8) -> None:
    existing = _load_existing()
    repo_ids = _load_repo_id_map()

    fresh_repos = (
        set() if force
        else {r for r, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    )
    to_fetch = [r for r in repos if r not in fresh_repos]
    if limit and limit < len(to_fetch):
        import random
        to_fetch = random.sample(to_fetch, limit)
    skipped = len(repos) - len(to_fetch)

    console.print(f"[bold]funding[/bold]: {len(repos)} risk-scope repos, "
                  f"{len(to_fetch)} to fetch, {skipped} skipped (fresh)")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return

    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_funding_for_repo(session, limiter, repo)
            except RuntimeError as e:
                log.warning("funding fetch failed for %s: %s", repo, e)
                return {"repo": repo, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    t_start = time.monotonic()
    completed = 0
    total = len(to_fetch)

    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("funding", total=total)
            for coro in asyncio.as_completed([one(r) for r in to_fetch]):
                result = await coro
                completed += 1
                progress.advance(task)
                if "_error" in result:
                    continue
                # Compute class and write row
                sponsors = int(result["github_sponsors"])
                sources = int(result["funding_sources"])
                has_json = result["has_funding_json"] == "True"
                cls = funding_class(sponsors, sources, has_json)
                row = {
                    **result,
                    "repo_id": repo_ids.get(result["repo"], ""),
                    "funding_class": cls,
                    "fetched_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(timespec="seconds"),
                }
                existing[result["repo"]] = row
                _write(existing)
                if completed % 25 == 0 or completed == total:
                    console.print(
                        f"[dim]{completed}/{total}  ({result['repo']}: "
                        f"sponsors={sponsors}, sources={sources}, class={cls})[/dim]"
                    )

    elapsed = time.monotonic() - t_start
    console.print(f"[green]done[/green] in {elapsed:.1f}s · "
                  f"{completed}/{total} written → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true",
                   help="ignore TTL — refetch everything")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    repos = sorted({e.repo for e in load_risk_repos() if e.repo})
    asyncio.run(batch_fetch(repos, force=args.force, limit=args.limit,
                            concurrency=args.concurrency))


if __name__ == "__main__":
    main()
