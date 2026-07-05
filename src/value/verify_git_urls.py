#!/usr/bin/env python3
"""Value pipeline step — verify every repo's git URL accepts a connection.

Runs after `unify_value_data` has written `data/value/value.csv`. Reads that
file back, refreshes the two validation caches, sets the `repo_id`
(identity) column, canonicalises a github `repo` to its current (post-rename)
name, and rewrites the file.

Two strategies, chosen per row's `platform`:

  1. platform == github → `src.sources.github.fetch_repo_owner_data.fetch_and_persist`
     hits `/repos/{owner}/{repo}` via the GitHub API. Persists richer
     metadata to `data/sources/github/repos.csv` (valid, license, owner,
     stars, …) so downstream pipelines (eligibility) don't re-fetch. The
     `repo_id` column is set to `gh/<numeric>`.
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
import datetime as dt
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.value.build_git_urls import platform_and_slug
from src.value.unify_value_data import OUTPUT_FILE, write_value_data

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_VALIDITY_CACHE = DATA_DIR / "sources" / "git" / "urls.csv"

GIT_URL_TTL_DAYS = 365
LS_REMOTE_TIMEOUT = 25
LSREMOTE_PARALLEL = 5

_VALIDITY_FIELDS = ["url", "valid", "method", "checked_at"]

# Hosts that don't support the unauthenticated `git://` daemon — keep / fall
# back to https://. Most modern GitLab installs, Bitbucket, KDE Invent,
# Phabricator, Codeberg, OpenLDAP, postgresql.org, qemu.org, etc.
HTTPS_ONLY_HOST_PREFIXES: tuple[str, ...] = (
    "gitlab.",                  # GitLab Enterprise / .com
    "invent.kde.org",
    "salsa.debian.org",
    "code.videolan.org",
    "bitbucket.org",
    "dev.gnupg.org",            # Phabricator
    "chromium.googlesource.com",  # gitiles
    "aomedia.googlesource.com",
    # Hosts that historically supported git:// but disabled it later:
    "git.openldap.org",
    "git.postgresql.org",
    "codeberg.org",
    "git.libssh.org",
    "git.qemu.org",
    "pagure.io",
)


def _host(url: str) -> str:
    """Extract the host component from a URL, or '' if not parseable."""
    if "//" not in url:
        return ""
    return url.split("//", 1)[1].split("/", 1)[0]


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_validity_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            u = (r.get("url") or "").strip()
            if u:
                out[u] = r
    return out


def _save_validity_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_VALIDITY_FIELDS,
                           quoting=csv.QUOTE_MINIMAL, extrasaction="ignore")
        w.writeheader()
        for u in sorted(cache.keys()):
            w.writerow(cache[u])
    os.replace(tmp, path)


def _is_fresh(checked_at: str) -> bool:
    if not checked_at:
        return False
    try:
        when = dt.datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=GIT_URL_TTL_DAYS)
        return when >= cutoff
    except Exception:
        return False


def _lsremote_pass(urls: list[str]) -> dict[str, bool]:
    """Definitive `git ls-remote` check for the ambiguous URLs."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_HTTP_LOW_SPEED_LIMIT": "500",
        "GIT_HTTP_LOW_SPEED_TIME": "20",
    }

    def _check(url: str) -> tuple[str, bool]:
        try:
            r = subprocess.run(
                ["git", "ls-remote", "--exit-code", url, "HEAD"],
                capture_output=True, timeout=LS_REMOTE_TIMEOUT,
                env=env, text=True,
            )
            return url, (r.returncode == 0 and bool(r.stdout.strip()))
        except subprocess.TimeoutExpired:
            return url, False
        except Exception:
            return url, False

    out: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=LSREMOTE_PARALLEL) as ex:
        futs = {ex.submit(_check, u): u for u in urls}
        for fut in as_completed(futs):
            u, ok = fut.result()
            out[u] = ok
    return out


def _canonicalize_git_url(url: str) -> str:
    """Rewrite known-wrong git URL patterns to their canonical clone form.

    Pipeline of rewrites — each rule UPDATES `u` rather than returning
    early, so post-processing (github-strip, https→git:// conversion)
    runs unconditionally at the end.

    Returns the cleaned URL, or `""` if there's no git equivalent
    (non-git VCS like Mercurial, dead servers, github URL — github URLs
    are tracked via the `repo` / `platform` columns).
    """
    u = (url or "").strip()
    if not u:
        return ""

    # ── Pattern rewrites (mechanical) ─────────────────────────────────
    # Strip gitweb display-suffixes (;a=summary, ;a=tree, etc.)
    u = re.sub(r";[a-z]=[\w]+(;[a-z]=[\w]+)*\b", "", u)

    # Savannah cgit/git web view → git:// (HTTPS smart-http is broken/slow,
    # git:// works instantly; path drops the `/git/` prefix).
    m = re.match(r"^https?://git\.savannah\.(gnu|nongnu)\.org/(?:cgit|git)/(.+)$", u)
    if m:
        u = f"git://git.savannah.{m.group(1)}.org/{m.group(2)}"

    # kernel.org cgit web view → smart-http git endpoint at /pub/scm/...
    m = re.match(r"^https?://git\.kernel\.org/cgit/(.+?)(?:\.git/?)?$", u)
    if m:
        u = f"https://git.kernel.org/pub/scm/{m.group(1)}.git"

    # GnuPG gitweb → use gpg/* GitHub mirror (their smart-http is broken)
    m = re.match(r"^https?://git\.gnupg\.org/cgi-bin/gitweb\.cgi\?p=([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # GnuPG direct .git URL → also use gpg/* GitHub mirror
    m = re.match(r"^https?://git\.gnupg\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://github.com/gpg/{m.group(1)}.git"

    # Sourceware project landing page → git endpoint at /git/X.git
    m = re.match(r"^https?://sourceware\.org/([a-z][\w-]*)/?$", u)
    if m and m.group(1) not in ("git", "cgit", "elfutils-doc"):
        u = f"https://sourceware.org/git/{m.group(1)}.git"

    # VideoLAN gitweb → code.videolan.org GitLab
    m = re.match(r"^https?://git\.videolan\.org/\?p=([\w.-]+)\.git", u)
    if m:
        u = f"https://code.videolan.org/videolan/{m.group(1)}.git"

    # Linux-NFS gitweb (?p=user/proj.git) → git:// form
    m = re.match(r"^https?://git\.linux-nfs\.org/\?p=([^/]+)/([\w.-]+)\.git", u)
    if m:
        u = f"git://git.linux-nfs.org/projects/{m.group(1)}/{m.group(2)}.git"

    # freedesktop anongit → gitlab.freedesktop.org
    m = re.match(r"^https?://anongit\.freedesktop\.org/git/(.+)\.git$", u)
    if m:
        u = f"https://gitlab.freedesktop.org/{m.group(1)}.git"

    # OpenLDAP archive download URL → main repo
    if u.startswith("https://git.openldap.org/openldap/openldap/-/archive/"):
        u = "https://git.openldap.org/openldap/openldap.git"

    # Xiph old git.xiph.org → gitlab.xiph.org
    m = re.match(r"^https?://git\.xiph\.org/([\w.-]+)\.git$", u)
    if m:
        u = f"https://gitlab.xiph.org/xiph/{m.group(1)}.git"

    # SourceForge legacy <project>.git.sourceforge.net → modern git.code.sf.net
    m = re.match(r"^https?://([\w-]+)\.git\.sourceforge\.net/?$", u)
    if m:
        u = f"https://git.code.sf.net/p/{m.group(1)}/code"

    # GitLab API URL (graphviz et al.) — not a git endpoint
    if u.startswith("https://gitlab.com/api/v4/") or "/api/v4/projects/" in u:
        u = "https://gitlab.com/graphviz/graphviz.git" if "4207231" in u else ""

    # Postgres canonical missing .git suffix
    if u == "https://git.postgresql.org/git/postgresql":
        u = "https://git.postgresql.org/git/postgresql.git"

    # libpthread-stubs path mismatch on gitlab.freedesktop.org
    if u == "https://gitlab.freedesktop.org/xorg/lib/libpthread-stubs.git":
        u = "https://gitlab.freedesktop.org/xorg/lib/pthread-stubs.git"

    # ── Post-processing (always runs) ─────────────────────────────────
    if not u:
        return ""

    # Non-git VCS — blank out, no canonical git exists
    if u.startswith(("https://hg.", "http://hg.",
                     "https://foss.heptapod.net/",
                     "https://code.launchpad.net/",
                     "https://core.tcl-lang.org/",
                     "https://gmplib.org/repo/",
                     "https://bitbucket.org/stoneleaf/",
                     "https://www.bytereef.org/")):
        return ""

    # Drop the raw github URL here; the verification loop re-derives the
    # canonical github clone URL from the (post-rename) `repo` slug, so every
    # github repo ends up with both an owner/repo slug and a git_url.
    if re.match(r"^https?://github\.com/", u):
        return ""

    # Standardise on git:// scheme for non-github URLs unless the host
    # explicitly disabled the unauthenticated git daemon — those hosts
    # are listed at module level in HTTPS_ONLY_HOST_PREFIXES.
    host = _host(u)
    https_only = any(host == h or host.startswith(h)
                     for h in HTTPS_ONLY_HOST_PREFIXES)

    if https_only:
        # Convert any scheme to https:// for these hosts
        if u.startswith("git://"):
            return "https://" + u[len("git://"):]
        if u.startswith("http://"):
            return "https://" + u[len("http://"):]
        return u
    # Otherwise prefer git:// — it's faster and tends to work on traditional
    # git daemons (Savannah, kernel.org, sourceware, sourceforge, …).
    if u.startswith("https://"):
        return "git://" + u[len("https://"):]
    if u.startswith("http://"):
        return "git://" + u[len("http://"):]
    return u


def _verify_non_github(urls: list[str],
                       cache_path: Path = GIT_VALIDITY_CACHE,
                       force: bool = False) -> dict[str, bool]:
    """`git ls-remote` verification for non-github URLs, with TTL cache."""
    cache = _load_validity_cache(cache_path)
    now = _now_iso()

    fresh: dict[str, bool] = {}
    to_check: list[str] = []
    for u in urls:
        c = cache.get(u)
        if not force and c and _is_fresh(c.get("checked_at", "")):
            fresh[u] = (c.get("valid", "").lower() == "true")
        else:
            to_check.append(u)

    console.print(
        f"  [dim]non-github URLs:[/dim] {len(urls):,} unique  "
        f"[green]{len(fresh):,} cached[/green]  "
        f"[yellow]{len(to_check):,} to verify[/yellow]"
    )

    new_results: dict[str, bool] = {}
    if to_check:
        t0 = time.monotonic()
        results = _lsremote_pass(to_check)
        ok = sum(1 for v in results.values() if v)
        console.print(
            f"  [dim]ls-remote ({time.monotonic()-t0:.1f}s):[/dim] "
            f"valid={ok}/{len(to_check)}"
        )
        for u, v in results.items():
            new_results[u] = v
            cache[u] = {"url": u, "valid": str(bool(v)),
                        "method": "ls-remote", "checked_at": now}
        _save_validity_cache(cache_path, cache)
    return {**fresh, **new_results}


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
