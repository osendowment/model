"""Shared loaders for the *top* repo set — the valid class-A repos.

Source of truth: `data/value/value.csv` — `load_top_repos` returns repos
whose `class` is one of `settings.json risk_input.value_classes`
(default {A}) and whose `git_valid` column is `True`; this is the set the
risk pipeline runs on. Repo metadata (`repo_id`, `archived`, `size`, `stars`)
is enriched from `data/sources/github/repos.csv`, the authoritative GitHub-API
record populated by `src.sources.github.fetch_repo_owner_data`.

The risk pipeline is GitHub-only (metadata is enriched via the GitHub API), so
`load_top_repos` keeps only `value.csv` rows whose `platform` is `github`;
non-GitHub repos (gitlab / codeberg / custom hosts) are out of scope.

Scope = valid class-A AND not-archived, EXCEPT *live-upstream GitHub mirrors*.
Some class-A repos are GitHub mirrors of a live non-GitHub upstream (e.g.
`bminor/glibc` mirrors sourceware.org; `libpixman/pixman` mirrors
gitlab.freedesktop.org). The GitHub mirror is often flagged `archived=True`
even though the real upstream is alive and updated daily — those mirrors are
identified from `data/value/overrides.csv` (a row with both a non-empty
`repo` and a non-empty non-github `git_url`) and are kept in scope despite the
archived mirror flag, so they still get a risk score.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass

from src.common.params import TOP_REPO_CLASSES, TOP_REPO_PLATFORMS

log = logging.getLogger(__name__)

VALUE_FILE = "data/value/value.csv"
REPOS_FILE = "data/sources/github/repos.csv"
GITLAB_PROJECTS_FILE = "data/sources/gitlab/repos.csv"

# Class precedence — highest class wins if a repo has multiple rows.
_RANK = {"A": 3, "B": 2, "C": 1}


def to_repo_id(raw: object) -> str:
    """Normalise a raw GitHub repo id to the canonical `gh/<id>` form.

    The pipeline's join key is namespaced by host: GitHub repos are keyed by
    GitHub's stable numeric Repos-API id as `gh/<id>`; GitLab repos use
    `gl/<nickname>-<id>` (or a bare `gl/<id>` for gitlab.com; host nicknames per
    `HOST_NICKNAMES` in `src/sources/gitlab/gitlab_client.py`), produced by the
    value stage. This is idempotent: already-namespaced ids (`gh/…`, `gl/…`) and
    empties pass through unchanged; a bare legacy numeric id (`119609`) is
    promoted to `gh/119609`. Both `load_repo_ids` and `RepoEntry.repo_id` return
    this form, so every stage writer downstream of the loader emits it.
    """
    s = ("" if raw is None else str(raw)).strip()
    if not s or "/" in s:
        return s
    return f"gh/{s}"


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
    valid: bool = False
    full_name: str = ""
    # The project's canonical upstream clone URL when this repo is a mirror of
    # it — the thing being mirrored, not the mirror (a GitHub mirror of glibc
    # carries https://sourceware.org/git/glibc.git). Empty for non-mirror repos.
    canonical_url: str = ""
    git_url: str = ""  # authoritative clone URL from value.csv (host-agnostic)


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
                repo_id=to_repo_id(row.get("repo_id")),
                size_kb=int(row.get("size") or 0),
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
                valid=(row.get("valid") or "").strip().lower() in ("true", "1"),
                full_name=full,
                canonical_url=(row.get("canonical_url") or "").strip(),
            )
    return canon, meta


def _read_gitlab_projects(path: str = GITLAB_PROJECTS_FILE) -> dict[str, RepoEntry]:
    """Read data/sources/gitlab/repos.csv → {gl/ repo_id: RepoEntry}.

    The GitLab analogue of `_read_github_repos`'s `meta`, keyed by the stable
    `gl/{nickname}-{id}` repo_id the value stage stamps onto value.csv — so a GitLab
    top repo enriches from the GitLab project API (archived / stars / valid /
    path) instead of the GitHub Repos API. Only valid (fetched, 200) projects
    with a gl/ id are indexed; `size_kb` stays 0 (the project API doesn't expose
    repo size cheaply). No rename `canon` map: gl/ ids are already stable.
    """
    meta: dict[str, RepoEntry] = {}
    if not os.path.exists(path):
        return meta
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            if not rid.startswith("gl/"):
                continue
            full = (row.get("path_with_namespace") or "").strip().lower()
            meta[rid] = RepoEntry(
                repo=full,
                repo_id=rid,
                size_kb=0,
                stars=int(row.get("stars") or 0),
                archived=(row.get("archived") or "").strip().lower() in ("true", "1"),
                enriched=True,
                valid=(row.get("valid") or "").strip().lower() in ("true", "1"),
                full_name=full,
                canonical_url="",
            )
    return meta


def load_top_repos(
    value_file: str = VALUE_FILE,
    repos_file: str = REPOS_FILE,
    skip_archived: bool = False,
    skip_invalid: bool = True,
) -> list[RepoEntry]:
    """Return the *top* repos — valid class-A GitHub/GitLab repos — sorted by slug.

    **GitHub and GitLab only.** A row on any other host (`platform=custom` — a
    self-hosted git server such as sourceware or GNU Savannah) is never a top
    repo, however valuable: the risk dimensions cannot be measured for it (see
    `params.SUPPORTED_PLATFORMS`), so it is left out of scope rather than scored
    on partial data. Such repos stay in `value.csv` with `git_valid=True` and
    are simply not scored.

    Scope = valid class-A. Archived repos are **included by default**
    (`skip_archived=False`): the risk and eligibility stages both score them
    and surface archival as a signal (risk rescored, eligibility `active=False`)
    rather than dropping them silently. Pass `skip_archived=True` to exclude
    them. The risk pipeline runs on this set: repos whose `class` in
    `value.csv` is one of `settings.json top_repos.classes` (default {A}) AND
    whose `platform` is one of `top_repos.platforms` (`github`, `gitlab` — the
    only supported values) AND whose unified `git_valid` column is `True`.

    - Keeps rows whose `platform` ∈ `TOP_REPO_PLATFORMS`, `class` ∈
      `TOP_REPO_CLASSES`, a non-empty `repo`, and `git_valid == "True"`. Both the
      platform and class sets come from `settings.json top_repos`; the platform
      set is validated at import against `SUPPORTED_PLATFORMS`, so it can be
      narrowed but never widened past what the fetchers can measure. The
      `git_valid` gate applies to both platforms (host-agnostic) and is on by
      default (drops failed/404/invalid targets); pass `skip_invalid=False`
      to include rows regardless of validity. The eligibility stage shares
      this exact scope (valid class-A repos).
    - Slugs are canonicalised against `github/repos.csv` `full_name`, so a
      renamed repo (`gozala/events`) resolves to its current name
      (`browserify/events`) — the form the Search API and downstream joins need.
    - Deduped by canonical slug; highest class wins (A > B > C).
    - repo_id / archived / size_kb / stars enriched from `data/sources/github/repos.csv`.
      When `skip_archived=True` (opt-in), archived repos are dropped with no
      exemption — an archived GitHub repo is out. A project whose canonical
      upstream is off GitHub is repointed there via `data/value/overrides.csv`
      (git_url-only), so it enters scope as that upstream, not the archived
      mirror. Dropped archived slugs are logged at INFO for auditability.
    - Repos missing from github/repos.csv are returned with `enriched=False`
      and default metadata; those still get processed.
    """
    canon, gh_meta = _read_github_repos(repos_file)
    gl_meta = _read_gitlab_projects()
    # chosen: dedup key -> (class, platform, repo_id, slug, git_url, canonical_url).
    # The key is the canonical github slug for github repos and the stable gl/
    # repo_id for gitlab repos, so a same-named repo on both hosts never collapses.
    # git_url is value.csv's authoritative clone URL, carried so the clone-URL
    # routing (Phase 2) can hand each fetcher the repo's real host.
    #
    # canonical_url comes from value.csv, not from github/repos.csv: GitHub only
    # populates its own mirror_url field for repos imported through its mirror
    # feature, and leaves it null for the cron-pushed mirrors we actually track
    # (gnutools/glibc, qt/qt5). value.csv's column — written from overrides.csv —
    # is the one that knows a repo is a mirror, which build_workload needs: a
    # mirror's empty issue tracker is not the project's backlog.
    chosen: dict[str, tuple[str, str, str, str, str, str]] = {}
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cls = (row.get("class") or "").strip()
            if cls not in TOP_REPO_CLASSES:
                continue
            # GitHub/GitLab only (TOP_REPO_PLATFORMS ⊆ SUPPORTED_PLATFORMS, checked
            # at import). A custom-host row — a project whose canonical upstream is
            # a self-hosted git server — is dropped here: none of the four risk
            # dimensions can be measured for it, so it never enters scope.
            platform = (row.get("platform") or "").strip().lower()
            if platform not in TOP_REPO_PLATFORMS:
                continue
            raw = (row.get("repo") or "").strip().lower()
            if not raw:
                continue
            # git_valid is the renamed column (was `valid`); fall back for pre-rename CSVs.
            git_valid_val = (row.get("git_valid") or "").strip()
            if skip_invalid and git_valid_val != "True":
                continue
            rid = to_repo_id((row.get("repo_id") or "").strip())
            git_url = (row.get("git_url") or "").strip()
            canonical_url = (row.get("canonical_url") or "").strip()
            if platform == "github":
                slug = canon.get(raw, raw)      # resolve renamed repos
                key = slug
            else:
                slug = raw                       # non-github: value.csv path is canonical
                key = rid or f"{platform}:{raw}"
            if key not in chosen or _RANK.get(cls, 0) > _RANK.get(chosen[key][0], 0):
                chosen[key] = (cls, platform, rid, slug, git_url, canonical_url)

    entries: list[RepoEntry] = []
    dropped_archived: list[str] = []
    for key, (cls, platform, rid, slug, git_url, canonical_url) in chosen.items():
        if platform == "github":
            e = gh_meta.get(slug) or RepoEntry(repo=slug)
        else:  # gitlab — the only other platform that survives the gate above
            e = gl_meta.get(rid) or RepoEntry(repo=slug, repo_id=rid)
        e.repo = slug
        e.value_class = cls
        # value.csv wins: it carries the curated canonical upstream, where the
        # GitHub API's own field is null for a cron-pushed mirror.
        if canonical_url:
            e.canonical_url = canonical_url
        if not e.repo_id:
            e.repo_id = rid
        # Clone URL: prefer value.csv's authoritative git_url (host-agnostic).
        # github/repos.csv enrichment carries no git_url column, so never let it
        # blank this; a github entry that somehow lacks one falls back to its
        # canonical github clone URL (byte-identical to the old hardcoded path).
        if git_url:
            e.git_url = git_url
        elif platform == "github" and not e.git_url:
            e.git_url = f"https://github.com/{slug}.git"
        if skip_archived and e.archived:
            dropped_archived.append(slug)
            continue
        entries.append(e)

    if dropped_archived:
        log.info(
            "Skipped %d archived risk repos: %s",
            len(dropped_archived),
            ", ".join(sorted(dropped_archived)),
        )
    entries.sort(key=lambda e: e.repo)
    return entries


def load_top_slugs(*args, **kwargs) -> list[str]:
    """Convenience wrapper returning just the lowercased repo slugs."""
    return [e.repo for e in load_top_repos(*args, **kwargs)]


def load_github_top_slugs(*args, **kwargs) -> list[str]:
    """`load_top_slugs` restricted to GitHub repos (skip gl/… non-GitHub ids).

    The GitHub-API fetchers (contributor-metrics, issue-metrics, churn) hit
    ``github.com`` / the GitHub REST+Search API and cannot service a GitLab
    target — a GitLab slug like ``gnome/glib`` would resolve to an unrelated
    GitHub mirror and pollute the output. This is the fetch list they use so a
    ``gitlab`` entry in ``top_repos.platforms`` never reaches a GitHub call.
    """
    return [
        e.repo for e in load_top_repos(*args, **kwargs)
        if not str(e.repo_id or "").startswith("gl/")
    ]


def load_git_urls(value_file: str = VALUE_FILE) -> dict[str, str]:
    """Map `repo_id` -> `git_url` from value.csv — the authoritative clone URL
    per repo, host-agnostic (github OR gitlab OR other). Every per-repo
    git-source file carries a `git_url` column so a downstream fetcher can clone
    the real host instead of assuming `github.com/{repo}`; this is the map that
    backfills it and that `git_url_for` resolves against.
    """
    out: dict[str, str] = {}
    if not os.path.exists(value_file):
        return out
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = to_repo_id((row.get("repo_id") or "").strip())
            url = (row.get("git_url") or "").strip()
            if rid and url:
                out[rid] = url
    return out


def git_url_for(repo_id: str, repo: str, git_urls: dict[str, str]) -> str:
    """The clone `git_url` for a per-repo source row: the value.csv-mapped URL
    (from `load_git_urls`) when known, else the canonical GitHub URL. GitHub is
    the fallback host because legacy github-source rows predate the git_url
    column and github.com/{repo} is always their clone URL. Empty when neither
    a mapped URL nor a `repo` slug is available.
    """
    rid = to_repo_id((repo_id or "").strip())
    if rid in git_urls:
        return git_urls[rid]
    slug = (repo or "").strip().lower()
    return f"https://github.com/{slug}.git" if slug else ""


def canonical_repo_map(repos_file: str = REPOS_FILE) -> dict[str, str]:
    """Map every known repo slug -> its canonical lowercased `full_name`.

    For risk scripts that read a `repo` slug straight from value.csv
    or a per-ecosystem results.csv: pass each raw slug through this map so
    a renamed repo (`gozala/events`) resolves to the same canonical name
    `load_top_repos` uses (`browserify/events`). Unknown slugs map to
    themselves via `.get(slug, slug)`.
    """
    return _read_github_repos(repos_file)[0]


def load_value_repo_ids(value_file: str = VALUE_FILE) -> dict[str, str]:
    """Map canonical repo slug -> canonical `gh/<id>` from value.csv.

    Fallback/overlay for `load_repo_ids` (github/repos.csv): a freshly
    renamed repo reaches value.csv under its new canonical slug before
    github/repos.csv catches up, so a repos.csv-only map leaves the new
    slug unresolvable and fetched rows land with a blank repo_id — which
    the id-keyed builder joins then silently drop. Ids are returned in
    the same canonical `gh/<id>` form as `load_repo_ids` (see to_repo_id).
    """
    out: dict[str, str] = {}
    if not os.path.exists(value_file):
        return out
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("platform") or "").strip().lower() != "github":
                continue
            slug = (row.get("repo") or "").strip().lower()
            rid = to_repo_id((row.get("repo_id") or "").strip())
            if slug and rid:
                out.setdefault(slug, rid)
    return out


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
            rid = to_repo_id(row.get("repo_id"))
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
load_ab_repos = load_top_repos
load_ab_slugs = load_top_slugs
