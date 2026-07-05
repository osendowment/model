"""Fetch raw GitLab project + namespace metadata for GitLab-family repos.

Reads `data/value/value.csv`, selects every row whose `git_url` points at a
GitLab instance, then hits `GET /projects/:url_encoded_path?license=true` on
that instance and flattens the response to `data/sources/gitlab/projects.csv`.
Owner/group metadata goes to `data/sources/gitlab/namespaces.csv`.

Mirrors src/sources/github/fetch_repo_owner_data.py (same TTL/upsert/redirect
patterns), keyed on the unified `repo_id = gl/{host}/{project_id}`.

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
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn)

from src.common.freshness import row_is_fresh
from src.sources.gitlab.gitlab_client import (GitLabLimiter, api_base,
                                              encode_project_path, make_repo_id,
                                              parse_git_url)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
console = Console()

REPO = Path(__file__).resolve().parents[3]
VALUE_FILE = REPO / "data" / "value" / "value.csv"
PROJECTS_OUT = REPO / "data" / "sources" / "gitlab" / "projects.csv"
NAMESPACES_OUT = REPO / "data" / "sources" / "gitlab" / "namespaces.csv"

TTL_DAYS = 90
MAX_CONCURRENT = 10
MAX_REDIRECTS = 10

PROJECT_FIELDS = [
    "project", "valid", "project_id", "repo_id", "host",
    "owner_type", "namespace_kind", "namespace_path",
    "name", "path_with_namespace", "description", "homepage",
    "default_branch", "license", "topics",
    "stars", "forks", "open_issues", "archived", "visibility",
    "created_at", "last_activity_at", "fetched_at",
]

NAMESPACE_FIELDS = [
    "namespace", "namespace_id", "host", "kind", "name", "path",
    "full_path", "web_url", "description", "fetched_at",
]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_gitlab_rows(value_file: Path | None = None) -> list[dict]:
    """Unique GitLab targets from value.csv.

    Returns [{host, path, project}] where `project` = "{host}/{path}".lower()
    is the dedup + upsert key. Rows whose `git_url` is not a GitLab instance
    are skipped.
    """
    vf = value_file or VALUE_FILE
    seen: dict[str, dict] = {}
    with open(vf, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            parsed = parse_git_url((r.get("git_url") or "").strip())
            if not parsed:
                continue
            host, path = parsed
            key = f"{host}/{path}".lower()
            if key not in seen:
                seen[key] = {"host": host, "path": path, "project": key}
    return sorted(seen.values(), key=lambda x: x["project"])


def _flat_project(d: dict, host: str, project_key: str) -> dict:
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
                    return key, _flat_project(await resp.json(), host, key), "ok"
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
