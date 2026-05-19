"""Scrape NumFOCUS sponsored projects from numfocus.org.

Two-step scrape: (1) project listing → slugs; (2) per-project page →
external website + GitHub link. NumFOCUS doesn't expose a JSON API, so
this is regex-based HTML scraping. Small list (~60 projects) so it runs
in a few seconds.

Output: data/foundations/numfocus/projects.csv

Usage:
    uv run python -m src.foundations.numfocus
"""

import argparse
import re
from urllib.parse import urlparse

import httpx

from src.foundations._common import (BROWSER_UA, atomic_write, banner,
                                     extract_package, github_slug, out_path,
                                     summary_table)

SLUG = "numfocus"
LISTING_URL = "https://numfocus.org/sponsored-projects"
PROJECT_URL = "https://numfocus.org/project/{slug}"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package"]


def fetch() -> list[dict]:
    client = httpx.Client(
        headers={"User-Agent": BROWSER_UA},
        timeout=30,
        follow_redirects=True,
    )
    resp = client.get(LISTING_URL)
    resp.raise_for_status()

    project_links = re.findall(
        r'href="https://numfocus\.org/project/([^"]+)"[^>]*>([^<]+)</a>',
        resp.text, re.IGNORECASE,
    )

    seen: set[str] = set()
    rows: list[dict] = []
    for slug, name in project_links:
        name = name.strip()
        if not name or name in seen or len(name) < 2:
            continue
        seen.add(name)

        website, gh, html = "", "", ""
        try:
            r = client.get(PROJECT_URL.format(slug=slug))
            html = r.text
            m = re.search(
                r'href="(https?://(?!numfocus\.org)[^"]+)"[^>]*>\s*'
                r'(?:Visit\s+)?(?:Website|Project\s+Site|Home)',
                html, re.IGNORECASE,
            )
            website = m.group(1) if m else ""
            if not website:
                m = re.search(
                    r'href="(https?://(?!numfocus\.org|twitter|facebook'
                    r'|github\.com/numfocus)[^"]+)"',
                    html,
                )
                website = m.group(1) if m else ""
            gh_m = re.search(r'href="(https://github\.com/[^"/]+/[^"/]+)"', html)
            gh = gh_m.group(1) if gh_m else ""
        except Exception:
            pass

        try:
            domain = urlparse(website).netloc.lower().removeprefix("www.") if website else ""
        except Exception:
            domain = ""

        eco, pkg = extract_package(website, html)
        rows.append({
            "name": name,
            "project_slug": slug,
            "domain": domain,
            "website": website,
            "github_repo": github_slug(gh, website, html),
            "ecosystem": eco,
            "package": pkg,
        })
    rows.sort(key=lambda r: r["project_slug"])
    return rows


def main() -> None:
    argparse.ArgumentParser().parse_args()
    banner(SLUG, "NumFOCUS sponsored projects", f"Scraping {LISTING_URL}")
    rows = fetch()
    path = out_path(SLUG)
    atomic_write(path, rows, COLS)
    summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
