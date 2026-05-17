"""Shared repo loaders for the risk pipeline.

Source of truth: `data/value-data.csv` — the risk pipeline runs on repos
whose `class` is one of `settings.json risk_input.value_classes`
(default {A, B}). Repo metadata (`repo_id`, `archived`, `size`, `stars`)
is enriched from `data/github/repos.csv`, the authoritative GitHub-API
record populated by `src.github.fetch_repo_owner_data`.

`load_eligible_repos` (eligibility-data.csv) is retained for the
eligibility stage only — no risk script should call it.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass

from src.pipeline.params import RISK_INPUT_CLASSES

log = logging.getLogger(__name__)

VALUE_FILE = "data/value-data.csv"
REPOS_FILE = "data/github/repos.csv"
ELIGIBILITY_FILE = "data/eligibility-data.csv"

# Class precedence — highest class wins if a repo has multiple rows.
_RANK = {"A": 4, "B": 3, "C": 2, "D": 1}


@dataclass
class RepoEntry:
    """One risk-scope repo with optional enrichment from github/repos.csv."""
    repo: str
    value_class: str = ""
    repo_id: str = ""
    size_kb: int = 0
    stars: int = 0
    archived: bool = False
    enriched: bool = False  # True iff metadata came from github/repos.csv


def _load_repos_meta(path: str) -> dict[str, RepoEntry]:
    """Map lowercased repo slug -> RepoEntry enriched from data/github/repos.csv."""
    out: dict[str, RepoEntry] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = RepoEntry(
                repo=slug,
                repo_id=(row.get("repo_id") or "").strip(),
                size_kb=int(row.get("size") or 0),
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
            )
    return out


def load_risk_repos(
    value_file: str = VALUE_FILE,
    repos_file: str = REPOS_FILE,
    skip_archived: bool = True,
    skip_invalid: bool = True,
) -> list[RepoEntry]:
    """Return repos in the risk-input value classes, sorted by slug.

    The risk pipeline runs on this set: repos whose `class` in
    `value-data.csv` is one of `settings.json risk_input.value_classes`
    (default {A, B}).

    - Keeps rows with `class` in RISK_INPUT_CLASSES and a non-empty
      `github_repo`. `skip_invalid` drops `gh_valid` != True (404 repos).
    - Deduped by lowercased `github_repo`; highest class wins (A > B > C > D).
    - repo_id / archived / size_kb / stars enriched from `data/github/repos.csv`.
      `skip_archived` drops archived repos.
    - Repos missing from github/repos.csv are returned with `enriched=False`
      and default metadata; those still get processed.
    """
    chosen: dict[str, str] = {}
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cls = (row.get("class") or "").strip()
            if cls not in RISK_INPUT_CLASSES:
                continue
            slug = (row.get("github_repo") or "").strip().lower()
            if not slug:
                continue
            if skip_invalid and (row.get("gh_valid") or "").strip() != "True":
                continue
            if slug not in chosen or _RANK.get(cls, 0) > _RANK.get(chosen[slug], 0):
                chosen[slug] = cls

    meta = _load_repos_meta(repos_file)
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
        log.info("Skipped %d archived risk repos", skipped_archived)
    entries.sort(key=lambda e: e.repo)
    return entries


def load_risk_slugs(*args, **kwargs) -> list[str]:
    """Convenience wrapper returning just the lowercased repo slugs."""
    return [e.repo for e in load_risk_repos(*args, **kwargs)]


def load_repo_ids(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map lowercased repo slug -> repo_id from data/github/repos.csv.

    Replaces the old per-script `repo -> repo_id` readers that keyed off
    `eligibility-data.csv`. Rows with an empty `repo_id` are skipped.
    """
    out: dict[str, str] = {}
    if not os.path.exists(repos_file):
        return out
    with open(repos_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            rid = (row.get("repo_id") or "").strip()
            if slug and rid:
                out[slug] = rid
    return out


# Back-compat aliases — risk scripts migrate to load_risk_*; these keep any
# remaining `load_ab_*` imports working. The old `top_repos_file` kwarg of
# load_ab_repos is no longer accepted.
load_ab_repos = load_risk_repos
load_ab_slugs = load_risk_slugs


def load_eligible_repos(
    eligibility_file: str = ELIGIBILITY_FILE,
) -> list[RepoEntry]:
    """Return repos with `eligibility=True` from `eligibility-data.csv`.

    Retained for the eligibility stage only. The risk pipeline now reads
    `value-data.csv` via `load_risk_repos` — no risk script should call this.
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
