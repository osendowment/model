"""GitLab API interaction — multi-instance async HTTP with per-host tokens.

Mirrors src/sources/github/github_client.py, but keyed per GitLab host: every
self-hosted instance (salsa.debian.org, invent.kde.org, gitlab.gnome.org, …)
has its own base URL, token, and rate-limit budget. The API surface is
identical across instances (`/api/v4`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from urllib.parse import quote, urlsplit

import aiohttp
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Curated seed of GitLab instances present in value.csv. `is_gitlab_host` also
# treats any `gitlab.*` host as an instance, so this list need not be exhaustive.
KNOWN_GITLAB_HOSTS: set[str] = {
    "gitlab.com", "salsa.debian.org", "invent.kde.org", "code.videolan.org",
}


def is_gitlab_host(host: str) -> bool:
    """True if `host` is a GitLab instance (curated seed OR a gitlab.* host)."""
    h = (host or "").lower()
    return h in KNOWN_GITLAB_HOSTS or h.startswith("gitlab.")


def parse_git_url(git_url: str) -> tuple[str, str] | None:
    """Split a clone URL into (host, path) if it points at a GitLab instance.

    Strips scheme, a trailing `.git`, a trailing slash, and any GitLab web
    suffix (`/-/tree/...`). Returns None for non-GitLab hosts or unparseable URLs.
    """
    if not git_url:
        return None
    u = git_url.strip()
    # Normalise scheme so urlsplit sees a netloc.
    if u.startswith(("git://", "git+https://", "git+http://")):
        u = "https://" + u.split("://", 1)[1]
    parts = urlsplit(u if "://" in u else "https://" + u)
    host = parts.netloc.lower()
    if not host or not is_gitlab_host(host):
        return None
    path = parts.path.strip("/")
    if "/-/" in path:                       # drop GitLab web suffixes
        path = path.split("/-/", 1)[0]
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path or "/" not in path:
        return None
    return host, path


def encode_project_path(path: str) -> str:
    """URL-encode a namespace/path for `GET /projects/:id` (slashes → %2F)."""
    return quote(path, safe="")


def api_base(host: str) -> str:
    return f"https://{host}/api/v4"


def clone_url(host: str, path: str) -> str:
    return f"https://{host}/{path}.git"


def make_repo_id(host: str, project_id: int | str) -> str:
    """Host-qualified unified id — gl/{host}/{project_id}."""
    return f"gl/{host}/{project_id}"


def load_token_map() -> dict[str, str]:
    """Per-host GitLab tokens.

    Precedence per host: `GITLAB_TOKENS` (JSON `{host: token}`) →
    `GITLAB_TOKEN_<HOST_SLUG>` (host with dots→underscores, upper) → `GITLAB_TOKEN`
    (applied to every known host as a default). Missing → anonymous for that host.
    """
    load_dotenv()
    out: dict[str, str] = {}
    default = os.environ.get("GITLAB_TOKEN", "").strip()
    if default:
        for h in KNOWN_GITLAB_HOSTS:
            out[h] = default
    for host in list(KNOWN_GITLAB_HOSTS):
        slug = host.upper().replace(".", "_").replace("-", "_")
        val = os.environ.get(f"GITLAB_TOKEN_{slug}", "").strip()
        if val:
            out[host] = val
    raw = os.environ.get("GITLAB_TOKENS", "").strip()
    if raw:
        try:
            for h, t in json.loads(raw).items():
                if t:
                    out[h.lower()] = t
        except (ValueError, AttributeError):
            log.warning("GITLAB_TOKENS is not valid JSON — ignoring")
    return out
