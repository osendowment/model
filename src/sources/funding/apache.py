"""Fetch Apache Software Foundation projects from projects.apache.org.

Source: https://projects.apache.org/json/foundation/projects.json
Output: data/sources/funding/apache/projects.csv

Usage:
    uv run python -m src.sources.funding.apache
"""

from urllib.parse import urlparse

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

SLUG = "apache"
SOURCE = "https://projects.apache.org/json/foundation/projects.json"
COLS = ["name", "shortname", "domain", "homepage", "github_repo",
        "ecosystem", "package", "repo_url",
        "category", "programming_language", "description", "created", "license"]


def fetch() -> list[dict]:
    resp = httpx.get(SOURCE, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    rows: list[dict] = []
    for shortname, proj in data.items():
        homepage = (proj.get("homepage") or "").strip()
        try:
            domain = urlparse(homepage).netloc.lower().removeprefix("www.")
        except Exception:
            domain = ""
        # Apache "repository" entries are usually URL strings but a handful
        # are dicts like {"browse": "..."} — normalise to a single URL string.
        repos = proj.get("repository") or []
        repo_url = ""
        for entry in repos:
            if isinstance(entry, str):
                repo_url = entry
                break
            if isinstance(entry, dict):
                repo_url = entry.get("browse") or entry.get("url") or ""
                if repo_url:
                    break
        desc = (proj.get("description") or "")[:200]
        eco, pkg = extract_package(homepage, repo_url, desc)
        rows.append({
            "name": proj.get("name", ""),
            "shortname": shortname,
            "domain": domain,
            "homepage": homepage,
            "github_repo": github_slug(repo_url, homepage, desc),
            "ecosystem": eco,
            "package": pkg,
            "repo_url": repo_url,
            "category": proj.get("category", ""),
            "programming_language": " | ".join(proj.get("programming-language", []) or []),
            "description": desc,
            "created": proj.get("created", ""),
            "license": " | ".join(proj.get("license", []) or []),
        })
    rows.sort(key=lambda r: r["shortname"])
    return rows


def main() -> None:
    args = parse_scraper_args("Fetch Apache Software Foundation projects.")
    banner(SLUG, "Apache Software Foundation", f"Fetching {SOURCE}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
