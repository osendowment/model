"""Fetch .github/FUNDING.yml signals per risk-scope repo.

Writes data/sources/github/funding-yml.csv:
    repo, repo_id, has_funding_yml, funding_yml_platforms,
    <one column per platform in FUNDING_PLATFORMS>, fetched_at

`funding_yml_platforms` = the FUNDING.yml mapping keys (github, patreon, …).
Each platform column holds that platform's handle(s) (comma-joined for a
list); the `github` column is the lower-cased, deduped usernames under the
`github:` key (so the sponsors fetcher can count co-maintainer sponsorships).

TTL-controlled: re-runs only fetch repos missing or older than TTL_DAYS.

Usage:
    uv run python -m src.sources.github.fetch_funding_yml
    uv run python -m src.sources.github.fetch_funding_yml --limit 20
    uv run python -m src.sources.github.fetch_funding_yml --force
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime
import logging
from pathlib import Path

import aiohttp
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from src.common.funding_platforms import FUNDING_PLATFORMS
from src.common.repos import load_risk_repos
from src.sources.github.github_client import GITHUB_API, _AsyncRateLimiter, _Deferred

console = Console()
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
FIELDS = (
    ["repo", "repo_id", "has_funding_yml", "funding_yml_platforms"]
    + FUNDING_PLATFORMS
    + ["fetched_at"]
)
TTL_DAYS = 90


def parse_funding_yml(text: str) -> dict[str, list[str] | str]:
    """Parse a `.github/FUNDING.yml` into {platform: value(s)}.

    FUNDING.yml is YAML, so we parse it as YAML — this correctly handles
    every shape GitHub accepts: a scalar value, an inline `[a, b]` list,
    AND a block-style list whose `- item` lines sit at column 0 under the
    key. The old hand-rolled line parser mis-read each block-list line as
    its own `key: value` pair (a URL's `https:` became the separator),
    emitting a bogus `- https` platform and silently dropping the real
    `custom:` key.

    Keys whose value is empty (null / "" / []) are dropped — an unset
    platform is not a funding signal. A file that is not a YAML mapping
    (or fails to parse) yields an empty dict.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str] | str] = {}
    for key, val in data.items():
        if val is None or val == "" or val == [] or val == {}:
            continue
        if isinstance(val, list):
            cleaned = [str(v).strip() for v in val if v not in (None, "")]
            if cleaned:
                result[str(key)] = cleaned
        else:
            result[str(key)] = str(val).strip()
    return result


def funding_yml_github_logins(yml: dict) -> list[str]:
    """Lower-cased usernames under the FUNDING.yml `github:` key (deduped, ordered)."""
    g = yml.get("github") if isinstance(yml, dict) else None
    raw = g if isinstance(g, list) else ([g] if isinstance(g, str) else [])
    seen: set[str] = set()
    out: list[str] = []
    for u in raw:
        ul = str(u).strip().lower()
        if ul and ul not in seen:
            seen.add(ul)
            out.append(ul)
    return out


def platform_handle_value(yml: dict, platform: str) -> str:
    """The FUNDING.yml handle(s) for one platform, comma-joined.

    `github` is special-cased to the deduped, lower-cased login list. Other
    platforms keep their raw value(s) as written (URLs/handles), stripped.
    Returns "" when the platform key is absent.
    """
    if platform == "github":
        return ",".join(funding_yml_github_logins(yml))
    val = yml.get(platform) if isinstance(yml, dict) else None
    if isinstance(val, list):
        return ",".join(str(v).strip() for v in val if str(v).strip())
    if isinstance(val, str):
        return val.strip()
    return ""


async def fetch_funding_yml(session, limiter, repo: str) -> tuple[bool, dict]:
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
            text = base64.b64decode(payload.get("content", "")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return True, {}
        return True, parse_funding_yml(text)


async def fetch_one(session, limiter, repo: str) -> dict:
    has_yml, yml = await fetch_funding_yml(session, limiter, repo)
    platforms = list(yml.keys()) if has_yml else []
    row = {
        "repo": repo,
        "has_funding_yml": "True" if has_yml else "False",
        "funding_yml_platforms": ",".join(platforms),
    }
    for p in FUNDING_PLATFORMS:
        row[p] = platform_handle_value(yml, p)
    return row


def _load_existing() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["repo"]] = row
    return out


def _load_repo_id_map() -> dict[str, str]:
    out: dict[str, str] = {}
    if GH_REPOS_FILE.exists():
        with open(GH_REPOS_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                slug = (row.get("repo") or "").strip().lower()
                rid = (row.get("repo_id") or "").strip()
                if slug and rid:
                    out[slug] = rid
    return out


def _write(rows: dict[str, dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for repo in sorted(rows):
            w.writerow(rows[repo])


def _is_fresh(row: dict, ttl_days: int) -> bool:
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


async def batch(repos: list[str], force: bool, limit: int | None, concurrency: int) -> None:
    existing = _load_existing()
    repo_ids = _load_repo_id_map()
    fresh = set() if force else {r for r, row in existing.items() if _is_fresh(row, TTL_DAYS)}
    to_fetch = [r for r in repos if r not in fresh]
    if limit and limit < len(to_fetch):
        import random
        to_fetch = random.sample(to_fetch, limit)
    console.print(f"[bold]funding-yml[/bold]: {len(repos)} repos, {len(to_fetch)} to fetch")
    if not to_fetch:
        console.print("[dim]Nothing to fetch.[/dim]")
        return
    limiter = _AsyncRateLimiter()
    sem = asyncio.Semaphore(concurrency)

    async def one(repo: str) -> dict:
        async with sem:
            try:
                return await fetch_one(session, limiter, repo)
            except RuntimeError as e:
                log.warning("funding-yml failed for %s: %s", repo, e)
                return {"repo": repo, "_error": str(e)}

    headers = {"Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
                      TaskProgressColumn(), TimeElapsedColumn(), console=console) as prog:
            task = prog.add_task("funding-yml", total=len(to_fetch))
            for coro in asyncio.as_completed([one(r) for r in to_fetch]):
                res = await coro
                prog.advance(task)
                if "_error" in res:
                    continue
                res["repo_id"] = repo_ids.get(res["repo"], "")
                res["fetched_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds")
                existing[res["repo"]] = res
                _write(existing)
    console.print(f"[green]done[/green] → {OUTPUT_FILE}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--force", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repos = sorted({e.repo for e in load_risk_repos() if e.repo})
    asyncio.run(batch(repos, args.force, args.limit, args.concurrency))


if __name__ == "__main__":
    main()
