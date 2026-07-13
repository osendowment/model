#!/usr/bin/env python3
"""Apply ecosyste.ms as the authoritative repo-identity layer onto results.csv.

Roll-up step. For every package in each ecosystem's `results.csv`, re-resolve the
canonical `git` URL (and `github_repo` slug) using the source priority:

    override > eco_strong > prior > eco_weak

  • override   — curated `data/value/overrides.csv` (absolute; manual corrections
                 for the cases where ecosyste.ms contradicts ground truth)
  • eco_strong — ecosyste.ms `repo_full_name` (its rename-resolved canonical
                 GitHub repo); this is the authoritative signal — it follows
                 GitHub renames/moves, so it beats our prior registry slug
  • prior      — the `git` / `github_repo` already in results.csv (the native
                 registry resolution from `build_git_urls`, or a previous run)
  • eco_weak   — ecosyste.ms raw `repository_url` / `homepage` recorded WITHOUT a
                 successful repo crawl (dead / un-crawled / non-GitHub) — fallback

This reads `data/sources/ecosystems/packages.csv` (run
`src.sources.ecosystems.candidates --scope top` first) and the overrides file,
then rewrites each `results.csv`'s `git`, `github_repo` and `eco_guess` columns
in place. It needs NO native raw inputs (npm nice-registry, crates db-dump, …),
so unlike `build_git_urls` it runs inside `--rollup`. Idempotent: re-running
re-resolves from the same eco + override inputs.

Because the canonical `git` URL is the grouping key in `unify_value_data`, a
rename fix re-groups a repo correctly and a merge-override (several packages →
one repo) collapses them into a single value.csv row.

Usage:
    uv run python -m src.value.apply_ecosystems_authority
    uv run python -m src.value.apply_ecosystems_authority --eco npm
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import _read_github_repos
from src.sources.github.fetch_repo_owner_data import fetch_and_persist, owners_from_repos
from src.sources.gitlab.fetch_project_data import REPOS_OUT as _GITLAB_PROJECTS_FILE
from src.sources.gitlab.fetch_project_data import fetch_and_persist as _gitlab_fetch_and_persist
from src.sources.gitlab.gitlab_client import parse_git_url as _parse_gitlab_url
from src.value.build_git_urls import (
    GIT_HOST_PRIORITY,
    _load_invalid_lookup,
    classify,
    merge_urls_with_source,
)
from src.value.git_urls import _canonicalize_git_url
from src.value.unify_value_data import _github_repo_from_url, load_repo_overrides

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
PACKAGES_CSV = DATA_DIR / "sources" / "ecosystems" / "packages.csv"
ECOSYSTEMS = ["npm", "pypi", "crates", "cpp"]

_REPOS_FILE_DEFAULT = str(DATA_DIR / "sources" / "github" / "repos.csv")


# ── eco source ─────────────────────────────────────────────────────────────────


def load_eco(ecosystem: str) -> dict[str, dict[str, list[str]]]:
    """{package: {"strong": [...], "weak": [...]}} for one ecosystem from packages.csv.

    strong = rename-resolved canonical GitHub repo (`repo_full_name` @ github),
    weak   = raw repository_url / homepage (no successful crawl).
    """
    out: dict[str, dict[str, list[str]]] = {}
    if not PACKAGES_CSV.exists():
        return out
    with open(PACKAGES_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("ecosystem") != ecosystem:
                continue
            full = (r.get("repo_full_name") or "").strip()
            host = (r.get("repo_host") or "").strip().lower()
            strong = [f"https://github.com/{full}.git"] if full and host == "github" else []
            weak = [u for u in (r.get("repository_url"), r.get("homepage")) if (u or "").strip()]
            out[r["package"]] = {"strong": strong, "weak": weak}
    return out


# ── per-package resolution ──────────────────────────────────────────────────────


def resolve(prior_git: str, prior_github: str, strong: list[str], weak: list[str],
            ov: dict | None, is_invalid) -> tuple[str, str, str]:
    """Return (git_url, github_repo, eco_guess) under the authoritative priority."""
    prior: list[str] = []
    if prior_git:
        prior.append(prior_git)
    elif prior_github:
        prior.append(f"https://github.com/{prior_github}.git")

    merged, srcs = merge_urls_with_source(
        {"eco_strong": strong, "prior": prior, "eco_weak": weak}, is_invalid,
    )

    # Curated override is absolute. The overrides file's `repo` column is
    # always a GitHub slug (non-GitHub identities are set via `git_url`).
    if ov:
        if ov.get("repo"):
            _, canon = classify(f"https://github.com/{ov['repo']}")
            if canon:
                merged["github"] = canon
                srcs["github"] = "override"
        elif ov.get("git_url"):
            # A git_url-only override is absolute: it names the canonical clone
            # URL, so drop every eco/prior-derived host and keep only it.
            # Otherwise a low-priority override host (e.g. sourceware, host
            # class 'custom') loses under GIT_HOST_PRIORITY to an eco-provided
            # higher-priority host — e.g. glibc's Debian salsa GitLab packaging
            # mirror would beat the sourceware upstream the override names.
            merged.clear()
            srcs.clear()
            plat, canon = classify(ov["git_url"])
            if plat:
                merged[plat] = canon
                srcs[plat] = "override"
        elif ov.get("canonical_url"):
            # The project has NO git upstream (tarball / hg / svn only). Drop every
            # eco/prior-derived URL and claim no identity at all: the alternative is
            # a fork or a personal mirror, which credits the wrong maintainers — the
            # exact failure this file exists to correct (IJG libjpeg was crediting
            # mozilla/mozjpeg; Info-ZIP was crediting zlib's author). Where the code
            # really lives is recorded in `canonical_url`, stamped by `apply_eco`.
            merged.clear()
            srcs.clear()

    git_plat = next((p for p in GIT_HOST_PRIORITY if merged.get(p)), None)
    git = merged.get(git_plat, "") if git_plat else ""
    src = srcs.get(git_plat, "") if git_plat else ""

    # github_repo is authoritative from the winning git URL when it's GitHub;
    # a git_url-only override explicitly clears it; otherwise keep the prior slug
    # (non-GitHub canonical repos may still carry a github mirror downstream).
    if git_plat == "github":
        github = _github_repo_from_url(git)
    elif ov and (ov.get("git_url") or ov.get("canonical_url")):
        github = ""
    else:
        github = prior_github

    guess = {"override": "override", "eco_strong": "eco",
             "eco_weak": "eco", "prior": "native"}.get(src, "")
    return git, github, guess


# ── GitHub canonical (rename) authority ─────────────────────────────────────────

CANONICAL_FILE = DATA_DIR / "sources" / "github" / "canonical-repos.csv"


def load_canonical_map() -> dict[str, str]:
    """{old_slug: canonical_slug} for repos GitHub has renamed/moved.

    From `src.sources.github.fetch_canonical` (`repository.nameWithOwner`). This
    is the TOP rename authority — GitHub is ground truth and never lags a move,
    unlike ecosyste.ms `repo_full_name` (which trailed React's move to the `react`
    org). Only entries whose canonical name actually differs are kept.
    """
    out: dict[str, str] = {}
    if CANONICAL_FILE.exists():
        with open(CANONICAL_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                repo = (r.get("repo") or "").strip().lower()
                canon = (r.get("canonical_repo") or "").strip().lower()
                if (r.get("status") or "") == "ok" and canon and canon != repo:
                    out[repo] = canon
    return out


# ── GitHub identity resolution ─────────────────────────────────────────────────


def _resolve_github(rows: list[dict], repos_file: str | None = None,
                    offline: bool = False, force: bool = False) -> None:
    """In-place: stamp repo_id, github_repo, git, canonical_url from github/repos.csv.

    After the eco-authority rewrite, each row's git URL is canonicalised
    (GitHub URLs are cleared; non-GitHub URLs normalised), then the GitHub
    slug is read from the github_repo column and looked up against repos.csv —
    which is kept fresh via a 90-day TTL fetch.  On a valid hit the row gets:
        repo_id    = "gh/<numeric>"
        github_repo = <post-rename canonical full_name>
        git        = "https://github.com/<full_name>.git"
        canonical_url  = <the project's canonical upstream clone URL when this
                          repo is a mirror of it, else "">
    Unresolved rows get empty repo_id/canonical_url and keep their (canonicalised)
    git URL.
    """
    if repos_file is None:
        repos_file = _REPOS_FILE_DEFAULT

    # Step 0: clear every prior-run repo_id up front. repo_id is re-derived from
    # the current git URL each resolve, so a stale id from a previous identity
    # (an override just repointed the row to a different GitHub/GitLab repo, or a
    # slug went 404) must not survive — otherwise _resolve_gitlab sees a
    # non-empty repo_id and skips the row, silently keeping the wrong id. Valid
    # rows are re-stamped from the live caches in step 5 (GitHub) / _resolve_gitlab.
    for r in rows:
        r["repo_id"] = ""

    # Step 1: canonicalize git URLs.  GitHub URLs are cleared (→ ""); non-GitHub
    # URLs are normalised (scheme, gitweb paths, …).  The github_repo column is the
    # fallback slug source for GitHub rows whose git column just became "".
    for r in rows:
        r["git"] = _canonicalize_git_url(r.get("git", ""))

    # Step 2: collect candidate GitHub slugs across all rows.
    def _slug(r: dict) -> str:
        return (_github_repo_from_url(r.get("git", ""))
                or (r.get("github_repo") or "").strip().lower())

    slugs = {_slug(r) for r in rows} - {""}

    # Step 3: TTL-gated fetch (90d).  Already-fresh rows cost ~0 network.
    # --offline skips the fetch entirely (hard-forbid network);
    # --refresh forces a refetch ignoring the TTL.
    if slugs and not offline:
        fetch_and_persist(
            repos=sorted(slugs),
            owners=owners_from_repos(sorted(slugs)),
            target="repos",
            force=force,
            quiet=True,
        )

    # Step 4: read back the identity map from repos.csv.
    canon, meta = _read_github_repos(repos_file)

    # Step 5: stamp each row from the identity map.
    for r in rows:
        slug = _slug(r)
        if not slug:
            r.setdefault("repo_id", "")
            r.setdefault("canonical_url", "")
            continue
        canonical = canon.get(slug, slug)
        entry = meta.get(canonical)
        if entry and entry.valid and entry.repo_id:
            r["repo_id"] = entry.repo_id  # already namespaced by the loader (gh/<id>)
            r["github_repo"] = entry.full_name
            r["git"] = f"https://github.com/{entry.full_name}.git"
            r["canonical_url"] = entry.canonical_url or ""
        else:
            r.setdefault("repo_id", "")
            r.setdefault("canonical_url", "")
            if slug:  # had a GitHub slug → keep its clone URL so it's not an orphan
                r["git"] = f"https://github.com/{slug}.git"


def _load_gitlab_repo_ids(path=None) -> dict[str, str]:
    """`{project_key: repo_id}` from gitlab/repos.csv, valid rows only.

    `project_key = "{host}/{path}".lower()` (the fetcher's upsert key); `repo_id`
    is the `gl/{nickname}-{id}` / `gl/{id}` form. Only rows that resolved to a real
    project (`valid == True`) with a `gl/` id are returned — so an unresolved /
    404 GitLab URL never gets an id (the validator half of the resolver).
    """
    path = path or _GITLAB_PROJECTS_FILE
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rid = (r.get("repo_id") or "").strip()
            key = (r.get("project") or "").strip().lower()
            if key and rid.startswith("gl/") and (r.get("valid") or "").strip().lower() == "true":
                out[key] = rid
    return out


def _resolve_gitlab(rows: list[dict], offline: bool = False, force: bool = False) -> None:
    """In-place: stamp `gl/` repo_id on GitLab rows GitHub left unresolved.

    Runs AFTER `_resolve_github`, so **GitHub wins**: only a row with an empty
    `repo_id` (GitHub didn't claim it) and a GitLab clone URL is considered.
    Collects the distinct GitLab targets, TTL-fetches their project metadata into
    `data/sources/gitlab/repos.csv` (skipped under --offline), then stamps
    `repo_id = gl/{nickname}-{id}` (`gl/{id}` for gitlab.com) for every target that
    resolved to a valid project. A 404 / unreachable GitLab URL keeps an empty
    repo_id — it groups by `git_url` and gets `git_valid` from the ls-remote pass.
    """
    def _target(r: dict):
        if (r.get("repo_id") or "").strip():
            return None                      # GitHub already resolved this row
        return _parse_gitlab_url((r.get("git") or "").strip())

    targets: dict[str, dict] = {}
    for r in rows:
        parsed = _target(r)
        if parsed:
            host, path = parsed
            key = f"{host}/{path}".lower()
            targets[key] = {"host": host, "path": path, "project": key}
    if not targets:
        return
    if not offline:
        _gitlab_fetch_and_persist(target="projects", targets=list(targets.values()),
                                  force=force, quiet=True)
    ids = _load_gitlab_repo_ids()
    for r in rows:
        parsed = _target(r)
        if not parsed:
            continue
        rid = ids.get(f"{parsed[0]}/{parsed[1]}".lower())
        if rid:
            r["repo_id"] = rid


# Dispatcher so other platforms can slot in later. GitHub is applied first
# (priority); GitLab fills repo_ids GitHub left empty.
PLATFORM_RESOLVERS: dict[str, object] = {"github": _resolve_github, "gitlab": _resolve_gitlab}


# ── per-ecosystem rewrite ───────────────────────────────────────────────────────


def apply_eco(ecosystem: str, overrides: dict, is_invalid, canonical: dict,
              offline: bool = False, force: bool = False) -> dict:
    results_path = DATA_DIR / "sources" / ecosystem / "results.csv"
    if not results_path.exists():
        return {"ecosystem": ecosystem, "total": 0}

    eco = load_eco(ecosystem)
    with open(results_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    for col in ("git", "github_repo", "eco_guess"):
        if col not in fields:
            fields.append(col)

    c = {"total": len(rows), "eco": 0, "override": 0, "git_changed": 0, "gh_changed": 0}
    for r in rows:
        pkg = r["package"]
        prior_git = (r.get("git") or "").strip().lower()
        prior_gh = (r.get("github_repo") or "").strip().lower()
        e = eco.get(pkg, {})
        git, gh, guess = resolve(
            prior_git, prior_gh, e.get("strong", []), e.get("weak", []),
            overrides.get((pkg, ecosystem)), is_invalid,
        )
        # GitHub canonical authority (top rename layer): remap a renamed/moved
        # repo to its current slug so unify groups it under the canonical URL.
        canon = canonical.get(gh)
        if canon and canon != gh:
            gh, git = canon, f"https://github.com/{canon}.git"
        if guess == "eco":
            c["eco"] += 1
        elif guess == "override":
            c["override"] += 1
        if git != prior_git:
            c["git_changed"] += 1
        if gh != prior_gh:
            c["gh_changed"] += 1
        r["git"], r["github_repo"], r["eco_guess"] = git, gh, guess

    # ── Identity pass (GitHub first — it wins — then GitLab fills the rest) ──────
    # After the eco-authority rewrite, stamp repo_id / canonical_url from repos.csv,
    # then stamp gl/ ids on any GitLab rows GitHub left unresolved.
    for col in ("repo_id", "canonical_url"):
        if col not in fields:
            fields.append(col)
    _resolve_github(rows, offline=offline, force=force)
    _resolve_gitlab(rows, offline=offline, force=force)

    # A curated `canonical_url` override names where a repo-less project's source
    # actually lives. Stamp it AFTER the identity pass: _resolve_github fills
    # canonical_url from github/repos.csv (blank for these rows, since they resolve
    # to no repo), so stamping earlier would be overwritten.
    for r in rows:
        ov = overrides.get((r["package"], ecosystem))
        if ov and ov.get("canonical_url"):
            r["canonical_url"] = ov["canonical_url"]

    tmp = results_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(results_path)
    return {"ecosystem": ecosystem, **c}


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print(stats: list[dict]) -> None:
    t = Table(title="[bold]ecosyste.ms authoritative resolution[/bold]",
              show_header=True, header_style="bold dim", padding=(0, 1))
    t.add_column("Eco", style="bold")
    for col in ("Rows", "eco-won", "override", "git Δ", "github Δ"):
        t.add_column(col, justify="right")
    tot = {"total": 0, "eco": 0, "override": 0, "git_changed": 0, "gh_changed": 0}
    for s in stats:
        t.add_row(s["ecosystem"], f"{s.get('total',0):,}", f"{s.get('eco',0):,}",
                  f"{s.get('override',0):,}", f"[green]{s.get('git_changed',0):,}[/green]",
                  f"[green]{s.get('gh_changed',0):,}[/green]")
        for k in tot:
            tot[k] += s.get(k, 0)
    t.add_section()
    t.add_row("[bold]Total[/bold]", f"[bold]{tot['total']:,}[/bold]", f"[bold]{tot['eco']:,}[/bold]",
              f"[bold]{tot['override']:,}[/bold]", f"[bold green]{tot['git_changed']:,}[/bold green]",
              f"[bold green]{tot['gh_changed']:,}[/bold green]")
    console.print()
    console.print(t)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eco", choices=ECOSYSTEMS + ["all"], default="all")
    parser.add_argument("--offline", action="store_true",
                        help="Skip network fetches; use only existing caches.")
    parser.add_argument("--refresh", action="store_true",
                        help="Force refetch, ignoring TTL caches.")
    args = parser.parse_args()

    if not PACKAGES_CSV.exists():
        console.print(f"[red]missing {PACKAGES_CSV} — run "
                      "src.sources.ecosystems.candidates --scope top first[/red]")
        raise SystemExit(1)

    overrides = load_repo_overrides()
    is_invalid = _load_invalid_lookup()
    canonical = load_canonical_map()
    ecosystems = ECOSYSTEMS if args.eco == "all" else [args.eco]
    console.print(f"[bold]ecosyste.ms authoritative layer[/bold]  "
                  f"[dim]{len(overrides)} overrides | {len(canonical)} GitHub renames | "
                  "priority: override > github-canonical > eco_strong > prior > eco_weak[/dim]")
    stats = [apply_eco(e, overrides, is_invalid, canonical,
                       offline=args.offline, force=args.refresh) for e in ecosystems]
    _print(stats)


if __name__ == "__main__":
    main()
