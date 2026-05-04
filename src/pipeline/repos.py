"""Shared repo loaders for the risk pipeline.

Source of truth: `data/value-data.csv` (A/B class only, non-empty github_repo).
Optional enrichment from `data/github/search/top-repos.csv` for `size_kb`,
`repo_id`, `archived`, and `stars` — used by the git and contributors fetchers
for sparse-vs-tarball decisions and stable sorting.

A/B is defined as the rolled-up `class` column in value-data.csv.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

VALUE_FILE = "data/value-data.csv"
ELIGIBILITY_FILE = "data/eligibility-data.csv"
TOP_REPOS_FILE = "data/github/search/top-repos.csv"

# Class precedence — A beats B if a repo has multiple classifications.
_RANK = {"A": 2, "B": 1}


@dataclass
class RepoEntry:
    """One eligible repo with optional enrichment from top-repos.csv."""
    repo: str
    value_class: str = ""
    repo_id: str = ""
    size_kb: int = 0
    stars: int = 0
    archived: bool = False
    enriched: bool = False  # True iff metadata came from top-repos.csv


def _load_top_repos_meta(path: str) -> dict[str, RepoEntry]:
    out: dict[str, RepoEntry] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row["repo"].strip().lower()
            out[slug] = RepoEntry(
                repo=slug,
                repo_id=row.get("repo_id", "") or "",
                size_kb=int(row.get("size") or 0),
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
            )
    return out


def load_ab_repos(
    value_file: str = VALUE_FILE,
    top_repos_file: str = TOP_REPOS_FILE,
    skip_archived: bool = True,
) -> list[RepoEntry]:
    """Return A/B-class repos from value-data.csv, sorted by slug.

    - Filters: `class` in {A, B}, non-empty `github_repo`, optionally not archived.
    - Deduped by lowercased `github_repo`; keeps the highest class (A beats B).
    - Repos missing from top-repos.csv are returned with `enriched=False` and
      default metadata (size_kb=0, archived=False); those still get processed.
    """
    chosen: dict[str, str] = {}
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cls = (row.get("class") or "").strip()
            if cls not in _RANK:
                continue
            slug = (row.get("github_repo") or "").strip().lower()
            if not slug:
                continue
            if slug not in chosen or _RANK[cls] > _RANK[chosen[slug]]:
                chosen[slug] = cls

    meta = _load_top_repos_meta(top_repos_file)
    entries: list[RepoEntry] = []
    skipped_archived = 0
    for slug, cls in chosen.items():
        e = meta.get(slug) or RepoEntry(repo=slug)
        e.repo = slug
        e.value_class = cls
        if skip_archived and e.archived:
            skipped_archived += 1
            continue
        entries.append(e)

    if skipped_archived:
        log.info("Skipped %d archived A/B repos", skipped_archived)
    entries.sort(key=lambda e: e.repo)
    return entries


def load_ab_slugs(*args, **kwargs) -> list[str]:
    """Convenience wrapper returning just the lowercased repo slugs."""
    return [e.repo for e in load_ab_repos(*args, **kwargs)]


def load_eligible_repos(
    eligibility_file: str = ELIGIBILITY_FILE,
) -> list[RepoEntry]:
    """Return repos with `eligibility=True` from `eligibility-data.csv`.

    The risk pipeline runs on this set: licensed-OSS, non-EOL, valid (non-404)
    AB-class repos. Built by `src.pipeline.eligibility`. Repos not present
    or with eligibility != True are excluded.
    """
    if not os.path.exists(eligibility_file):
        raise SystemExit(
            f"missing {eligibility_file} — run "
            "`uv run python -m src.pipeline.eligibility` first"
        )
    out: list[RepoEntry] = []
    with open(eligibility_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("eligibility") or "").strip() != "True":
                continue
            slug = row["repo"].strip().lower()
            if not slug:
                continue
            out.append(RepoEntry(
                repo=slug,
                value_class=(row.get("value_class") or "").strip(),
                repo_id=(row.get("repo_id") or "").strip(),
                enriched=True,
            ))
    out.sort(key=lambda e: e.repo)
    return out
