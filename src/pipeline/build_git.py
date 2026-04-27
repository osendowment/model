#!/usr/bin/env python3
"""Build per-ecosystem `git.csv` capturing upstream Git URLs across hosting platforms.

The existing pipeline only kept `github_repo` and discarded everything else.
This script re-scans the same raw inputs and writes a richer table per
ecosystem at `data/{ecosystem}/git.csv` with the schema:

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

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

PLATFORMS = ("github", "gitlab", "bitbucket", "sourcehut", "codeberg", "custom")
GIT_FIELDS = ["package"] + list(PLATFORMS)


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
        # GitLab supports nested groups (group/subgroup/repo). Take first 2 — covers most cases.
        return ("gitlab", _slug(host, *parts[:2]))

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


# ── per-ecosystem URL collectors ───────────────────────────────────────────────


def npm_urls() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "npm" / "nice-registry" / "packages.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["package"]] = [r.get("repo_url", "")]
    return out


def pypi_urls() -> dict[str, list[str]]:
    """PyPI raw mapping is github-only -- BigQuery extract pre-filtered. Captured for completeness."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "pypi" / "raw" / "package-github-mapping.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["package"]] = [r.get("github_url", "")]
    return out


def crates_urls() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "crates" / "db-dump" / "crates.csv"
    if not path.exists():
        return out
    df = pl.read_csv(path, columns=["name", "homepage", "repository"],
                     schema_overrides={"homepage": pl.Utf8, "repository": pl.Utf8})
    for name, hp, rp in zip(df["name"].to_list(), df["homepage"].to_list(), df["repository"].to_list()):
        out[name] = [rp or "", hp or ""]
    return out


def debian_urls() -> dict[str, list[str]]:
    """Aggregate by source package: union of homepage + vcs_browser across binaries."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "debian" / "raw" / "package-metadata.csv"
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
    path = DATA_DIR / "homebrew" / "raw" / "formulas.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["name"]] = [r.get("homepage", ""), r.get("source_url", "")]
    return out


def ossfuzz_main_repos() -> dict[str, str]:
    """{project: main_repo} from OSS-Fuzz. Many C/C++ projects only publish git via OSS-Fuzz."""
    out: dict[str, str] = {}
    path = DATA_DIR / "ossfuzz" / "projects.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            url = (r.get("main_repo") or "").strip()
            if url:
                out[r["project"]] = url
    return out


def repology_urls_lookup() -> dict[str, list[str]]:
    """{project: [candidate_url, ...]} from Repology HTML scrape (`fetch_repology_urls.py`)."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "repology" / "project-urls.csv"
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["project"], []).append(r["candidate_url"])
    return out


def cpp_urls() -> dict[str, list[str]]:
    """For the unified C/C++ ecosystem, collect URLs from debian + homebrew + oss-fuzz."""
    out: dict[str, list[str]] = {}
    path = DATA_DIR / "cpp" / "raw" / "packages.csv"
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
            for src in (r.get("debian_sources", "") or "").split(","):
                src = src.strip()
                if src and src in deb:
                    for u in deb[src]:
                        if u and u not in urls:
                            urls.append(u)
            for fm in (r.get("homebrew_formulas", "") or "").split(","):
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
    "debian": "source",
    "homebrew": "formula",
}


def universe(ecosystem: str) -> set[str]:
    path = DATA_DIR / ecosystem / "results.csv"
    if not path.exists():
        return set()
    col = PKG_COL[ecosystem]
    with open(path, encoding="utf-8") as f:
        return {r[col] for r in csv.DictReader(f)}


def before_github_count(ecosystem: str) -> int:
    """How many rows in current results.csv have a non-empty github_repo."""
    path = DATA_DIR / ecosystem / "results.csv"
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


def build(ecosystem: str, get_urls) -> dict:
    raw_urls = get_urls()
    pkgs = universe(ecosystem)

    rows: list[dict] = []
    counts = {p: 0 for p in PLATFORMS}
    any_count = 0
    for pkg in sorted(pkgs):
        merged = merge_urls(raw_urls.get(pkg, []))
        rows.append({"package": pkg, **merged})
        has_any = False
        for p in PLATFORMS:
            if merged[p]:
                counts[p] += 1
                has_any = True
        if has_any:
            any_count += 1

    out_path = DATA_DIR / ecosystem / "git.csv"
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
    results_path = DATA_DIR / ecosystem / "results.csv"
    git_path = DATA_DIR / ecosystem / "git.csv"
    if not results_path.exists() or not git_path.exists():
        return (0, 0)

    git_lookup: dict[str, str] = {}
    with open(git_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for p in PLATFORMS:
                if r[p]:
                    git_lookup[r["package"]] = r[p]
                    break

    with open(results_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        original_fields = list(reader.fieldnames or [])

    if "git" in original_fields:
        new_fields = original_fields
    elif "github_repo" in original_fields:
        new_fields = []
        for col in original_fields:
            new_fields.append(col)
            if col == "github_repo":
                new_fields.append("git")
    else:
        # No github_repo — append git at the end.
        new_fields = original_fields + ["git"]

    pkg_col = PKG_COL[ecosystem]
    n_with = 0
    for r in rows:
        r["git"] = git_lookup.get(r[pkg_col], "")
        if r["git"]:
            n_with += 1

    tmp = results_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=new_fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(results_path)

    return (n_with, len(rows))


def main() -> None:
    console.rule("[bold white]build_git.py[/bold white]")
    stats = [build(eco, fn) for eco, fn in ECOSYSTEMS]
    console.print()
    print_summary(stats)
    console.print()
    console.print("[bold]Injecting `git` column into results.csv[/bold]\n")
    for s in stats:
        n, total = update_results_csv(s["ecosystem"])
        pct = 100.0 * n / total if total else 0.0
        console.print(f"  {s['ecosystem']:>9}: [green]{n:,}[/green]/{total:,} rows have a git URL  ({pct:.0f}%)")


if __name__ == "__main__":
    main()
