"""Scrape Software Freedom Conservancy member projects.

SFC is a fiscal sponsor — it holds trademarks and assets for projects like
Git, Inkscape, BusyBox, Wine. The site has no API, so we parse the
projects listing HTML.

Output: data/sources/funding/foundations/software-freedom-conservancy.csv

Usage:
    uv run python -m src.sources.funding.sfc
"""

import re
from urllib.parse import urlparse

import httpx

from src.sources.funding._common import (
    BROWSER_UA,
    banner,
    extract_package,
    github_slug,
    is_output_fresh,
    parse_scraper_args,
    summary_table,
    write_projects,
)

SLUG = "sfc"
LISTING_URL = "https://sfconservancy.org/projects/current/"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]

# Curated `owner/name` for the SFC members whose primary (or the project's own
# official mirror) is GitHub, keyed by their homepage apex domain. Most SFC
# members develop on kernel.org / GitLab / Gerrit / Bitbucket / self-hosted git
# and have NO GitHub repo — those are matched by `domain` instead, so they are
# deliberately absent here. Verified live (2026). Entries marked "mirror" are
# the project's OWN official read-only GitHub mirror (safe to attribute).
GITHUB_BY_DOMAIN: dict[str, str] = {
    "backdropcms.org": "backdrop/backdrop",
    "buildbot.net": "buildbot/buildbot",
    "etherpad.org": "ether/etherpad-lite",
    "gevent.org": "gevent/gevent",
    "harvey-os.github.io": "Harvey-OS/harvey",
    "houdiniproject.org": "houdiniproject/houdini",
    "inkscape.org": "inkscape/inkscape",            # official mirror of GitLab
    "k-3d.org": "K-3D/k3d",
    "liblouis.io": "liblouis/liblouis",
    "opentripplanner.org": "opentripplanner/OpenTripPlanner",
    "phpmyadmin.net": "phpmyadmin/phpmyadmin",
    "seleniumhq.org": "SeleniumHQ/selenium",
    "swig.org": "swig/swig",
    "xapian.org": "xapian/xapian",                  # official mirror
}


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

    # `<h2 ...>` may carry an id (e.g. `<h2 id="busybox">`), so match `<h2[^>]*>`
    # — a bare `<h2>` silently dropped every id-headed member (BusyBox, …).
    h2 = list(re.finditer(
        r'<h2[^>]*>\s*<a\s+href="([^"]*)"[^>]*>([^<]+)</a>\s*</h2>', html))

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

        # github_repo priority: (1) curated overlay by domain (most reliable);
        # (2) the heading href itself when it IS a github repo (Linux XIA,
        # U-Boot point their homepage at github); (3) a github link in the
        # description block.
        github_repo = GITHUB_BY_DOMAIN.get(domain) or github_slug(url, gh_url, chunk)

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
            "github_repo": github_repo,
            "ecosystem": eco,
            "package": pkg,
            "description": desc,
        })
    rows.sort(key=lambda r: r["project_slug"])
    return rows


def main() -> None:
    args = parse_scraper_args("Scrape Software Freedom Conservancy projects.")
    banner(SLUG, "Software Freedom Conservancy", f"Scraping {LISTING_URL}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
