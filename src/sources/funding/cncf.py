"""Fetch CNCF landscape projects from the official landscape.yml.

Note: CNCF is hosted under the Linux Foundation. We tag CNCF projects
separately because that's the most specific (and the more useful) host
when classifying a repo. The matcher prefers `cncf` over `lf` on overlap.

Source: https://github.com/cncf/landscape (landscape.yml)
Output: data/sources/funding/cncf/projects.csv

Only rows with `maturity` set (sandbox / incubating / graduated / archived)
are kept — those are actual CNCF projects, not just landscape entries.

Usage:
    uv run python -m src.sources.funding.cncf
"""

from urllib.parse import urlparse

import httpx
import yaml

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

SLUG = "cncf"
SOURCE = "https://raw.githubusercontent.com/cncf/landscape/master/landscape.yml"
COLS = ["name", "domain", "homepage_url", "github_repo",
        "ecosystem", "package", "repo_url",
        "maturity", "category", "subcategory", "description", "accepted"]


def fetch() -> list[dict]:
    resp = httpx.get(SOURCE, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = yaml.safe_load(resp.text)

    rows: list[dict] = []
    for category in data.get("landscape") or []:
        cat_name = category.get("name", "")
        for subcat in category.get("subcategories") or []:
            subcat_name = subcat.get("name", "")
            for item in subcat.get("items") or []:
                maturity = item.get("project") or ""
                if not maturity:
                    continue  # landscape-only entries (vendors etc.) — skip
                homepage = (item.get("homepage_url") or "").strip()
                repo_url = (item.get("repo_url") or "").strip()
                try:
                    domain = urlparse(homepage).netloc.lower().removeprefix("www.")
                except Exception:
                    domain = ""
                extra = item.get("extra") or {}
                desc = (item.get("description") or "")[:200]
                eco, pkg = extract_package(homepage, repo_url, desc)
                rows.append({
                    "name": item.get("name", ""),
                    "domain": domain,
                    "homepage_url": homepage,
                    "github_repo": github_slug(repo_url, homepage, desc),
                    "ecosystem": eco,
                    "package": pkg,
                    "repo_url": repo_url,
                    "maturity": maturity,
                    "category": cat_name,
                    "subcategory": subcat_name,
                    "description": desc,
                    "accepted": extra.get("accepted") or "",
                })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def main() -> None:
    args = parse_scraper_args("Fetch Cloud Native Computing Foundation projects.")
    banner(SLUG, "Cloud Native Computing Foundation", f"Fetching {SOURCE}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path, group_by="maturity")


if __name__ == "__main__":
    main()
