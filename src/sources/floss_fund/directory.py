"""Match repos against the FLOSS Fund directory export.

The export (`data/sources/floss-fund/funding-json.csv`, produced by
`src.sources.floss_fund.funding_json`) lists every registered manifest with a
`project_repository` URL. A risk-scope repo "has funding.json" iff its
`owner/repo` appears here — derived, no per-repo fetch.

A manifest's repo URL may be a redirect to GitHub (e.g.
`tukaani.org/xz/redirect-to-github-xz` → `github.com/tukaani-project/xz`); the
fetcher resolves those into a `project_repository_resolved` column, which
`export_repo_slug` prefers over the raw URL. A manifest may also name a GitHub
*org page* (`github.com/<org>`) rather than a specific repo — `github_org_page`
extracts the org, whose funding the builder applies to every repo it owns.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

_GH_RE = re.compile(r"github\.com[/:]+([^/]+/[^/]+)")
_GH_ORG_RE = re.compile(r"github\.com[/:]+([^/]+)$")

# GitHub paths that look like `github.com/<seg>` but are not an org/user page.
_GH_RESERVED = {
    "sponsors", "orgs", "about", "marketplace", "topics", "collections",
    "apps", "settings", "features", "pricing", "explore", "search",
}


def normalize_github_repo(url: str | None) -> str | None:
    """`https://github.com/Owner/Repo.git/` → `owner/repo`; non-github → None."""
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    m = _GH_RE.search(u)
    return m.group(1) if m else None


def github_org_page(url: str | None) -> str | None:
    """A GitHub org/user page URL (`github.com/<org>`, no repo) → `<org>` (lower).

    Returns None for a specific repo (`github.com/o/r`), a non-GitHub URL, or a
    reserved GitHub path (`github.com/sponsors`). Used to detect org-level FLOSS
    Fund manifests whose funding applies to every repo under that owner.
    """
    if normalize_github_repo(url):  # a specific repo, not an org page
        return None
    u = (url or "").strip().lower()
    u = re.split(r"[?#]", u, maxsplit=1)[0].rstrip("/")
    u = re.sub(r"\.git$", "", u)
    m = _GH_ORG_RE.search(u)
    if not m:
        return None
    org = m.group(1)
    return org if org and org not in _GH_RESERVED else None


def export_repo_slug(row: dict) -> str | None:
    """Repo slug for a FLOSS export row — resolved redirect URL wins over raw.

    `project_repository_resolved` (a redirect that lands on a GitHub repo, set by
    the fetcher) is preferred; falls back to the raw `project_repository`. Returns
    None when neither names a GitHub repo (org pages, non-GitHub hosts).
    """
    return (normalize_github_repo(row.get("project_repository_resolved"))
            or normalize_github_repo(row.get("project_repository")))


def normalize_repo_url(url: str | None) -> str:
    """Host-agnostic repo-URL normalizer: lowercased, scheme/`.git`/trailing
    slash stripped — `https://GitLab.com/a/B.git/` → `gitlab.com/a/b`.
    The join key for non-GitHub manifests (GitHub ones join by slug/repo_id)."""
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"^(https?://|git://)", "", u)
    u = re.sub(r"\.git$", "", u)
    return u


def export_repo_url(row: dict) -> str:
    """Normalized repository URL for a FLOSS export row (resolved wins), "" if
    the row names no repository. Complements `export_repo_slug` for manifests
    hosted outside GitHub (GitLab instances, custom hosts)."""
    return (normalize_repo_url(row.get("project_repository_resolved"))
            or normalize_repo_url(row.get("project_repository")))


def load_directory_repos(path: Path | str) -> set[str]:
    """Set of normalized `owner/repo` slugs from the export (resolved-or-raw)."""
    out: set[str] = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = export_repo_slug(row)
            if slug:
                out.add(slug)
    return out
