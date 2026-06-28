"""Fetch OpenJS Foundation projects.

OpenJS doesn't publish a landscape.yml. The canonical project roster
lives in the Cross-Project Council README as a markdown table grouped by
stage (Impact / At-Large / Incubation / Emeritus).

Source: https://raw.githubusercontent.com/openjs-foundation/cross-project-council/main/README.md
Output: data/sources/funding/openjs/projects.csv

Note: OpenJS is hosted under the Linux Foundation, so its projects also
appear in the LF landscape. We tag them as `openjs` (more specific) and
the matcher's priority order ensures OpenJS wins over `lf` on overlap.

Usage:
    uv run python -m src.sources.funding.openjs
"""

import argparse
import re
from urllib.parse import urlparse

import httpx

from src.sources.funding._common import (USER_AGENT, atomic_write, banner,
                                     extract_package, github_slug, out_path,
                                     summary_table)

SLUG = "openjs"
SOURCE = ("https://raw.githubusercontent.com/openjs-foundation"
          "/cross-project-council/main/README.md")
COLS = ["name", "stage", "domain", "homepage", "github_repo",
        "ecosystem", "package", "description"]

# Section headers in the README that introduce a project table.
# Stage names match OpenJS's official progression.
_STAGE_HEADERS = re.compile(
    r"^####?\s*(Impact|At[-\s]Large|Incubation|Incubating|Emeritus)\s*Projects?",
    re.MULTILINE | re.IGNORECASE,
)

# Match a markdown table row whose second cell is `[Name](url)`. We allow
# leading-image cells because the table uses a logo column first.
_ROW = re.compile(
    r"^\|[^|\n]*\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|(.*?)\|\s*$",
    re.MULTILINE,
)


def _slug_stage(label: str) -> str:
    norm = re.sub(r"\s+", "-", label.strip().lower())
    if norm == "incubating":
        norm = "incubation"
    return norm


def fetch() -> list[dict]:
    resp = httpx.get(SOURCE, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    text = resp.text

    # Find each stage section and slice the README between consecutive headers
    # so we can tag every row with its stage.
    headers = list(_STAGE_HEADERS.finditer(text))
    if not headers:
        return []

    rows: list[dict] = []
    seen: set[str] = set()
    for i, h in enumerate(headers):
        stage = _slug_stage(h.group(1))
        section = text[h.end(): headers[i + 1].start() if i + 1 < len(headers) else len(text)]

        for m in _ROW.finditer(section):
            name = m.group(1).strip()
            homepage = m.group(2).strip()
            tail = m.group(3) or ""
            if name.lower() in {"project name", "name", "project"}:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)

            try:
                domain = urlparse(homepage).netloc.lower().removeprefix("www.")
            except Exception:
                domain = ""

            # Pull a github repo from the row body — many entries link to
            # `github.com/owner/repo/...` in the Charter / Contributing cells.
            gh = github_slug(homepage, tail)
            eco, pkg = extract_package(homepage, tail)

            rows.append({
                "name": name,
                "stage": stage,
                "domain": domain if domain != "github.com" else "",
                "homepage": homepage,
                "github_repo": gh,
                "ecosystem": eco,
                "package": pkg,
                "description": "",
            })
    rows.sort(key=lambda r: (r["stage"], r["name"].lower()))
    return rows


def main() -> None:
    argparse.ArgumentParser().parse_args()
    banner(SLUG, "OpenJS Foundation", f"Fetching {SOURCE}")
    rows = fetch()
    path = out_path(SLUG)
    atomic_write(path, rows, COLS)
    summary_table(SLUG, rows, path, group_by="stage")


if __name__ == "__main__":
    main()
