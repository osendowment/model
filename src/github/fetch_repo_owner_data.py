"""Fetch raw GitHub repo + owner metadata for AB-class projects.

Reads `data/value-data.csv`, takes every repo whose `value_class` is A or B
(roughly the top ~1.2k OSS projects across npm/pypi/crates/cpp), then:

  1. Hits `GET /repos/{owner}/{repo}` for each unique repo.
  2. Hits `GET /users/{login}` or `/orgs/{login}` for each unique owner —
     gives us the owner's `blog`, `name` (display name), `company`, etc.
     None of which are returned by the search endpoint that built
     `top-repos.csv`, so this is the only way to populate `repo_owner_url`
     in the eligibility table.

Cached: rows in `data/github/repos.csv` / `data/github/users.csv` whose
`fetched_at` is within 90 days are skipped. Use `--force` to refetch.

Async with 10-way concurrency + token rotation (reuses the same rate
limiter pattern as `fetch_top_repos.py`). For the AB scope (~1.2k repos +
~700 owners) the full run takes ~2 min on a single GitHub token.

Usage:
    uv run python -m src.github.fetch_repo_owner_data
    uv run python -m src.github.fetch_repo_owner_data --target repos
    uv run python -m src.github.fetch_repo_owner_data --target users
    uv run python -m src.github.fetch_repo_owner_data --limit 20
    uv run python -m src.github.fetch_repo_owner_data --force
    uv run python -m src.github.fetch_repo_owner_data --classes A B C
"""

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TextColumn, TimeElapsedColumn)
from rich.table import Table

from src.github.github_client import get_revolver

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

console = Console()

REPO = Path(__file__).resolve().parents[2]
VALUE_FILE = REPO / "data" / "value-data.csv"
REPOS_OUT = REPO / "data" / "github" / "repos.csv"
USERS_OUT = REPO / "data" / "github" / "users.csv"

GITHUB_API = "https://api.github.com"
TTL_DAYS = 90
MAX_CONCURRENT = 10
MAX_REDIRECTS = 10  # cap on a rename-redirect chain before we give up

REPO_FIELDS = [
    "repo", "valid", "repo_id", "node_id",
    "owner_login", "owner_id", "owner_type",
    "name", "full_name", "description", "homepage",
    "default_branch", "license", "language", "topics",
    "size", "stars", "forks", "open_issues", "watchers",
    "fork", "has_downloads", "has_issues", "has_wiki",
    "archived", "disabled", "visibility",
    "created_at", "updated_at", "pushed_at",
    "fetched_at",
]

USER_FIELDS = [
    "login", "user_id", "node_id", "type",
    "name", "blog", "company", "location", "email", "bio",
    "twitter_username", "html_url",
    "public_repos", "public_gists", "followers", "following",
    "created_at", "updated_at",
    "fetched_at",
]


# ── Rate limiter (10-way concurrent, token-rotating) ──────────────────────────


class _Limiter:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._gate = asyncio.Lock()
        self._revolver = get_revolver()
        self._n = 0

    async def get(self, session: aiohttp.ClientSession,
                  url: str) -> aiohttp.ClientResponse:
        async with self._sem:
            async with self._gate:
                wait = self._revolver.wait_time()
                if wait > 0:
                    log.info("All tokens exhausted, sleeping %.1fs", wait)
                    await asyncio.sleep(wait)
                token = self._revolver.best_token()
            headers = {"Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = await session.get(url, headers=headers, timeout=30)
            if token:
                rem = resp.headers.get("X-RateLimit-Remaining")
                rst = resp.headers.get("X-RateLimit-Reset")
                self._revolver.update(
                    token,
                    int(rem) if rem is not None else None,
                    float(rst) if rst is not None else None,
                )
            self._n += 1
            return resp

    @property
    def n(self) -> int:
        return self._n


# ── TTL / IO helpers ──────────────────────────────────────────────────────────


def _ttl_cutoff() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=TTL_DAYS)


def _is_fresh(fetched_at: str, cutoff: dt.datetime) -> bool:
    if not fetched_at:
        return False
    try:
        when = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return when >= cutoff
    except Exception:
        return False


def _load_existing(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get(key):
                continue
            # Backfill `valid` for legacy rows written before the column existed.
            # Pre-`valid` runs only wrote rows on HTTP 200, so anything in the
            # CSV with a repo_id is by construction a successful fetch.
            if not r.get("valid") and r.get("repo_id"):
                r["valid"] = "True"
            out[r[key].lower()] = r
    return out


def _atomic_write(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Inputs ────────────────────────────────────────────────────────────────────


def load_ab_repos(classes: set[str]) -> list[str]:
    """Return unique repo slugs (lowercased, sorted) whose `class` is in `classes`.

    Note: `data/value-data.csv` uses column name `class`; per-ecosystem
    `results.csv` files use `value_class`. We read the unified file here.
    """
    seen: set[str] = set()
    with open(VALUE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("class") or "") not in classes:
                continue
            repo = (r.get("github_repo") or "").strip().lower()
            if repo and "/" in repo:
                seen.add(repo)
    return sorted(seen)


def owners_from_repos(repos: list[str]) -> list[str]:
    return sorted({r.split("/", 1)[0] for r in repos})


# ── Repo flattening ───────────────────────────────────────────────────────────


def _flat_repo(d: dict, full_name: str) -> dict:
    owner = d.get("owner") or {}
    lic = (d.get("license") or {}) if isinstance(d.get("license"), dict) else {}
    return {
        # Always key on the slug we asked for. If GitHub redirected us, the
        # API's full_name will differ — capture the new name in `full_name`.
        "repo": full_name.lower(),
        "valid": True,
        "repo_id": d.get("id", ""),
        "node_id": d.get("node_id", ""),
        "owner_login": owner.get("login", ""),
        "owner_id": owner.get("id", ""),
        "owner_type": owner.get("type", ""),
        "name": d.get("name", ""),
        "full_name": d.get("full_name", ""),
        "description": (d.get("description") or "")[:500],
        "homepage": d.get("homepage") or "",
        "default_branch": d.get("default_branch", ""),
        "license": lic.get("spdx_id") or lic.get("key") or "",
        "language": d.get("language") or "",
        "topics": " | ".join(d.get("topics") or []),
        "size": d.get("size", ""),
        "stars": d.get("stargazers_count", ""),
        "forks": d.get("forks_count", ""),
        "open_issues": d.get("open_issues_count", ""),
        "watchers": d.get("subscribers_count", ""),
        "fork": d.get("fork", ""),
        "has_downloads": d.get("has_downloads", ""),
        "has_issues": d.get("has_issues", ""),
        "has_wiki": d.get("has_wiki", ""),
        "archived": d.get("archived", ""),
        "disabled": d.get("disabled", ""),
        "visibility": d.get("visibility", ""),
        "created_at": d.get("created_at", ""),
        "updated_at": d.get("updated_at", ""),
        "pushed_at": d.get("pushed_at", ""),
        "fetched_at": _now_iso(),
    }


def _flat_user(d: dict, login: str) -> dict:
    return {
        "login": d.get("login", login),
        "user_id": d.get("id", ""),
        "node_id": d.get("node_id", ""),
        "type": d.get("type", ""),
        "name": d.get("name") or "",
        "blog": d.get("blog") or "",
        "company": d.get("company") or "",
        "location": d.get("location") or "",
        "email": d.get("email") or "",
        "bio": (d.get("bio") or "")[:500],
        "twitter_username": d.get("twitter_username") or "",
        "html_url": d.get("html_url", ""),
        "public_repos": d.get("public_repos", ""),
        "public_gists": d.get("public_gists", ""),
        "followers": d.get("followers", ""),
        "following": d.get("following", ""),
        "created_at": d.get("created_at", ""),
        "updated_at": d.get("updated_at", ""),
        "fetched_at": _now_iso(),
    }


# ── Async fetch loops ─────────────────────────────────────────────────────────


def _invalid_repo_row(slug: str) -> dict:
    """Sparse row for a 404'd repo — preserves the slug + check timestamp so
    re-runs honour the TTL and don't keep hammering known-dead repos."""
    return {"repo": slug.lower(), "valid": False, "fetched_at": _now_iso()}


async def _fetch_repo(limiter: _Limiter, session: aiohttp.ClientSession,
                      slug: str) -> tuple[str, dict | None, str]:
    """Return (slug, row, status).

    On 200 → row is the flattened repo with valid=True.
    On 404 → row is a sparse {repo, valid=False, fetched_at} so the repo is
             recorded as known-invalid and won't be re-fetched until TTL expires.
    On transient errors → row is None (will retry on the next run).

    301/302 redirects (a renamed or moved repo) are followed *iteratively*
    to the end of the chain — a repo can be renamed more than once, so a
    single hop may land on yet another redirect. The originally-requested
    `slug` stays the row key (so the cache/TTL keep tracking it); the repo's
    current name lands in the row's `full_name` from the final payload.
    A chain longer than `MAX_REDIRECTS` is abandoned as `redirect_loop`.
    """
    url = f"{GITHUB_API}/repos/{slug}"
    for attempt in range(4):
        current = url
        redirects = 0
        while True:
            try:
                resp = await limiter.get(session, current)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                break  # transient — fall through to the back-off + retry
            async with resp:
                if resp.status == 200:
                    return slug, _flat_repo(await resp.json(), slug), "ok"
                if resp.status == 404:
                    return slug, _invalid_repo_row(slug), "404"
                if resp.status in (301, 302) and "Location" in resp.headers:
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        log.warning("%s: redirect chain exceeded %d hops",
                                    slug, MAX_REDIRECTS)
                        return slug, None, "redirect_loop"
                    current = resp.headers["Location"]
                    continue  # follow the next hop of the chain
                if resp.status == 403:
                    break  # rate limited — re-queue via the outer retry
                text = await resp.text()
                log.warning("%s: HTTP %d %s", slug, resp.status, text[:120])
                return slug, None, f"http_{resp.status}"
        # Inner loop exited without returning → transient error or 403
        # rate-limit → back off and retry the whole chain from the start.
        await asyncio.sleep(2 ** attempt)
    return slug, None, "error"


async def _fetch_user(limiter: _Limiter, session: aiohttp.ClientSession,
                      login: str) -> tuple[str, dict | None, str]:
    """Return (login, flattened-row-or-None, status)."""
    # `/users/{login}` works for both User and Org accounts.
    url = f"{GITHUB_API}/users/{login}"
    for attempt in range(4):
        try:
            resp = await limiter.get(session, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt)
            continue
        async with resp:
            if resp.status == 200:
                return login, _flat_user(await resp.json(), login), "ok"
            if resp.status == 404:
                return login, None, "404"
            if resp.status == 403:
                await asyncio.sleep(2)
                continue
            return login, None, f"http_{resp.status}"
    return login, None, "error"


# ── Orchestration ─────────────────────────────────────────────────────────────


async def fetch_many(items: list[str], fetch_one, label: str
                     ) -> tuple[list[dict], dict[str, int]]:
    """Drive async fetches with a rich progress bar; collect rows + status counts."""
    limiter = _Limiter()
    rows: list[dict] = []
    status_counts: dict[str, int] = {}

    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[status]}"),
        console=console,
    )

    async with aiohttp.ClientSession() as session:
        with progress:
            task = progress.add_task(label, total=len(items), status="")
            sem = asyncio.Semaphore(MAX_CONCURRENT)

            async def _wrap(item):
                async with sem:
                    return await fetch_one(limiter, session, item)

            coros = [asyncio.create_task(_wrap(it)) for it in items]
            for coro in asyncio.as_completed(coros):
                _, row, status = await coro
                if row:
                    rows.append(row)
                status_counts[status] = status_counts.get(status, 0) + 1
                progress.update(
                    task, advance=1,
                    status=f"{limiter.n} api calls · "
                           f"{', '.join(f'{k}={v}' for k, v in status_counts.items())}",
                )

    return rows, status_counts


def upsert(out_path: Path, key: str, fields: list[str],
           new_rows: list[dict]) -> int:
    """Merge new rows into the existing CSV by `key`. Returns total written."""
    existing = _load_existing(out_path, key)
    for r in new_rows:
        k = (r.get(key) or "").lower()
        if not k:
            continue
        existing[k] = r
    sorted_rows = sorted(existing.values(), key=lambda r: r[key].lower())
    _atomic_write(out_path, sorted_rows, fields)
    return len(sorted_rows)


# ── Main ──────────────────────────────────────────────────────────────────────


def _filter_stale(items: list[str], existing: dict[str, dict],
                  cutoff: dt.datetime, force: bool) -> tuple[list[str], int, int]:
    """Return (to_fetch, fresh_count, missing_count)."""
    to_fetch, fresh, missing = [], 0, 0
    for it in items:
        row = existing.get(it.lower())
        if row and not force and _is_fresh(row.get("fetched_at", ""), cutoff):
            fresh += 1
        else:
            to_fetch.append(it)
            if not row:
                missing += 1
    return to_fetch, fresh, missing


def fetch_and_persist(
    repos: list[str] | None = None,
    owners: list[str] | None = None,
    target: str = "both",
    force: bool = False,
    limit: int | None = None,
    quiet: bool = False,
) -> dict:
    """Fetch /repos/{slug} + /users/{login} for given lists. Idempotent.

    The callable form of `main()` so other pipelines (notably
    `src.pipeline.value.unify_value_data`) can drive github data collection without going
    through the CLI. Behaviour:

    - Caches via 90-day TTL on `fetched_at` (loaded from existing CSVs).
    - 404'd repos are recorded with `valid=False` so re-runs honour the
      same TTL on dead repos.
    - If `repos` is None, falls back to the historical AB-class loader.
    - If `owners` is None, derives from `repos` (or from `repos.csv` after
      the repo phase, to pick up any redirects).

    Returns: {"repos": {fresh, fetched, statuses, total},
              "users": {...}, "elapsed_s": float}.
    """
    if not quiet:
        console.rule("[bold cyan]github/fetch_repo_owner_data")
        console.print(f"  TTL=[dim]{TTL_DAYS}d[/dim]  force=[dim]{force}[/dim]  target=[dim]{target}[/dim]")

    if repos is None:
        repos = load_ab_repos({"A", "B"})
    if owners is None:
        owners = owners_from_repos(repos)

    if not quiet:
        console.print(
            f"  Scope: [bold]{len(repos):,}[/bold] repos · "
            f"[bold]{len(owners):,}[/bold] unique owners")

    cutoff = _ttl_cutoff()
    out: dict = {"elapsed_s": 0.0}
    t0 = time.monotonic()

    if target in ("repos", "both"):
        existing = _load_existing(REPOS_OUT, "repo")
        to_fetch, fresh, missing = _filter_stale(repos, existing, cutoff, force)
        if limit:
            to_fetch = to_fetch[:limit]
        if not quiet:
            console.print(f"  [bold]Repos:[/bold] fresh={fresh:,} missing={missing:,} "
                          f"to_fetch={len(to_fetch):,}")
        if to_fetch:
            new_rows, statuses = asyncio.run(
                fetch_many(to_fetch, _fetch_repo, "repos"))
        else:
            new_rows, statuses = [], {}
        total = upsert(REPOS_OUT, "repo", REPO_FIELDS, new_rows)
        out["repos"] = {"fresh": fresh, "fetched": len(new_rows),
                        "statuses": statuses, "total": total}

    if target in ("users", "both"):
        # Re-derive owners after repo upsert so renames/redirects propagate.
        if REPOS_OUT.exists():
            wanted = {x.lower() for x in repos}
            with open(REPOS_OUT, encoding="utf-8") as f:
                owners = sorted({(r["owner_login"] or "").lower()
                                 for r in csv.DictReader(f)
                                 if r.get("owner_login") and r["repo"] in wanted})
        existing = _load_existing(USERS_OUT, "login")
        to_fetch, fresh, missing = _filter_stale(owners, existing, cutoff, force)
        if limit:
            to_fetch = to_fetch[:limit]
        if not quiet:
            console.print(f"  [bold]Users:[/bold] fresh={fresh:,} missing={missing:,} "
                          f"to_fetch={len(to_fetch):,}")
        if to_fetch:
            new_rows, statuses = asyncio.run(
                fetch_many(to_fetch, _fetch_user, "users"))
        else:
            new_rows, statuses = [], {}
        total = upsert(USERS_OUT, "login", USER_FIELDS, new_rows)
        out["users"] = {"fresh": fresh, "fetched": len(new_rows),
                        "statuses": statuses, "total": total}

    out["elapsed_s"] = time.monotonic() - t0
    if not quiet:
        console.print(f"  [dim]elapsed {out['elapsed_s']:.1f}s[/dim]")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--target", choices=["repos", "users", "both"], default="both")
    p.add_argument("--classes", nargs="+", default=["A", "B"],
                   help="value_class values to include (default: A B)")
    p.add_argument("--limit", type=int, default=None,
                   help="cap on number of items to fetch (testing)")
    p.add_argument("--force", action="store_true",
                   help="ignore TTL — refetch everything")
    args = p.parse_args()

    repos = load_ab_repos(set(args.classes))
    owners = owners_from_repos(repos)
    fetch_and_persist(
        repos=repos, owners=owners,
        target=args.target, force=args.force, limit=args.limit,
    )


if __name__ == "__main__":
    main()
