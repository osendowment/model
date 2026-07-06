#!/usr/bin/env python3
"""Build per-ecosystem `git.csv` capturing upstream Git URLs across hosting platforms.

The existing pipeline only kept `github_repo` and discarded everything else.
This script re-scans the same raw inputs and writes a richer table per
ecosystem at `data/sources/{ecosystem}/git.csv` with the schema:

    package, github, gitlab, bitbucket, sourcehut, codeberg, custom

URLs are normalised to canonical `.git` form when applicable (sourcehut uses
no `.git` suffix). `custom` is a catch-all for any other Git source we
recognise (Savannah, sourceware, kernel.org cgit, KDE/GNOME/freedesktop
anongit, GitWeb, etc.).

Downstream pipelines still use only the `github` column for now -- the rest
is captured for future work (mirror discovery, host-coverage stats).

Usage:
    uv run -m src.build_git
"""

import csv
from pathlib import Path
from urllib.parse import urlparse

import polars as pl
from rich.console import Console
from rich.table import Table

from src.common.lfs import is_lfs_pointer

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

PLATFORMS = ("github", "gitlab", "bitbucket", "sourcehut", "codeberg", "custom")
GIT_FIELDS = ["package"] + list(PLATFORMS) + ["eco_guess"]
# Order in which the `git` column picks across platforms — github wins.
GIT_HOST_PRIORITY = ("github", "gitlab", "codeberg", "sourcehut", "bitbucket", "custom")


# ── URL classification ─────────────────────────────────────────────────────────

CUSTOM_HOSTS = {
    "git.savannah.gnu.org", "git.savannah.nongnu.org",
    "sourceware.org", "git.kernel.org", "code.qt.io",
    "anongit.kde.org", "invent.kde.org",
    "anongit.freedesktop.org", "gitlab.freedesktop.org",  # FdO is GitLab — special case below
    "git.gnome.org", "gitweb.gentoo.org",
    "pagure.io", "src.fedoraproject.org",
    "git.eclipse.org", "review.openstack.org",
}

GIT_INDICATORS = ("git.", "/git/", "/cgit", "/scm/", "/gitweb")


def normalize_url(url: str) -> str:
    """Canonicalise SSH / git+ / git:// to https:// form."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("git+"):
        url = url[4:]
    if url.startswith("git://"):
        url = "https://" + url[len("git://"):]
    elif url.startswith("ssh://git@"):
        url = "https://" + url[len("ssh://git@"):]
    elif url.startswith("git@"):
        # git@github.com:owner/repo.git → https://github.com/owner/repo.git
        try:
            host, path = url[len("git@"):].split(":", 1)
            url = f"https://{host}/{path}"
        except ValueError:
            pass
    return url


def _slug(host: str, *segments: str, suffix: str = ".git") -> str:
    return f"https://{host}/{'/'.join(segments)}{suffix}"


def classify(url: str) -> tuple[str, str]:
    """Return (platform, canonical_url) or ("", "") if not a recognised Git URL.

    The returned URL is fully lowercased — most git hosts are
    case-insensitive on the path and we want consistent hashing/joins.
    """
    url = normalize_url(url).lower()
    if not url:
        return ("", "")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return ("", "")
    if not host:
        return ("", "")

    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]

    if host == "github.com" and len(parts) >= 2:
        return ("github", _slug(host, parts[0].lower(), parts[1].lower()))

    # GitLab: gitlab.com, gitlab.* self-hosted, salsa.debian.org, gitlab.freedesktop.org, gitlab.gnome.org, ...
    if (host.startswith("gitlab.") or host == "salsa.debian.org") and len(parts) >= 2:
        # GitLab allows arbitrarily nested groups — `gitlab.freedesktop.org/xorg/lib/libXau`
        # is a 3-segment project path, not a 2-segment one. Strip GitLab's `/-/` UI
        # separator (e.g. `/-/issues`, `/-/blob/main/...`) and keep all path segments
        # before it as the project. Truncating to 2 segments collapses every
        # xorg/lib/* lib into the same `xorg/lib.git` repo.
        proj_parts: list[str] = []
        for p in parts:
            if p == "-":
                break
            proj_parts.append(p)
        if len(proj_parts) >= 2:
            return ("gitlab", _slug(host, *proj_parts))

    if host == "bitbucket.org" and len(parts) >= 2:
        return ("bitbucket", _slug(host, parts[0].lower(), parts[1].lower()))

    if host == "git.sr.ht" and len(parts) >= 2:
        # sourcehut: ~user/repo, no .git suffix
        return ("sourcehut", f"https://git.sr.ht/{parts[0]}/{parts[1]}")

    if host == "codeberg.org" and len(parts) >= 2:
        return ("codeberg", _slug(host, parts[0].lower(), parts[1].lower()))

    # Custom: anything else that looks like a git host or has git markers.
    looks_like_git = (
        host in CUSTOM_HOSTS
        or url.lower().endswith(".git")
        or any(ind in url.lower() for ind in GIT_INDICATORS)
    )
    if looks_like_git:
        return ("custom", url)

    return ("", "")


def merge_urls(urls: list[str]) -> dict[str, str]:
    """Pick the first URL per platform from a list of candidate URLs."""
    out: dict[str, str] = {p: "" for p in PLATFORMS}
    for u in urls:
        plat, canon = classify(u)
        if plat and not out[plat]:
            out[plat] = canon
    return out


def platform_and_slug(url: str) -> tuple[str, str]:
    """Return `(platform, repo_slug)` for a git URL, or `("", "")` if unrecognised.

    The single source of truth for the `platform` / `repo` columns in
    `data/value/value.csv`. `platform` is the host class from `classify`
    (github, gitlab, bitbucket, sourcehut, codeberg, custom); `repo` is the
    project path on that host:

      - github / bitbucket / codeberg → `owner/repo` (first two path segments)
      - gitlab                        → the full nested path (`xorg/lib/libx11`);
                                        GitLab allows arbitrarily deep groups
      - sourcehut                     → `~user/repo`
      - custom                        → best-effort full path (`gettext`,
                                        `binutils-gdb`); may be empty for
                                        query-string gitweb URLs that carry no
                                        clean path.

    The slug is derived from `classify`'s already-canonicalised, lowercased
    URL, so it is stable regardless of the input scheme (git://, ssh, https).
    """
    plat, canon = classify(url)
    if not plat:
        return ("", "")
    path = urlparse(canon).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if plat in ("github", "bitbucket", "codeberg"):
        slug = "/".join(parts[:2]) if len(parts) >= 2 else path
    else:  # gitlab (nested), sourcehut (~user/repo), custom (full path)
        slug = "/".join(parts)
    return (plat, slug.lower())


# ── Validity-aware merge ───────────────────────────────────────────────────────
#
# Source-priority for picking each platform slot is native > eco. But if
# the priority candidate is KNOWN-invalid in the validity cache, fall through
# to the next candidate. Unknown URLs (no cache entry) get the benefit of the
# doubt and win on priority — they'll get validated by `value.py` later, and
# the cache will inform the next build.
#
# Two caches feed this lookup:
#   • data/sources/git/urls.csv          — non-github URLs (ls-remote results, by URL)
#   • data/sources/github/repos.csv      — github repos (API results, by owner/repo slug)


def _load_invalid_lookup():
    """Return `is_invalid(url) -> bool`, True only if KNOWN invalid."""
    import re
    invalid_urls: set[str] = set()
    invalid_slugs: set[str] = set()

    nongh = DATA_DIR / "sources" / "git" / "urls.csv"
    if nongh.exists():
        with open(nongh, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if (r.get("valid") or "").lower() == "false":
                    url = (r.get("url") or "").strip()
                    if url:
                        invalid_urls.add(url)

    gh_repos = DATA_DIR / "sources" / "github" / "repos.csv"
    if gh_repos.exists():
        with open(gh_repos, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if (r.get("valid") or "").lower() == "false":
                    slug = (r.get("repo") or "").strip().lower()
                    if slug:
                        invalid_slugs.add(slug)

    gh_url_re = re.compile(r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$",
                           re.IGNORECASE)

    def is_invalid(url: str) -> bool:
        if not url:
            return False
        if url in invalid_urls:
            return True
        m = gh_url_re.match(url)
        if m:
            slug = f"{m.group(1)}/{m.group(2)}".lower()
            return slug in invalid_slugs
        return False

    return is_invalid


def merge_urls_with_source(
    sources: dict[str, list[str]],
    is_invalid,
) -> tuple[dict[str, str], dict[str, str]]:
    """Pick the first not-known-invalid URL per platform across labelled source lists.

    `sources` is an ordered dict like {"native": [...], "eco": [...]}.
    Iteration order is the source priority. Returns (winners, source_label_per_platform).
    If every candidate for a slot is known-invalid, keeps the first one anyway
    (so `git_url` doesn't silently drop to empty — value.py's verifier will
    re-flag it).
    """
    candidates: dict[str, list[tuple[str, str]]] = {p: [] for p in PLATFORMS}
    for label, urls in sources.items():
        for u in urls:
            plat, canon = classify(u)
            if not plat:
                continue
            if any(c == canon for c, _ in candidates[plat]):
                continue
            candidates[plat].append((canon, label))

    winners: dict[str, str] = {p: "" for p in PLATFORMS}
    sources_used: dict[str, str] = {p: "" for p in PLATFORMS}
    for plat, cands in candidates.items():
        chosen = None
        for canon, label in cands:
            if not is_invalid(canon):
                chosen = (canon, label)
                break
        if chosen is None and cands:
            chosen = cands[0]  # all known-invalid — keep first for visibility
        if chosen:
            winners[plat], sources_used[plat] = chosen
    return winners, sources_used


# ── per-ecosystem URL collectors ───────────────────────────────────────────────


def npm_urls() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "sources" / "npm" / "nice-registry" / "packages.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["package"]] = [r.get("repo_url", "")]
    return out


def pypi_urls() -> dict[str, list[str]]:
    """PyPI URLs from `package-urls.csv` if present (full project_urls + home_page),
    falling back to the legacy github-only `package-github-mapping.csv`.

    Run `uv run -m src.sources.pypi.fetch_pypi_urls` to populate the rich source.
    """
    out: dict[str, list[str]] = {}
    rich = DATA_DIR / "sources" / "pypi" / "raw" / "package-urls.csv"
    if rich.exists():
        with open(rich, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out.setdefault(r["package"], []).append(r["url"])
        return out
    legacy = DATA_DIR / "sources" / "pypi" / "raw" / "package-github-mapping.csv"
    if not legacy.exists():
        return out
    with open(legacy, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["package"]] = [r.get("github_url", "")]
    return out


def crates_urls() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = DATA_DIR.parent / "tmp" / "crates-db-dump" / "crates.csv"
    if not path.exists():
        return out
    if is_lfs_pointer(path):
        raise SystemExit(
            f"{path} is a Git LFS pointer — stray from when the dump lived in git. "
            "Delete it and run `uv run python -m src.sources.crates.fetch_db_dump`, "
            "or use `--rollup` to rebuild value.csv from existing results.csv."
        )
    df = pl.read_csv(path, columns=["name", "homepage", "repository"],
                     schema_overrides={"homepage": pl.Utf8, "repository": pl.Utf8})
    for name, hp, rp in zip(df["name"].to_list(), df["homepage"].to_list(), df["repository"].to_list()):
        out[name] = [rp or "", hp or ""]
    return out


def debian_urls() -> dict[str, list[str]]:
    """Aggregate by source package: union of homepage + vcs_browser across binaries."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "sources" / "debian" / "raw" / "package-metadata.csv"
    if not path.exists():
        return out
    by_source: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            source = r.get("source") or r.get("package", "")
            urls = by_source.setdefault(source, [])
            for col in ("vcs_browser", "homepage"):  # vcs_browser preferred
                u = r.get(col, "").strip()
                if u and u not in urls:
                    urls.append(u)
    return by_source


def homebrew_urls() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "sources" / "homebrew" / "raw" / "formulas.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["name"]] = [r.get("homepage", ""), r.get("source_url", "")]
    return out


def ossfuzz_main_repos() -> dict[str, str]:
    """{project: main_repo} from OSS-Fuzz. Many C/C++ projects only publish git via OSS-Fuzz."""
    out: dict[str, str] = {}
    path = DATA_DIR / "sources" / "ossfuzz" / "projects.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            url = (r.get("main_repo") or "").strip()
            if url:
                out[r["project"]] = url
    return out


def repology_urls_lookup() -> dict[str, list[str]]:
    """{project: [candidate_url, ...]} from the Repology scrape (`fetch_repology_urls.py`).

    The CSV doubles as a fetch cache, so it also holds sentinel rows (blank
    `candidate_url`, `status` in none/not_found/error) recording checked-but-
    empty projects — skip those, keep only real URLs.
    """
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "sources" / "repology" / "project-urls.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            url = (r.get("candidate_url") or "").strip()
            if url:
                out.setdefault(r["project"], []).append(url)
    return out


def cpp_urls() -> dict[str, list[str]]:
    """For the unified C/C++ ecosystem, collect URLs from debian + homebrew + oss-fuzz."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "sources" / "cpp" / "raw" / "packages.csv"
    if not path.exists():
        return out

    deb = debian_urls()
    brew = homebrew_urls()
    fuzz = ossfuzz_main_repos()
    repology = repology_urls_lookup()

    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            project = r["project"]
            urls: list[str] = []
            # cpp/raw/packages.csv uses `|` as the multi-value separator
            # (e.g. "tree-sitter|tree-sitter@0.25", "openssl@3|openssl@3.0|openssl@3.5"),
            # NOT `,`. Splitting on `,` collapses the whole list into a single
            # bogus key and the lookup misses every constituent formula/source.
            for src in (r.get("debian_sources", "") or "").split("|"):
                src = src.strip()
                if src and src in deb:
                    for u in deb[src]:
                        if u and u not in urls:
                            urls.append(u)
            for fm in (r.get("homebrew_formulas", "") or "").split("|"):
                fm = fm.strip()
                if fm and fm in brew:
                    for u in brew[fm]:
                        if u and u not in urls:
                            urls.append(u)
            # OSS-Fuzz `main_repo` — high-quality git URL for security-critical projects.
            if project in fuzz and fuzz[project] not in urls:
                urls.append(fuzz[project])
            # Repology HTML scrape — covers gnu projects, freedesktop, gitlab.gnome, etc.
            for u in repology.get(project, []):
                if u and u not in urls:
                    urls.append(u)
            out[project] = urls
    return out


# ── universe per ecosystem (rows that survived the pipeline) ──────────────────

PKG_COL = {
    "npm": "package",
    "pypi": "package",
    "crates": "package",
    "cpp": "package",
    "debian": "package",
    "homebrew": "package",
}


def universe(ecosystem: str) -> set[str]:
    path = DATA_DIR / "sources" / ecosystem / "results.csv"
    if not path.exists():
        return set()
    col = PKG_COL[ecosystem]
    with open(path, encoding="utf-8") as f:
        return {r[col] for r in csv.DictReader(f)}


def before_github_count(ecosystem: str) -> int:
    """How many rows in current results.csv have a non-empty github_repo."""
    path = DATA_DIR / "sources" / ecosystem / "results.csv"
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for r in csv.DictReader(f) if (r.get("github_repo") or "").strip())


# ── orchestration ──────────────────────────────────────────────────────────────


ECOSYSTEMS = [
    ("npm", npm_urls),
    ("pypi", pypi_urls),
    ("crates", crates_urls),
    ("debian", debian_urls),
    ("homebrew", homebrew_urls),
    ("cpp", cpp_urls),
]


def _pick_git(merged: dict[str, str]) -> str:
    """Same host-priority pick that unify_value_data uses for the `git` column."""
    for plat in GIT_HOST_PRIORITY:
        if merged.get(plat):
            return merged[plat]
    return ""


def _load_ecosystems_urls(ecosystem: str) -> dict[str, list[str]]:
    """Load ecosyste.ms-derived URLs from data/sources/{eco}/raw/ecosystems.csv.

    Returns {package: [repository_url, homepage]} skipping empty values.
    `merge_urls()` will classify each URL into the right platform slot.
    """
    path = DATA_DIR / "sources" / ecosystem / "raw" / "ecosystems.csv"
    if not path.exists():
        return {}
    out: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            urls = [u for u in (r.get("repository_url"), r.get("homepage")) if (u or "").strip()]
            if urls:
                out[r["package"]] = urls
    return out


def build(ecosystem: str, get_urls) -> dict:
    raw_urls = get_urls()
    pkgs = universe(ecosystem)
    eco_cache = _load_ecosystems_urls(ecosystem)  # ecosyste.ms backfill (run via src.sources.ecosystems.packages)
    is_invalid = _load_invalid_lookup()  # cache-backed validator; unknown URLs → win on priority

    rows: list[dict] = []
    counts = {p: 0 for p in PLATFORMS}
    any_count = 0
    n_eco_github = n_eco_git = 0
    for pkg in sorted(pkgs):
        native = list(raw_urls.get(pkg, []))
        eco = [u for u in eco_cache.get(pkg, []) if u not in native]

        # Validity-aware merge across source priority native > eco.
        # If a higher-priority candidate is known-invalid, fall through to the
        # next one. `sources_used` tells us which source actually won each slot.
        merged_full, sources_used = merge_urls_with_source(
            {"native": native, "eco": eco}, is_invalid,
        )

        # eco_used now reflects what ACTUALLY won, not just availability.
        eco_used: list[str] = []
        if sources_used.get("github") == "eco":
            eco_used.append("github")

        # The `git` URL is whichever platform won by host priority.
        git_plat = next((p for p in GIT_HOST_PRIORITY if merged_full.get(p)), None)
        if git_plat:
            src = sources_used.get(git_plat, "")
            if src == "eco" and "git" not in eco_used:
                eco_used.append("git")

        eco_used.sort()
        if "github" in eco_used:
            n_eco_github += 1
        if "git" in eco_used:
            n_eco_git += 1

        row = {"package": pkg, **merged_full,
               "eco_guess": ",".join(eco_used)}
        rows.append(row)
        has_any = False
        for p in PLATFORMS:
            if merged_full[p]:
                counts[p] += 1
                has_any = True
        if has_any:
            any_count += 1

    out_path = DATA_DIR / "sources" / ecosystem / "git.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GIT_FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    return {
        "ecosystem": ecosystem,
        "total": len(rows),
        "before": before_github_count(ecosystem),
        **counts,
        "any": any_count,
        "eco_github": n_eco_github,
        "eco_git": n_eco_git,
    }


def _print_coverage_table(stats: list[dict]) -> None:
    """Compact before-vs-after table sized for narrow terminals."""
    table = Table(title="[bold]Git URL coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Eco", style="bold")
    table.add_column("Total", justify="right")
    table.add_column("Before", justify="right", style="dim")
    table.add_column("After", justify="right", style="green")
    table.add_column("Δ", justify="right")

    tot = {"total": 0, "before": 0, "any": 0}
    for s in stats:
        delta = s["any"] - s["before"]
        delta_str = f"[green]+{delta}[/green]" if delta > 0 else "[dim]·[/dim]"
        bp = 100.0 * s["before"] / s["total"] if s["total"] else 0.0
        ap = 100.0 * s["any"] / s["total"] if s["total"] else 0.0
        table.add_row(
            s["ecosystem"],
            f"{s['total']:,}",
            f"{s['before']:,} {bp:.0f}%",
            f"{s['any']:,} {ap:.0f}%",
            delta_str,
        )
        tot["total"] += s["total"]
        tot["before"] += s["before"]
        tot["any"] += s["any"]

    bp = 100.0 * tot["before"] / tot["total"] if tot["total"] else 0.0
    ap = 100.0 * tot["any"] / tot["total"] if tot["total"] else 0.0
    delta = tot["any"] - tot["before"]
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{tot['total']:,}[/bold]",
        f"[bold]{tot['before']:,} {bp:.0f}%[/bold]",
        f"[bold green]{tot['any']:,} {ap:.0f}%[/bold green]",
        f"[bold green]+{delta}[/bold green]",
    )
    console.print(table)


def _print_platform_table(stats: list[dict]) -> None:
    """Per-platform counts (non-zero only) for narrow terminals."""
    short = {"github": "gh", "gitlab": "gl", "bitbucket": "bb",
             "sourcehut": "sh", "codeberg": "cb", "custom": "ct"}
    table = Table(title="[bold]Per-platform breakdown[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Eco", style="bold")
    for p in PLATFORMS:
        table.add_column(short[p], justify="right")

    tot = {p: 0 for p in PLATFORMS}
    for s in stats:
        cells = []
        for p in PLATFORMS:
            cells.append(f"{s[p]:,}" if s[p] else "[dim]·[/dim]")
            tot[p] += s[p]
        table.add_row(s["ecosystem"], *cells)

    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        *(f"[bold]{tot[p]:,}[/bold]" if tot[p] else "[dim]·[/dim]" for p in PLATFORMS),
    )
    console.print(table)


def print_summary(stats: list[dict]) -> None:
    _print_coverage_table(stats)
    console.print()
    _print_platform_table(stats)


def update_results_csv(ecosystem: str) -> tuple[int, int]:
    """Inject a `git` column into results.csv right after `github_repo`.

    `git` holds the canonical Git URL for the package — github first, else
    gitlab/bitbucket/sourcehut/codeberg/custom in priority order. Idempotent.
    Returns (rows_with_git, total_rows).
    """
    results_path = DATA_DIR / "sources" / ecosystem / "results.csv"
    git_path = DATA_DIR / "sources" / ecosystem / "git.csv"
    if not results_path.exists() or not git_path.exists():
        return (0, 0)

    git_lookup: dict[str, str] = {}
    eco_guess_lookup: dict[str, str] = {}
    with open(git_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for p in PLATFORMS:
                if r[p]:
                    git_lookup[r["package"]] = r[p]
                    break
            eco_guess_lookup[r["package"]] = (r.get("eco_guess") or "").strip()

    with open(results_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fields = list(reader.fieldnames or [])

    # Layout: insert `git` after `github_repo`, then `eco_guess` right after it.
    # When `git` is freshly inserted (not already a column), `eco_guess` must
    # follow it in the same step — otherwise it falls to the end of the row,
    # since the loop iterates the original columns and never revisits `git`.
    new_fields: list[str] = []
    inserted_git = "git" in original_fields
    inserted_eco = "eco_guess" in original_fields
    for col in original_fields:
        new_fields.append(col)
        if col == "github_repo" and not inserted_git:
            new_fields.append("git")
            inserted_git = True
            if not inserted_eco:
                new_fields.append("eco_guess")
                inserted_eco = True
        if col == "git" and not inserted_eco:
            new_fields.append("eco_guess")
            inserted_eco = True
    if not inserted_git:
        new_fields.append("git")
    if not inserted_eco:
        new_fields.append("eco_guess")

    # Also build a slug lookup from the `github` column in git.csv, used to
    # backfill `github_repo` in results.csv when the per-ecosystem extractor
    # missed it but a downstream source (OSS-Fuzz, Repology) found one.
    github_slug_lookup: dict[str, str] = {}
    with open(git_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gh_url = (r.get("github") or "").strip()
            if not gh_url:
                continue
            # github URL is canonicalised by classify(): https://github.com/owner/repo.git
            try:
                from urllib.parse import urlparse
                tail = urlparse(gh_url).path.lstrip("/")
                if tail.endswith(".git"):
                    tail = tail[:-4]
                parts = [p for p in tail.split("/") if p]
                if len(parts) >= 2:
                    github_slug_lookup[r["package"]] = f"{parts[0].lower()}/{parts[1].lower()}"
            except Exception:
                pass

    pkg_col = PKG_COL[ecosystem]
    n_with = 0
    n_gh_filled = 0
    for r in rows:
        r["git"] = git_lookup.get(r[pkg_col], "")
        if r["git"]:
            n_with += 1
        r["eco_guess"] = eco_guess_lookup.get(r[pkg_col], "")
        # Backfill github_repo slug if missing.
        if "github_repo" in new_fields and not (r.get("github_repo") or "").strip():
            slug = github_slug_lookup.get(r[pkg_col], "")
            if slug:
                r["github_repo"] = slug
                n_gh_filled += 1

    tmp = results_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(results_path)

    return (n_with, len(rows), n_gh_filled)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser()
    p.parse_args()

    console.rule("[bold white]build_git.py[/bold white]")
    stats = [build(eco, fn) for eco, fn in ECOSYSTEMS]
    console.print()
    print_summary(stats)
    console.print()
    console.print("[bold]Injecting `git` column into results.csv[/bold]\n")
    for s in stats:
        n, total, n_gh = update_results_csv(s["ecosystem"])
        pct = 100.0 * n / total if total else 0.0
        extra = f"  [blue](+{n_gh} github_repo backfilled)[/blue]" if n_gh else ""
        console.print(f"  {s['ecosystem']:>9}: [green]{n:,}[/green]/{total:,} rows have a git URL  ({pct:.0f}%){extra}")


if __name__ == "__main__":
    main()
