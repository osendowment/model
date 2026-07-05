"""Scrape the GNU Project package list.

Every GNU package is part of the GNU Project and stewarded by the Free
Software Foundation (a 501(c)(3)); the FSF holds copyright assignments for
most GNU packages and accepts donations earmarked for GNU. So a GNU package
is a legitimate `intent`/`nonprofit` funding signal — host = `gnu` (parent
`fsf`).

Source: https://www.gnu.org/manual/blurbs.html — one entry per currently
maintained GNU package (~355), plain static HTML, keyed off the
`/software/<slug>/` link. Homepage + Savannah URLs are deterministic from the
slug; a curated dict overlays the handful of packages with an *authoritative*
GitHub mirror (autotools-mirror/*, coreutils/*, mirror/*, gcc-mirror/gcc,
bminor/glibc). We NEVER guess a mirror slug — packages with only third-party
forks get `github_repo=""` and match by the gnu.org domain instead.

Output: data/sources/funding/foundations/gnu-project.csv

Usage:
    uv run python -m src.sources.funding.gnu
"""

import re
from urllib.parse import urlparse

import httpx

from src.sources.funding._common import (
    BROWSER_UA,
    banner,
    is_output_fresh,
    parse_scraper_args,
    summary_table,
    write_projects,
)

SLUG = "gnu"
LISTING_URL = "https://www.gnu.org/manual/blurbs.html"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]

# Authoritative GitHub mirrors, verified live (HTTP 200). Only packages whose
# canonical dev home is git.savannah.gnu.org / sourceware but which have a
# maintained, trustworthy GitHub mirror. Keyed by the blurbs `/software/<slug>/`
# slug. Everything else has only volunteer forks → left blank on purpose.
GITHUB_MIRRORS: dict[str, str] = {
    "gcc": "gcc-mirror/gcc",
    "libc": "bminor/glibc",  # GNU C Library — its blurbs/software slug is "libc"
    "autoconf": "autotools-mirror/autoconf",
    "automake": "autotools-mirror/automake",
    "libtool": "autotools-mirror/libtool",
    "m4": "autotools-mirror/m4",
    "gettext": "autotools-mirror/gettext",
    "gnulib": "coreutils/gnulib",
    "coreutils": "coreutils/coreutils",
    "bash": "mirror/bash",
    "make": "mirror/make",
    "sed": "mirror/sed",
    "tar": "mirror/tar",
    "wget": "mirror/wget",
    "patch": "mirror/patch",
    "gnutls": "gnutls/gnutls",
}

# <h4 id="slug"> … <a href="/software/slug/">Name</a></h4><p>blurb…</p>
_ENTRY_RE = re.compile(
    r'<h4\s+id="(?P<slug>[^"]+)">.*?'
    r'<a\s+href="/software/(?P=slug)/"[^>]*>(?P<name>[^<]+)</a>\s*</h4>'
    r'\s*<p>(?P<blurb>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def _clean_blurb(html: str) -> str:
    # Drop the trailing "(doc)" small-link, strip tags, collapse whitespace.
    html = re.sub(r"<small>.*?</small>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", text).strip()[:200]


def fetch() -> list[dict]:
    resp = httpx.get(
        LISTING_URL,
        headers={"User-Agent": BROWSER_UA},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.text

    rows: list[dict] = []
    seen: set[str] = set()
    for m in _ENTRY_RE.finditer(html):
        slug = m.group("slug").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        website = f"https://www.gnu.org/software/{slug}/"
        rows.append({
            "name": m.group("name").strip(),
            "project_slug": slug,
            "domain": urlparse(website).netloc.lower().removeprefix("www."),
            "website": website,
            "github_repo": GITHUB_MIRRORS.get(slug, ""),
            "ecosystem": "",
            "package": "",
            "description": _clean_blurb(m.group("blurb")),
        })
    rows.sort(key=lambda r: r["project_slug"])
    return rows


def main() -> None:
    args = parse_scraper_args("Scrape the GNU Project package list.")
    banner(SLUG, "GNU Project", f"Scraping {LISTING_URL}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
