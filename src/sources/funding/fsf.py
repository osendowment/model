"""Free Software Foundation roster — deliberately a small curated list.

Unlike the other foundations, the FSF has **no scrapeable repo-level project
list** that maps to funding attribution:

- The High Priority Projects page (fsf.org/campaigns/priority-projects/) lists
  ~11 thematic *areas* (free phone OS, decentralization, accessibility, …) with
  NO project homepages or repos — zero attributable repos.
- The Free Software Directory (directory.fsf.org) is a *catalog* of thousands of
  third-party projects the FSF merely lists, NOT funds — ingesting it wholesale
  would create false funding attribution, so it is intentionally excluded.

What the FSF actually stewards and funds is **GNU** — every GNU package is an
FSF project (copyright-assigned, donations earmarked). GNU is tracked in its own
roster (`gnu.py`, emitted as host `fsf/gnu`), which is where essentially all
FSF-funded software lands.

This file therefore records only the FSF's OWN software / sites, matched by the
`fsf.org` domain (they self-host on vcs.fsf.org, so there are ~no GitHub repos
to attribute). Its match yield against a library-heavy risk scope is ~0 by
design — the value of the FSF relationship flows through the GNU roster. It
exists so the FSF host is represented explicitly and auditable.

NOTE: `github.com/FSFE` is the Free Software Foundation *Europe*, a legally
separate sister organisation — deliberately NOT attributed here.

Output: data/sources/funding/foundations/free-software-foundation.csv

Usage:
    uv run python -m src.sources.funding.fsf
"""

from src.sources.funding._common import (
    banner,
    is_output_fresh,
    parse_scraper_args,
    summary_table,
    write_projects,
)

SLUG = "fsf"
COLS = ["name", "project_slug", "domain", "website", "github_repo",
        "ecosystem", "package", "description"]

# The FSF's own software / operated sites. Matched by domain (fsf.org suffix);
# no GitHub repos — the FSF self-hosts on vcs.fsf.org. Substantive FSF-funded
# software is GNU (see gnu.py). Kept short and curated on purpose.
PROJECTS: list[dict] = [
    {
        "name": "Free Software Foundation",
        "project_slug": "fsf",
        "domain": "fsf.org",
        "website": "https://www.fsf.org/",
        "github_repo": "",
        "ecosystem": "",
        "package": "",
        "description": "501(c)(3) that sponsors the GNU Project and funds free "
                       "software; holds GNU copyright assignments.",
    },
    {
        "name": "Email Self-Defense (Edward)",
        "project_slug": "email-self-defense",
        "domain": "emailselfdefense.fsf.org",
        "website": "https://emailselfdefense.fsf.org/",
        "github_repo": "",
        "ecosystem": "",
        "package": "",
        "description": "FSF-run GnuPG email-encryption guide and Edward reply "
                       "bot (AGPL, hosted on vcs.fsf.org).",
    },
    {
        "name": "Free Software Directory",
        "project_slug": "free-software-directory",
        "domain": "directory.fsf.org",
        "website": "https://directory.fsf.org/",
        "github_repo": "",
        "ecosystem": "",
        "package": "",
        "description": "FSF-operated catalog of free software (the platform is "
                       "FSF's; the catalog entries are not FSF projects).",
    },
    {
        "name": "Respects Your Freedom",
        "project_slug": "respects-your-freedom",
        "domain": "ryf.fsf.org",
        "website": "https://ryf.fsf.org/",
        "github_repo": "",
        "ecosystem": "",
        "package": "",
        "description": "FSF hardware-certification program for devices that "
                       "respect users' freedom.",
    },
]


def fetch() -> list[dict]:
    """Return the curated FSF roster (no network — there is no list to scrape)."""
    return sorted(PROJECTS, key=lambda r: r["project_slug"])


def main() -> None:
    args = parse_scraper_args("Emit the curated Free Software Foundation roster.")
    banner(SLUG, "Free Software Foundation",
           "Curated roster (FSF has no scrapeable repo list — see module docstring)")
    if is_output_fresh(SLUG, args):
        return
    rows = fetch()
    path, written = write_projects(SLUG, rows, COLS, force=args.force)
    if written:
        summary_table(SLUG, rows, path)


if __name__ == "__main__":
    main()
