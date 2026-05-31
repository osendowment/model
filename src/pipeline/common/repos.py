"""Shared repo loaders for the risk pipeline.

Source of truth: `data/value/value.csv` — the risk pipeline runs on repos
whose `class` is one of `settings.json risk_input.value_classes`
(default {A, B}). Repo metadata (`repo_id`, `archived`, `size`, `stars`)
is enriched from `data/sources/github/repos.csv`, the authoritative GitHub-API
record populated by `src.github.fetch_repo_owner_data`.

`load_eligible_repos` (eligibility-data.csv) is retained for the
eligibility stage only — no risk script should call it.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass

from src.pipeline.common.params import RISK_INPUT_CLASSES

log = logging.getLogger(__name__)

VALUE_FILE = "data/value/value.csv"
REPOS_FILE = "data/sources/github/repos.csv"
ELIGIBILITY_FILE = "data/eligibility/eligibility.csv"

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


def _read_github_repos(path: str) -> tuple[dict[str, str], dict[str, RepoEntry]]:
    """Read data/sources/github/repos.csv → (canon, meta).

    GitHub's `/repos/{owner}/{repo}` endpoint follows renames, so a repo
    whose slug in value-data.csv is stale (e.g. `gozala/events`) was still
    fetched successfully and recorded with its **current** name in the
    `full_name` column (`browserify/events`). The Search API does *not*
    follow renames, so the risk pipeline must key on the current name.

    - `canon` maps every known slug — both the looked-up `repo` and the
      rename-resolved `full_name` — to the canonical lowercased `full_name`.
    - `meta` maps the canonical name to a RepoEntry with repo_id / archived
      / size_kb / stars.
    """
    canon: dict[str, str] = {}
    meta: dict[str, RepoEntry] = {}
    if not os.path.exists(path):
        return canon, meta
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            full = (row.get("full_name") or "").strip().lower() or slug
            canon[slug] = full
            canon[full] = full
            meta[full] = RepoEntry(
                repo=full,
                repo_id=(row.get("repo_id") or "").strip(),
                size_kb=int(row.get("size") or 0),
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
            )
    return canon, meta


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
    - Slugs are canonicalised against `github/repos.csv` `full_name`, so a
      renamed repo (`gozala/events`) resolves to its current name
      (`browserify/events`) — the form the Search API and downstream joins need.
    - Deduped by canonical slug; highest class wins (A > B > C > D).
    - repo_id / archived / size_kb / stars enriched from `data/sources/github/repos.csv`.
      `skip_archived` drops archived repos.
    - Repos missing from github/repos.csv are returned with `enriched=False`
      and default metadata; those still get processed.
    """
    canon, meta = _read_github_repos(repos_file)
    chosen: dict[str, str] = {}
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cls = (row.get("class") or "").strip()
            if cls not in RISK_INPUT_CLASSES:
                continue
            raw = (row.get("github_repo") or "").strip().lower()
            if not raw:
                continue
            if skip_invalid and (row.get("gh_valid") or "").strip() != "True":
                continue
            slug = canon.get(raw, raw)  # resolve renamed repos to current name
            if slug not in chosen or _RANK.get(cls, 0) > _RANK.get(chosen[slug], 0):
                chosen[slug] = cls

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


def canonical_repo_map(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map every known repo slug -> its canonical lowercased `full_name`.

    For risk scripts that read `github_repo` straight from value-data.csv
    or a per-ecosystem results.csv: pass each raw slug through this map so
    a renamed repo (`gozala/events`) resolves to the same canonical name
    `load_risk_repos` uses (`browserify/events`). Unknown slugs map to
    themselves via `.get(slug, slug)`.
    """
    return _read_github_repos(repos_file)[0]


def load_repo_ids(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map repo slug -> repo_id from data/sources/github/repos.csv.

    Replaces the old per-script `repo -> repo_id` readers that keyed off
    `eligibility-data.csv`. Both the looked-up `repo` slug and the
    rename-resolved `full_name` are keyed to the same `repo_id`, so callers
    resolve correctly whether they hold a stale or a canonical slug. Rows
    with an empty `repo_id` are skipped.
    """
    out: dict[str, str] = {}
    if not os.path.exists(repos_file):
        return out
    with open(repos_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if not rid:
                continue
            slug = (row.get("repo") or "").strip().lower()
            full = (row.get("full_name") or "").strip().lower()
            if slug:
                out[slug] = rid
            if full:
                out[full] = rid
    return out


def load_default_branches(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map repo slug -> default_branch from data/sources/github/repos.csv.

    Both the looked-up `repo` slug and the rename-resolved `full_name` are
    keyed to the same branch, so callers resolve whether they hold a stale
    or canonical slug. Rows with an empty default_branch are skipped.

    Used by SHA-pinned fetchers (scc, lizard, …) to verify a snapshot SHA
    is actually on the repo's default branch — GitHub's Commits API can
    hand back a fork-network sibling's commit.
    """
    out: dict[str, str] = {}
    if not os.path.exists(repos_file):
        return out
    with open(repos_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            branch = (row.get("default_branch") or "").strip()
            if not branch:
                continue
            slug = (row.get("repo") or "").strip().lower()
            full = (row.get("full_name") or "").strip().lower()
            if slug:
                out[slug] = branch
            if full:
                out[full] = branch
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
            "`uv run python -m src.pipeline.eligibility.classify_eligibility` first"
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
