"""Fetch projects under the Python Software Foundation umbrella.

There's no single "PSF projects" landscape — the PSF acts as fiscal host
and trademark holder for several distinct project groups. We pull the two
that matter most for this corpus:

  • PyPA — Python Packaging Authority (`github.com/pypa/*`):
    pip, setuptools, packaging, virtualenv, build, twine, wheel, etc.
    Source: GitHub `/orgs/pypa/repos` (paginated).

  • Python core (`github.com/python/*`): cpython, peps, mypy, etc.
    Source: GitHub `/orgs/python/repos`.

Output: data/sources/funding/foundations/python-software-foundation.csv

Authenticated with the same token revolver as the other scrapers
(`src.sources.github.github_client`). Unauthenticated runs work but burn the 60
req/hr anonymous budget — fine for the ~5 paginated calls we need.

Usage:
    uv run python -m src.sources.funding.psf
"""

import httpx

from src.sources.funding._common import (
    USER_AGENT,
    banner,
    extract_package,
    github_slug,
    is_output_fresh,
    parse_scraper_args,
    summary_table,
    write_projects,
)
from src.sources.github.github_client import get_revolver

SLUG = "psf"
ORGS = ["pypa", "python"]
COLS = ["name", "github_repo", "ecosystem", "package", "homepage",
        "default_branch", "license", "language", "topics",
        "stars", "archived", "created_at", "pushed_at",
        "description", "category"]


def _fetch_org_repos(org: str) -> list[dict]:
    revolver = get_revolver()
    out: list[dict] = []
    page = 1
    while True:
        token = revolver.best_token()
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"https://api.github.com/orgs/{org}/repos"
        resp = httpx.get(url, params={"per_page": 100, "page": page,
                                      "type": "public"},
                         headers=headers, timeout=30)
        if token:
            rem = resp.headers.get("X-RateLimit-Remaining")
            rst = resp.headers.get("X-RateLimit-Reset")
            revolver.update(token, int(rem) if rem else None,
                            float(rst) if rst else None)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def fetch() -> list[dict]:
    rows: list[dict] = []
    for org in ORGS:
        repos = _fetch_org_repos(org)
        print(f"  {org:8} → {len(repos):,} repos")
        for r in repos:
            if r.get("fork"):
                # Skip forks — PSF/PyPA fork plenty of upstreams for tooling.
                continue
            html = r.get("html_url") or ""
            homepage = r.get("homepage") or ""
            desc = (r.get("description") or "")[:200]
            lic = (r.get("license") or {}) if isinstance(r.get("license"), dict) else {}
            eco, pkg = extract_package(homepage, desc)
            rows.append({
                "name": r.get("name", ""),
                "github_repo": github_slug(html, homepage),
                "ecosystem": eco,
                "package": pkg,
                "homepage": homepage,
                "default_branch": r.get("default_branch", ""),
                "license": lic.get("spdx_id") or lic.get("key") or "",
                "language": r.get("language") or "",
                "topics": " | ".join(r.get("topics") or []),
                "stars": r.get("stargazers_count", ""),
                "archived": r.get("archived", ""),
                "created_at": r.get("created_at", ""),
                "pushed_at": r.get("pushed_at", ""),
                "description": desc,
                "category": org,
            })
    rows.sort(key=lambda x: (x["category"], x["name"].lower()))
    return rows


def main() -> None:
    args = parse_scraper_args("Fetch Python Software Foundation projects.")
    banner(SLUG, "Python Software Foundation",
           f"Fetching github orgs: {', '.join(ORGS)}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path, group_by="category")


if __name__ == "__main__":
    main()
