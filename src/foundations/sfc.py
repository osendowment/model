"""Scrape Software Freedom Conservancy member projects.

SFC is a fiscal sponsor — it holds trademarks and assets for projects like
Git, Inkscape, BusyBox, Wine. The site has no API, so we parse the
projects listing HTML.

Output: data/sources/foundations/sfc/projects.csv

Usage:
    uv run python -m src.foundations.sfc
"""

import argparse
import re
from urllib.parse import urlparse

import httpx

from src.foundations._common import (BROWSER_UA, atomic_write, banner,
                                     extract_package, github_slug, out_path,
                                     summary_table)

SLUG = "sfc"
LISTING_URL = "https://sfconservancy.org/projects/current/"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fetch() -> list[dict]:
    resp = httpx.get(
        LISTING_URL,
        headers={"User-Agent": BROWSER_UA},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.text

    h2 = list(re.finditer(
        r'<h2>\s*<a\s+href="([^"]*)"[^>]*>([^<]+)</a>\s*</h2>', html))

    rows: list[dict] = []
    for i, m in enumerate(h2):
        chunk = html[m.end():(h2[i + 1].start() if i + 1 < len(h2) else len(html))]
        url, name = m.group(1).strip(), m.group(2).strip()

        website, domain = "", ""
        if url and not url.startswith("/") and "sfconservancy.org" not in url:
            website = url
            try:
                domain = urlparse(url).netloc.lower().removeprefix("www.")
            except Exception:
                pass

        # Best-effort: pull a github link out of the project description block.
        gh_m = re.search(r'href="(https://github\.com/[^"/]+/[^"/]+)"', chunk)
        gh_url = gh_m.group(1) if gh_m else ""

        desc_m = re.search(r"<p>(.*?)</p>", chunk, re.DOTALL)
        desc = ""
        if desc_m:
            desc = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip()
            desc = re.sub(r"\s+", " ", desc)[:200]

        eco, pkg = extract_package(website, gh_url, chunk)
        rows.append({
            "name": name,
            "project_slug": _slugify(name),
            "domain": domain,
            "website": website,
            "github_repo": github_slug(gh_url, website, chunk),
            "ecosystem": eco,
            "package": pkg,
            "description": desc,
        })
    rows.sort(key=lambda r: r["project_slug"])
    return rows


def main() -> None:
    argparse.ArgumentParser().parse_args()
    banner(SLUG, "Software Freedom Conservancy", f"Scraping {LISTING_URL}")
    rows = fetch()
    path = out_path(SLUG)
    atomic_write(path, rows, COLS)
    summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
