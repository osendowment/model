"""Fetch Eclipse Foundation projects from the official Eclipse projects API.

Source: https://projects.eclipse.org/api/projects (paginated JSON)
Output: data/sources/foundations/eclipse/projects.csv

Usage:
    uv run python -m src.foundations.eclipse
"""

import argparse
from urllib.parse import urlparse

import httpx

from src.foundations._common import (USER_AGENT, atomic_write, banner,
                                     extract_package, github_slug, out_path,
                                     summary_table)

SLUG = "eclipse"
SOURCE = "https://projects.eclipse.org/api/projects"
COLS = ["project_id", "name", "domain", "github_repo",
        "ecosystem", "package", "homepage",
        "description", "state", "license"]
PAGE_SIZE = 100
MAX_PAGES = 30  # safety net — Eclipse currently has ~12 pages


def fetch() -> list[dict]:
    projects: list[dict] = []
    for page in range(MAX_PAGES):
        resp = httpx.get(
            SOURCE,
            params={"page": page, "pagesize": PAGE_SIZE},
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        projects.extend(batch)

    rows: list[dict] = []
    for p in projects:
        homepage = (p.get("website_url") or "").strip()
        try:
            domain = urlparse(homepage).netloc.lower().removeprefix("www.")
        except Exception:
            domain = ""
        repos = p.get("github_repos") or []
        repo_url = repos[0].get("url", "") if repos else ""
        licenses = p.get("licenses") or []
        lic = licenses[0].get("name", "") if licenses else ""
        desc = (p.get("summary") or "")[:200]
        eco, pkg = extract_package(homepage, repo_url, desc)
        rows.append({
            "project_id": p.get("project_id", ""),
            "name": p.get("name", ""),
            "domain": domain,
            "github_repo": github_slug(repo_url, homepage, desc),
            "ecosystem": eco,
            "package": pkg,
            "homepage": homepage,
            "description": desc,
            "state": p.get("state", ""),
            "license": lic,
        })
    rows.sort(key=lambda r: r["project_id"])
    return rows


def main() -> None:
    argparse.ArgumentParser().parse_args()
    banner(SLUG, "Eclipse Foundation", f"Fetching {SOURCE}")
    rows = fetch()
    path = out_path(SLUG)
    atomic_write(path, rows, COLS)
    summary_table(SLUG, rows, path, group_by="state")


if __name__ == "__main__":
    main()
