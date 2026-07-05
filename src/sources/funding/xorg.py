"""Scrape the X.Org project list.

The X.Org Foundation is a 501(c)(3) (fiscally hosted by the Software Freedom
Conservancy) that stewards the X Window System and its libraries, so an X.Org
project is a legitimate intent/nonprofit funding signal — host = `xorg`.

X.Org develops on gitlab.freedesktop.org/xorg. Its GitHub mirrors live under
the third-party github.com/gitlab-freedesktop-mirrors/<name> org (which ALSO
mirrors non-X.Org freedesktop projects, so that org is NOT an org-prefix rule —
we match by the exact per-project mirror slug from this roster, plus the x.org
domain). Each project's mirror slug is `gitlab-freedesktop-mirrors/<last-path>`
(e.g. xorg/lib/libxcb -> gitlab-freedesktop-mirrors/libxcb).

Output: data/sources/funding/foundations/x-org-foundation.csv

Usage:
    uv run python -m src.sources.funding.xorg
"""

import httpx

from src.sources.funding._common import (
    USER_AGENT,
    banner,
    is_output_fresh,
    parse_scraper_args,
    summary_table,
    write_projects,
)

SLUG = "xorg"
API = "https://gitlab.freedesktop.org/api/v4/groups/xorg/projects"
MIRROR_ORG = "gitlab-freedesktop-mirrors"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]


def fetch() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30,
                      follow_redirects=True) as client:
        for page in range(1, 21):  # safety cap (~2000 projects)
            resp = client.get(API, params={
                "per_page": 100, "page": page, "include_subgroups": "true",
                "archived": "false", "simple": "true"})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for p in batch:
                path = (p.get("path") or "").strip()  # e.g. libxcb
                if not path or path in seen:
                    continue
                seen.add(path)
                rows.append({
                    "name": p.get("name") or path,
                    "project_slug": path,
                    "domain": "x.org",
                    "website": p.get("web_url") or "",
                    "github_repo": f"{MIRROR_ORG}/{path}",  # flattened GH mirror
                    "ecosystem": "",
                    "package": "",
                    "description": " ".join((p.get("description") or "").split())[:200],
                })
    rows.sort(key=lambda r: r["project_slug"].lower())
    return rows


def main() -> None:
    args = parse_scraper_args("Scrape the X.Org project list (X.Org Foundation).")
    banner(SLUG, "X.Org Foundation", f"Scraping {API}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
