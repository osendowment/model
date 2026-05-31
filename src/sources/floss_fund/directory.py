"""Match repos against the FLOSS Fund directory export.

The export (`data/sources/floss-fund/funding-json.csv`, produced by
`src.sources.floss_fund.funding_json`) lists every registered manifest with a
`project_repository` URL. A risk-scope repo "has funding.json" iff its
`owner/repo` appears here — derived, no per-repo fetch.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

_GH_RE = re.compile(r"github\.com[/:]+([^/]+/[^/]+)")


def normalize_github_repo(url: str | None) -> str | None:
    """`https://github.com/Owner/Repo.git/` → `owner/repo`; non-github → None."""
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    m = _GH_RE.search(u)
    return m.group(1) if m else None


def load_directory_repos(path: Path | str) -> set[str]:
    """Set of normalized `owner/repo` slugs from the export's `project_repository`."""
    out: set[str] = set()
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = normalize_github_repo(row.get("project_repository"))
            if slug:
                out.add(slug)
    return out
