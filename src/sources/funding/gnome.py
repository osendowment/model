"""Scrape the GNOME project list.

The GNOME Foundation is a 501(c)(3) that legally stewards the GNOME project
and accepts donations on its behalf, so a GNOME project is a legitimate
intent/nonprofit funding signal — host = `gnome`.

GNOME develops on gitlab.gnome.org and mirrors every project to
github.com/GNOME/<name>, so the GitLab group API is the machine-readable
roster and `path_with_namespace` (`GNOME/pango`) is exactly the GitHub mirror
slug. Attribution also fires via the org-prefix (github.com/GNOME → gnome) and
the gnome.org domain, so a repo need not be in this roster to match.

Output: data/sources/funding/foundations/gnome.csv

Usage:
    uv run python -m src.sources.funding.gnome
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

SLUG = "gnome"
API = "https://gitlab.gnome.org/api/v4/groups/GNOME/projects"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]


def fetch() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=30,
                      follow_redirects=True) as client:
        for page in range(1, 31):  # safety cap (~3000 projects)
            resp = client.get(API, params={
                "per_page": 100, "page": page, "include_subgroups": "true",
                "archived": "false", "simple": "true"})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for p in batch:
                pwn = (p.get("path_with_namespace") or "").strip()  # GNOME/pango
                if not pwn or pwn in seen:
                    continue
                seen.add(pwn)
                rows.append({
                    "name": p.get("name") or p.get("path") or pwn,
                    "project_slug": (p.get("path") or "").strip(),
                    "domain": "gnome.org",
                    "website": p.get("web_url") or "",
                    "github_repo": pwn,   # github.com mirror uses the same slug
                    "ecosystem": "",
                    "package": "",
                    "description": " ".join((p.get("description") or "").split())[:200],
                })
    rows.sort(key=lambda r: r["github_repo"].lower())
    return rows


def main() -> None:
    args = parse_scraper_args("Scrape the GNOME project list (GNOME Foundation).")
    banner(SLUG, "GNOME Foundation", f"Scraping {API}")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
