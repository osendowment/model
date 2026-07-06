"""Fetch raw GitLab project + namespace metadata for GitLab-family repos.

Reads `data/value/value.csv`, selects every row whose `git_url` points at a
GitLab instance, then hits `GET /projects/:url_encoded_path?license=true` on
that instance and flattens the response to `data/sources/gitlab/repos.csv`
(plus a best-effort `GET /projects/:id/languages` for the primary `language`).
Owner/group metadata goes to `data/sources/gitlab/namespaces.csv`.

Mirrors src/sources/github/fetch_repo_owner_data.py (same TTL/upsert/redirect
patterns), keyed on the unified `repo_id = gl/{nickname}-{project_id}` (bare
`gl/{project_id}` for gitlab.com; nicknames come from `HOST_NICKNAMES` in
`gitlab_client.py` — see `gitlab_client.make_repo_id`).

Usage:
    uv run python -m src.sources.gitlab.fetch_project_data
    uv run python -m src.sources.gitlab.fetch_project_data --limit 5
    uv run python -m src.sources.gitlab.fetch_project_data --target namespaces
    uv run python -m src.sources.gitlab.fetch_project_data --force
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import logging
import os
import time
from pathlib import Path

import aiohttp
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from src.common.freshness import row_is_fresh
from src.sources.gitlab.gitlab_client import GitLabLimiter, api_base, encode_project_path, make_repo_id, parse_git_url

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
console = Console()

REPO = Path(__file__).resolve().parents[3]
VALUE_FILE = REPO / "data" / "value" / "value.csv"
REPOS_OUT = REPO / "data" / "sources" / "gitlab" / "repos.csv"
NAMESPACES_OUT = REPO / "data" / "sources" / "gitlab" / "namespaces.csv"

TTL_DAYS = 90
MAX_CONCURRENT = 10
MAX_REDIRECTS = 10

PROJECT_FIELDS = [
    "project", "valid", "project_id", "repo_id", "host",
    "owner_type", "namespace_kind", "namespace_path",
    "name", "path_with_namespace", "description", "homepage",
    "default_branch", "license", "language", "topics",
    "stars", "forks", "open_issues", "archived", "visibility",
    "created_at", "last_activity_at", "fetched_at",
]

NAMESPACE_FIELDS = [
    "namespace", "namespace_id", "host", "kind", "name", "path",
    "full_path", "web_url", "description", "fetched_at",
]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _primary_language(langs: dict) -> str:
    """Primary language = the key with the largest byte-share percentage.

    `GET /projects/:id/languages` returns `{"C": 90.8, "CMake": 3.7, ...}`,
    the GitLab analogue of GitHub-linguist. We keep only the top language as a
    single string, mirroring GitHub's scalar `language` column. `{}` (empty or
    binary-only repo) → "".
    """
    if not langs:
        return ""
    return max(langs, key=langs.get)


def load_gitlab_rows(value_file: Path | None = None,
                     classes: set[str] | None = None) -> list[dict]:
    """Unique GitLab targets from value.csv.

    Returns [{host, path, project}] where `project` = "{host}/{path}".lower()
    is the dedup + upsert key. Rows whose `git_url` is not a GitLab instance
    are skipped. When `classes` is given (e.g. {"A", "B"}), only rows whose
    `class` column is in that set are included — mirrors the GitHub owner
    fetcher's default A/B scoping.
    """
    vf = value_file or VALUE_FILE
    seen: dict[str, dict] = {}
    with open(vf, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if classes is not None and (r.get("class") or "").strip() not in classes:
                continue
            parsed = parse_git_url((r.get("git_url") or "").strip())
            if not parsed:
                continue
            host, path = parsed
            key = f"{host}/{path}".lower()
            if key not in seen:
                seen[key] = {"host": host, "path": path, "project": key}
    return sorted(seen.values(), key=lambda x: x["project"])


def _flat_project(d: dict, host: str, project_key: str, language: str = "") -> dict:
    ns = d.get("namespace") or {}
    kind = ns.get("kind", "")
    lic = d.get("license") if isinstance(d.get("license"), dict) else {}
    pid = d.get("id", "")
    owner_type = "Organization" if kind == "group" else "User" if kind == "user" else ""
    return {
        "project": project_key,
        "valid": True,
        "project_id": pid,
        "repo_id": make_repo_id(host, pid) if pid != "" else "",
        "host": host,
        "owner_type": owner_type,
        "namespace_kind": kind,
        "namespace_path": ns.get("full_path", ""),
        "name": d.get("name", ""),
        "path_with_namespace": d.get("path_with_namespace", ""),
        "description": (d.get("description") or "")[:500],
        "homepage": d.get("web_url") or d.get("homepage") or "",
        "default_branch": d.get("default_branch", ""),
        "license": (lic or {}).get("key") or (lic or {}).get("nickname") or "",
        "language": language,
        "topics": " | ".join(d.get("topics") or d.get("tag_list") or []),
        "stars": d.get("star_count", ""),
        "forks": d.get("forks_count", ""),
        "open_issues": d.get("open_issues_count", ""),
        "archived": d.get("archived", ""),
        "visibility": d.get("visibility", ""),
        "created_at": d.get("created_at", ""),
        "last_activity_at": d.get("last_activity_at", ""),
        "fetched_at": _now_iso(),
    }


def _invalid_project_row(host: str, project_key: str) -> dict:
    """Sparse row for a 404'd project — preserves key + timestamp so re-runs
    honour the TTL and don't keep hammering known-dead projects."""
    return {"project": project_key, "valid": False, "host": host,
            "fetched_at": _now_iso()}


async def _fetch_languages(limiter, session, host: str, project_id) -> str:
    """Best-effort primary language via `GET /projects/:id/languages`.

    Keyed by numeric id (rename/redirect-proof). Retries 429/transient with
    backoff — same as `_fetch_project` — so a rate-limited miss doesn't get
    mistaken for a genuinely empty breakdown. A blank is returned only when the
    repo really has no detected languages (`{}`) or every retry is exhausted;
    the project row still carries valid+fetched_at either way."""
    if project_id in ("", None):
        return ""
    url = f"{api_base(host)}/projects/{project_id}/languages"
    for attempt in range(4):
        try:
            resp = await limiter.get(session, host, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt)
            continue
        async with resp:
            if resp.status == 200:
                try:
                    return _primary_language(await resp.json())
                except (aiohttp.ClientError, ValueError):
                    return ""
            if resp.status == 429:                 # throttled — back off and retry
                await asyncio.sleep(2 ** attempt)
                continue
            return ""                              # 404/other → genuinely no languages
    return ""


async def _fetch_project(limiter, session, item: dict) -> tuple[str, dict | None, str]:
    """Return (project_key, row, status). 200→ok row; 404→sparse invalid row;
    transient→None. Follows 301/302 rename chains to the terminal response."""
    host, path, key = item["host"], item["path"], item["project"]
    url = f"{api_base(host)}/projects/{encode_project_path(path)}?license=true"
    for attempt in range(4):
        current = url
        redirects = 0
        while True:
            try:
                resp = await limiter.get(session, host, current)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                break
            async with resp:
                if resp.status == 200:
                    d = await resp.json()
                    lang = await _fetch_languages(limiter, session, host, d.get("id"))
                    return key, _flat_project(d, host, key, lang), "ok"
                if resp.status == 404:
                    return key, _invalid_project_row(host, key), "404"
                if resp.status in (301, 302) and "Location" in resp.headers:
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        return key, None, "redirect_loop"
                    current = resp.headers["Location"]
                    continue
                if resp.status == 429:
                    break
                log.warning("%s: HTTP %d", key, resp.status)
                return key, None, f"http_{resp.status}"
        await asyncio.sleep(2 ** attempt)
    return key, None, "error"


def _flat_namespace(d: dict, host: str, ns_key: str) -> dict:
    """Flatten a namespace object to a row."""
    return {
        "namespace": ns_key,
        "namespace_id": d.get("id", ""),
        "host": host,
        "kind": d.get("kind", ""),
        "name": d.get("name", ""),
        "path": d.get("path", ""),
        "full_path": d.get("full_path", ""),
        "web_url": d.get("web_url", ""),
        "description": (d.get("description") or "")[:500],
        "fetched_at": _now_iso(),
    }


async def _fetch_namespace(limiter, session, item: dict) -> tuple[str, dict | None, str]:
    """Return (namespace_key, row|None, status). 200→ok; 404→None; other/transient handled."""
    host, full_path, key = item["host"], item["full_path"], item["namespace"]
    url = f"{api_base(host)}/namespaces/{encode_project_path(full_path)}"
    for attempt in range(4):
        try:
            resp = await limiter.get(session, host, url)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** attempt)
            continue
        async with resp:
            if resp.status == 200:
                return key, _flat_namespace(await resp.json(), host, key), "ok"
            if resp.status == 404:
                return key, None, "404"
            if resp.status == 429:
                await asyncio.sleep(2)
                continue
            return key, None, f"http_{resp.status}"
    return key, None, "error"


def _load_existing(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r.get(key) or "").lower()
            if k:
                out[k] = r
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


def _filter_stale(items: list[dict], existing: dict[str, dict], key: str,
                  force: bool) -> tuple[list[dict], int, int]:
    """Return (to_fetch, fresh_count, missing_count) using a 90-day TTL.

    A row whose stored `valid` is the string "False" is still re-checked only
    once its `fetched_at` ages past TTL (dead-repo backoff), exactly like the
    GitHub fetcher — `row_is_fresh` handles the timestamp; missing rows always
    fetch.
    """
    to_fetch, fresh, missing = [], 0, 0
    for it in items:
        row = existing.get(it[key].lower())
        if row and not force and row_is_fresh(row, ttl_days=TTL_DAYS):
            fresh += 1
        else:
            to_fetch.append(it)
            if not row:
                missing += 1
    return to_fetch, fresh, missing


async def fetch_many(items: list[dict], fetch_one, label: str
                     ) -> tuple[list[dict], dict[str, int]]:
    """Drive async fetches with a rich progress bar; collect rows + status counts."""
    token_map = None  # GitLabLimiter loads from env by default
    limiter = GitLabLimiter(token_map)
    rows: list[dict] = []
    status_counts: dict[str, int] = {}
    progress = Progress(
        TextColumn("[bold cyan]{task.description}"), BarColumn(bar_width=30),
        MofNCompleteColumn(), TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[status]}"), console=console,
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
                progress.update(task, advance=1,
                                status=f"{limiter.n} api calls · "
                                       f"{', '.join(f'{k}={v}' for k, v in status_counts.items())}")
    return rows, status_counts


def _fresher(a: dict, b: dict) -> bool:
    """True if row `a` should win over `b` for the same id: a valid row beats a
    sparse/invalid one, then the more recently `fetched_at` wins (ties → a)."""
    av = str(a.get("valid")).strip().lower() == "true"
    bv = str(b.get("valid")).strip().lower() == "true"
    if av != bv:
        return av
    return (a.get("fetched_at") or "") >= (b.get("fetched_at") or "")


def _dedupe_by_id(rows: list[dict], id_field: str) -> list[dict]:
    """Collapse rows sharing a non-blank `id_field` to the single freshest one.

    A renamed GitLab project otherwise leaves a stale row under its old path
    *and* a row under the new path — both carrying the same `repo_id`. Every
    downstream join is by `repo_id`, so a duplicate lets an arbitrary (possibly
    staler/blank) row win. Rows with a blank `id_field` (e.g. sparse 404 rows,
    which carry no `repo_id`) are kept as-is, keyed by their own `project`."""
    best: dict[str, dict] = {}
    passthrough: list[dict] = []
    for r in rows:
        rid = (r.get(id_field) or "").strip()
        if not rid:
            passthrough.append(r)
            continue
        if rid not in best or _fresher(r, best[rid]):
            best[rid] = r
    return list(best.values()) + passthrough


def upsert(out_path: Path, key: str, fields: list[str], new_rows: list[dict],
           dedupe_by: str | None = None) -> int:
    """Merge new rows into the CSV by `key`. Returns total written.

    When `dedupe_by` is set (e.g. `"repo_id"`), rows that collapse to the same
    value in that column after the merge are reduced to the freshest one — this
    is what stops a renamed project from persisting under both its old and new
    path (see `_dedupe_by_id`)."""
    existing = _load_existing(out_path, key)
    for r in new_rows:
        k = (r.get(key) or "").lower()
        if k:
            existing[k] = r
    rows = list(existing.values())
    if dedupe_by:
        rows = _dedupe_by_id(rows, dedupe_by)
    sorted_rows = sorted(rows, key=lambda r: (r.get(key) or "").lower())
    _atomic_write(out_path, sorted_rows, fields)
    return len(sorted_rows)


def _namespaces_from_projects() -> list[dict]:
    """Derive namespace targets from repos.csv (after the project phase)."""
    if not REPOS_OUT.exists():
        return []
    seen: dict[str, dict] = {}
    with open(REPOS_OUT, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            host = r.get("host") or ""
            full = r.get("namespace_path") or ""
            if host and full:
                key = f"{host}/{full}".lower()
                seen[key] = {"host": host, "full_path": full, "namespace": key}
    return sorted(seen.values(), key=lambda x: x["namespace"])


def fetch_and_persist(target: str = "projects", force: bool = False,
                      limit: int | None = None, quiet: bool = False,
                      classes: set[str] | None = None,
                      targets: list[dict] | None = None) -> dict:
    """Fetch GitLab projects (+ namespaces). Idempotent under the 90-day TTL.

    `classes` (e.g. {"A", "B"}) scopes the projects phase to those value
    classes; None fetches every GitLab-hosted repo. `targets` (a list of
    ``{host, path, project}`` dicts, as produced by `load_gitlab_rows`) overrides
    the value.csv scan entirely — the value-rollup resolver passes the GitLab
    URLs it is resolving directly, mirroring the GitHub fetcher's explicit
    `repos=` list.
    """
    if not quiet:
        console.rule("[bold cyan]gitlab/fetch_project_data")
        scope = "/".join(sorted(classes)) if classes else "all"
        console.print(f"  TTL=[dim]{TTL_DAYS}d[/dim]  force=[dim]{force}[/dim]  "
                      f"target=[dim]{target}[/dim]  classes=[dim]{scope}[/dim]")
    out: dict = {"elapsed_s": 0.0}
    t0 = time.monotonic()

    if target in ("projects", "both"):
        items = targets if targets is not None else load_gitlab_rows(classes=classes)
        existing = _load_existing(REPOS_OUT, "project")
        to_fetch, fresh, missing = _filter_stale(items, existing, "project", force)
        if limit:
            to_fetch = to_fetch[:limit]
        if not quiet:
            console.print(f"  [bold]Projects:[/bold] fresh={fresh:,} missing={missing:,} "
                          f"to_fetch={len(to_fetch):,}")
        new_rows, statuses = (asyncio.run(fetch_many(to_fetch, _fetch_project, "projects"))
                              if to_fetch else ([], {}))
        total = upsert(REPOS_OUT, "project", PROJECT_FIELDS, new_rows,
                       dedupe_by="repo_id")
        out["projects"] = {"fresh": fresh, "fetched": len(new_rows),
                           "statuses": statuses, "total": total}

    if target in ("namespaces", "both"):
        items = _namespaces_from_projects()
        existing = _load_existing(NAMESPACES_OUT, "namespace")
        to_fetch, fresh, missing = _filter_stale(items, existing, "namespace", force)
        if limit:
            to_fetch = to_fetch[:limit]
        if not quiet:
            console.print(f"  [bold]Namespaces:[/bold] fresh={fresh:,} missing={missing:,} "
                          f"to_fetch={len(to_fetch):,}")
        new_rows, statuses = (asyncio.run(fetch_many(to_fetch, _fetch_namespace, "namespaces"))
                              if to_fetch else ([], {}))
        total = upsert(NAMESPACES_OUT, "namespace", NAMESPACE_FIELDS, new_rows)
        out["namespaces"] = {"fresh": fresh, "fetched": len(new_rows),
                             "statuses": statuses, "total": total}

    out["elapsed_s"] = time.monotonic() - t0
    if not quiet:
        console.print(f"  [dim]elapsed {out['elapsed_s']:.1f}s[/dim]")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--target", choices=["projects", "namespaces", "both"], default="both")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--classes", nargs="+", default=None,
                   help="value classes to include (e.g. A B); default: all")
    args = p.parse_args()
    fetch_and_persist(target=args.target, force=args.force, limit=args.limit,
                      classes=set(args.classes) if args.classes else None)


if __name__ == "__main__":
    main()
