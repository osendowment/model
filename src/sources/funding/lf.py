"""Fetch Linux Foundation landscape projects.

Source: https://github.com/jmertic/lf-landscape (landscape.yml)
Output: data/sources/funding/foundations/linux-foundation.csv

Note: CNCF projects also appear here (CNCF is hosted under LF). The
matcher prefers the more specific `cncf` slug when a repo matches both.

Usage:
    uv run python -m src.sources.funding.lf
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

SLUG = "lf"
SOURCE = "https://raw.githubusercontent.com/jmertic/lf-landscape/main/landscape.yml"
COLS = ["name", "domain", "homepage_url", "github_repo",
        "ecosystem", "package", "repo_url",
        "category", "subcategory", "description", "license", "accepted"]


def fetch() -> list[dict]:
    resp = httpx.get(SOURCE, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = yaml.safe_load(resp.text)

    rows: list[dict] = []
    for category in data.get("landscape") or []:
        cat_name = category.get("name", "")
        # Skip the "LF Members" category — those are companies paying LF dues,
        # not LF-hosted projects. Mixing them in causes false positives like
        # "boto3 → lf" because AWS is an LF member.
        if cat_name == "LF Members":
            continue
        for subcat in category.get("subcategories") or []:
            subcat_name = subcat.get("name", "")
            for item in subcat.get("items") or []:
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
                    "category": cat_name,
                    "subcategory": subcat_name,
                    "description": desc,
                    "license": item.get("license") or "",
                    "accepted": extra.get("accepted") or "",
                })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def main() -> None:
    args = parse_scraper_args("Fetch Linux Foundation landscape projects.")
    banner(SLUG, "Linux Foundation landscape", f"Fetching {SOURCE}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path, group_by="category")


if __name__ == "__main__":
    main()
