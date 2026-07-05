#!/usr/bin/env python3
"""Value pipeline step — verify every repo's git URL accepts a connection.

Runs after `unify_value_data` has written `data/value/value.csv`. Reads that
file back, refreshes the two validation caches, sets the `repo_id`
(identity) and `mirror_url` columns, canonicalises a github `repo` to its
current (post-rename) name, and rewrites the file.

Two strategies, chosen per row's `platform`:

  1. platform == github → `src.sources.github.fetch_repo_owner_data.fetch_and_persist`
     hits `/repos/{owner}/{repo}` via the GitHub API. Persists richer
     metadata to `data/sources/github/repos.csv` (valid, license, owner,
     stars, mirror_url, …) so downstream pipelines (eligibility) don't
     re-fetch. The `repo_id` column is set to `gh/<numeric>`, and `mirror_url`
     to the upstream a github mirror syncs from (empty for non-mirror repos).
  2. non-github canonical git URL → `git ls-remote --exit-code` against the
     URL itself. Persists OK/FAIL to `data/sources/git/urls.csv`; `repo_id`
     stays empty (no numeric id for non-github hosts).

Both layers are TTL'd by `GIT_URL_TTL_DAYS`. This step does NOT write a
validity verdict onto value.csv — the per-repo `valid` column is produced by
the separate `build_validation` step, which rolls these two caches up into
`data/value/validation.csv` and joins the verdict back.

Usage:
    uv run python -m src.value.verify_git_urls
"""

from __future__ import annotations

import csv

from rich.console import Console
from rich.table import Table

from src.value.build_git_urls import platform_and_slug
from src.value.git_urls import (
    _canonicalize_git_url,
    _verify_non_github,
)
from src.value.unify_value_data import OUTPUT_FILE, write_value_data

console = Console()


def verify_urls_in_aggregates(aggs: list[dict],
                              force: bool = False) -> tuple[list[dict], dict]:
    """Refresh the validation caches and set `repo_id` on every row.

    Two strategies, one per row's `platform`:
      • platform == github → trigger `fetch_repo_owner_data.fetch_and_persist`,
        refreshing `data/sources/github/repos.csv` (`valid` + metadata).
      • non-github git_url → `git ls-remote`, cached in
        `data/sources/git/urls.csv`.

    Sets `repo_id` (`gh/<numeric>` for github, empty otherwise), canonicalises
    a github `repo` to its current post-rename name, and re-derives
    `(platform, repo)` for non-github rows off the canonicalised URL. Does NOT
    write a validity verdict onto the rows — that is `build_validation`'s job.
    Returns (aggs, summary_stats); the summary counts are derived directly from
    the caches. Mutates `aggs`.
    """
    # Lazy import: keeps CLI startup snappy when not actually verifying.
    from src.sources.github.fetch_repo_owner_data import (
        fetch_and_persist,
        REPOS_OUT as GH_REPOS_FILE,
        owners_from_repos,
    )

    # First — mechanical canonicalisation of known wrong URL patterns
    # (cgit→git, gitweb→git, anongit→gitlab, etc.). This recovers the
    # majority of "valid project, wrong URL" cases without any network IO.
    rewritten = 0
    blanked = 0
    for a in aggs:
        old = (a.get("git_url") or "").strip()
        new = _canonicalize_git_url(old)
        if new != old:
            a["git_url"] = new
            if not new:
                blanked += 1
            else:
                rewritten += 1
    console.rule("[bold cyan]value/verify URLs")
    if rewritten or blanked:
        console.print(f"  [dim]URL canonicalisation:[/dim] "
                      f"rewritten=[cyan]{rewritten}[/cyan] "
                      f"blanked=[red]{blanked}[/red] (non-git VCS)")

    # A mechanical rewrite can turn a non-github git_url into a github one
    # (e.g. gnupg gitweb / xiph → a github mirror). Promote those to a github
    # `(platform, repo)` identity so they are validated via the GitHub API and
    # receive a `repo_id` below, keeping the "github repo ⟺ github URL" invariant.
    promoted = 0
    for a in aggs:
        gu = (a.get("git_url") or "").strip()
        if gu and (a.get("platform") or "").strip().lower() != "github":
            plat, slug = platform_and_slug(gu)
            if plat == "github" and slug:
                a["platform"], a["repo"] = "github", slug
                promoted += 1
    if promoted:
        console.print(f"  [dim]promoted to github identity "
                      f"(rewritten URL):[/dim] [cyan]{promoted}[/cyan]")

    # Split URLs: github (by platform) vs non-github (by URL)
    github_repos: set[str] = set()
    nongithub_urls: set[str] = set()
    for a in aggs:
        repo = (a.get("repo") or "").strip().lower()
        gu = (a.get("git_url") or "").strip()
        if (a.get("platform") or "").strip().lower() == "github" and "/" in repo:
            github_repos.add(repo)
        elif gu:
            nongithub_urls.add(gu)

    console.print(f"  github repos:    {len(github_repos):,}")
    console.print(f"  non-github URLs: {len(nongithub_urls):,}")

    # --- GitHub side ---
    repos_list = sorted(github_repos)
    owners_list = owners_from_repos(repos_list)
    fetch_and_persist(
        repos=repos_list,
        owners=owners_list,
        target="repos",  # don't fetch users here — that's eligibility's job
        force=force,
        quiet=False,
    )
    # Read back validity + identity. `data/sources/github/repos.csv` is keyed by the
    # slug we *asked* for (`repo`); `full_name` is the repo's current name
    # (differs when GitHub redirected us through a rename) and `repo_id` its
    # stable numeric id.
    gh_meta: dict[str, dict] = {}
    if GH_REPOS_FILE.exists():
        with open(GH_REPOS_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                slug = (r.get("repo") or "").strip().lower()
                if slug:
                    gh_meta[slug] = r

    # --- Non-github side ---
    nongithub_ok = _verify_non_github(sorted(nongithub_urls), force=force)

    # --- Annotate identity (repo_id) + canonicalise renamed slugs ---
    # Validity verdicts are NOT written onto the rows here — they live in
    # `validation.csv` / the `valid` column produced by the separate
    # `build_validation` step (which rolls up the two caches refreshed above).
    # This loop only sets `repo_id` (identity), rewrites `repo` to its current
    # post-rename name (github), re-derives `(platform, repo)` for non-github
    # rows off the canonicalised URL, and tallies the summary counts directly
    # from the caches (gh_meta / nongithub_ok).
    renamed = 0
    valid_rows = invalid_rows = no_url = 0
    invalid_examples: list[tuple[str, str, str]] = []  # (kind, key, url)
    for a in aggs:
        repo = (a.get("repo") or "").strip().lower()
        gu = (a.get("git_url") or "").strip()
        has_gh = (a.get("platform") or "").strip().lower() == "github" and "/" in repo

        meta = gh_meta.get(repo) if has_gh else None
        is_valid = bool(meta) and meta.get("valid", "").lower() == "true"

        # repo_id: `gh/<numeric>` — GitHub's stable repo id, namespaced by
        # platform. Only a repo that resolved (HTTP 200) carries one; sparse
        # 404 rows and every non-github repo have none.
        rid = (meta.get("repo_id") or "").strip() if is_valid else ""
        a["repo_id"] = f"gh/{rid}" if rid else ""

        # mirror_url: the upstream a GitHub *mirror* repo syncs from (GitHub's
        # own `mirror_url`), e.g. gcc-mirror/gcc → git://gcc.gnu.org/git/gcc.git.
        # Non-empty only for github mirror repos; empty for ordinary repos and
        # every non-github / orphan row.
        a["mirror_url"] = (meta.get("mirror_url") or "").strip() if is_valid else ""

        # Tally by the row's single target. github rows by the GitHub API
        # verdict; non-github rows by the ls-remote result; the rest no-URL.
        if has_gh:
            if is_valid:
                valid_rows += 1
            else:
                invalid_rows += 1
                invalid_examples.append(("gh", repo, gu))
        elif gu:
            if nongithub_ok.get(gu, False):
                valid_rows += 1
            else:
                invalid_rows += 1
                invalid_examples.append(("git", a.get("top_eco_pkg", ""), gu))
        else:
            no_url += 1

        if has_gh:
            # repo: rewrite to the repo's *current* name. The GitHub API
            # follows renames, so `full_name` is the live owner/repo even when
            # we queried a stale slug. Only a validated repo is canonicalised.
            if is_valid:
                full = (meta.get("full_name") or "").strip().lower()
                if full and "/" in full and full != repo:
                    a["repo"] = full
                    renamed += 1
            # git_url: every github repo also carries its canonical clone URL,
            # so a valid repo has both an owner/repo slug AND a git_url. Derive
            # it from the post-rename slug.
            final = (a.get("repo") or "").strip().lower()
            if final and "/" in final:
                a["git_url"] = f"https://github.com/{final}.git"
        else:
            # non-github: the mechanical canonicalisation may have rewritten
            # git_url onto a different host, so re-derive (platform, repo) from
            # the final URL to keep the identity and URL consistent.
            a["platform"], a["repo"] = platform_and_slug(gu) if gu else ("", "")

    if renamed:
        console.print(f"  [dim]github repo canonicalised to current "
                      f"name:[/dim] [cyan]{renamed}[/cyan]")

    return aggs, {
        "valid": valid_rows, "invalid": invalid_rows, "no_url": no_url,
        "invalid_examples": invalid_examples[:30],
    }


def _print_git_validity_table(stats: dict) -> None:  # pragma: no cover
    """Show counts + sample of invalid URLs."""
    total = stats["valid"] + stats["invalid"] + stats["no_url"]
    table = Table(title="[bold]Git URL Validity[/bold]",
                  header_style="bold dim", padding=(0, 1))
    table.add_column(""); table.add_column("Rows", justify="right")
    table.add_column("%", justify="right")

    def row(label, n, color=""):
        pct = f"{100*n/total:.1f}%" if total else "-"
        if color:
            table.add_row(f"[{color}]{label}[/{color}]",
                          f"[{color}]{n:,}[/{color}]",
                          f"[{color}]{pct}[/{color}]")
        else:
            table.add_row(label, f"{n:,}", pct)

    row("Valid",        stats["valid"],   "green")
    row("Invalid",      stats["invalid"], "red")
    row("No git URL",   stats["no_url"],  "dim")
    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(table)

    if stats["invalid_examples"]:
        sample = Table(title=f"[red]Invalid URLs[/red] (first {len(stats['invalid_examples'])})",
                       header_style="bold dim", padding=(0, 1))
        sample.add_column("kind", width=4); sample.add_column("repo / pkg", min_width=22)
        sample.add_column("git_url")
        for kind, key, url in stats["invalid_examples"]:
            sample.add_row(kind, key, url)
        console.print(sample)


def main() -> None:  # pragma: no cover
    """Read value-data.csv, verify every git URL, write the file back."""
    console.print("[bold]Verifying value-data git URLs...[/bold]\n")
    with open(OUTPUT_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rows, vstats = verify_urls_in_aggregates(rows)
    write_value_data(rows)

    console.print()
    _print_git_validity_table(vstats)
    console.print(f"\n[dim]Verified {len(rows):,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":  # pragma: no cover
    main()
