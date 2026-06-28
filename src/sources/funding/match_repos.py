"""Determine which foundation hosts each repo.

Reads every `data/sources/funding/{slug}/projects.csv` and builds three indexes:
  1. github_slug → host (e.g. `apache/kafka` → apache)
  2. github_org  → host (e.g. `apache/*`    → apache)
  3. apex_domain → host (e.g. `numpy.org`   → numfocus)

…and writes `data/sources/funding/host-by-repo.csv` with one row per repo:
  repo, host, host_source

Matching priority (highest specificity wins):
  1. Exact `owner/name` slug from a foundation CSV.
  2. Org-prefix from a curated list (high-confidence orgs only).
  3. Apex domain or domain-suffix (e.g. `kafka.apache.org` → apache).
  4. Empty (`""`) — repo not hosted by any tracked FOSS foundation.

CNCF wins over LF on overlap (CNCF lives inside the Linux Foundation —
the more specific label is more useful for funding decisions).

Usage:
    uv run python -m src.sources.funding.match_repos
"""

import argparse
import csv
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table

from src.sources.funding._common import DATA_DIR

console = Console()

REPOS_FILE = Path(__file__).resolve().parents[3] / "data" / "sources" / "github" / "search" / "top-repos.csv"
OUT_FILE = DATA_DIR / "host-by-repo.csv"

# Priority order — first match wins. More specific foundations come before
# parent / umbrella ones (CNCF and OpenJS both live under the Linux
# Foundation, so they win over `lf` on overlap).
SLUGS = ["apache", "cncf", "eclipse", "openjs", "psf", "numfocus", "sfc", "lf"]

# Parent foundation hierarchy. When a host has a parent (e.g. CNCF and OpenJS
# both live under the Linux Foundation), the emitted host string is
# `parent/child` so the relationship is encoded in the slug itself.
PARENT_FOUNDATION: dict[str, str] = {
    "cncf":   "lf",
    "openjs": "lf",
    # NumFOCUS, SFC, Apache, Eclipse, PSF are independent legal entities
    # — no parent. PyPA → PSF would qualify but we already collapse PyPA
    # repos to host=psf via the org-prefix rule, not a separate slug.
}


def _qualified(slug: str) -> str:
    """Return `parent/child` for hosts with a parent foundation, else slug."""
    parent = PARENT_FOUNDATION.get(slug)
    return f"{parent}/{slug}" if parent else slug

# GitHub orgs whose repos are essentially always hosted by the foundation.
# Conservative on purpose — only orgs we're sure about. Anything ambiguous
# is left to the per-repo slug match.
ORG_HOST: dict[str, str] = {
    "apache": "apache",
    # Eclipse umbrella orgs.
    "eclipse": "eclipse",
    "eclipse-archived": "eclipse",
    "eclipse-ee4j": "eclipse",
    "eclipse-vertx": "eclipse",
    "eclipse-cdt": "eclipse",
    "eclipse-iceoryx": "eclipse",
    "eclipse-jdt": "eclipse",
    "eclipse-platform": "eclipse",
    "eclipse-pde": "eclipse",
    "eclipse-equinox": "eclipse",
    # CNCF projects mostly live in their own orgs (kubernetes/, prometheus/…)
    # which we capture via the landscape's repo_url, so no org-prefix needed.
    "cncf": "cncf",
    "cncf-tags": "cncf",
    # NumFOCUS umbrella org (not all sponsored projects live here, but the
    # ones that do are unambiguous).
    "numfocus": "numfocus",
    # PSF — both PyPA tooling and the python/* core orgs map cleanly.
    "pypa": "psf",
    "python": "psf",
    # OpenJS hosted projects mostly live in their own orgs (jquery, nodejs,
    # eslint, webpack, expressjs, …) which the project_list match catches.
    "openjsf": "openjs",
    "openjs-foundation": "openjs",
}

# Domain suffixes whose subdomains all belong to a single foundation.
# Catches `kafka.apache.org` → apache, `projects.eclipse.org` → eclipse, etc.
DOMAIN_SUFFIX_HOST: list[tuple[str, str]] = [
    ("apache.org", "apache"),
    ("eclipse.org", "eclipse"),
    ("eclipse.dev", "eclipse"),
    ("cncf.io", "cncf"),
    ("numfocus.org", "numfocus"),
    ("sfconservancy.org", "sfc"),
    ("linuxfoundation.org", "lf"),
    # PSF / PyPA
    ("python.org", "psf"),
    ("pypa.io", "psf"),
    # OpenJS
    ("openjsf.org", "openjs"),
]

# Domains that show up as `homepage_url` for many foundation projects but
# are generic platforms — matching on these causes massive false positives
# (e.g. github.com → "lf" would mark every repo with a github homepage).
GENERIC_DOMAINS: set[str] = {
    "github.com", "gitlab.com", "bitbucket.org", "sourceforge.net",
    "github.io", "gitlab.io", "pages.dev",
    "npmjs.com", "pypi.org", "crates.io", "rubygems.org",
    "docs.rs", "readthedocs.io", "readthedocs.org",
    "twitter.com", "x.com", "facebook.com", "linkedin.com",
    "google.com", "youtube.com", "wikipedia.org",
    "googlegroups.com", "freelists.org", "discord.gg", "slack.com",
}


def _load_foundation_index(slug: str) -> tuple[set[str], set[str]]:
    """Return (set of `owner/name` slugs, set of apex domains) for one foundation."""
    path = DATA_DIR / slug / "projects.csv"
    if not path.exists():
        return set(), set()
    slugs: set[str] = set()
    domains: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gh = (r.get("github_repo") or "").strip().lower()
            if gh and "/" in gh:
                slugs.add(gh)
            d = (r.get("domain") or "").strip().lower()
            if d and "." in d and d not in GENERIC_DOMAINS:
                domains.add(d)
    return slugs, domains


def build_indexes() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Combine all foundation CSVs into slug→host and domain→host mappings.

    Earlier slugs in `SLUGS` win (priority order).
    """
    slug_to_host: dict[str, str] = {}
    domain_to_host: dict[str, str] = {}
    for host in SLUGS:
        slugs, domains = _load_foundation_index(host)
        for s in slugs:
            slug_to_host.setdefault(s, host)
        for d in domains:
            domain_to_host.setdefault(d, host)
    return slug_to_host, ORG_HOST, domain_to_host


def _apex(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _host_from_domain(host_url: str, domain_idx: dict[str, str]) -> str:
    """Match by exact apex domain, then by trailing suffix (e.g. `*.apache.org`)."""
    apex = _apex(host_url)
    if not apex:
        return ""
    if apex in domain_idx:
        return domain_idx[apex]
    for suffix, host in DOMAIN_SUFFIX_HOST:
        if apex == suffix or apex.endswith("." + suffix):
            return host
    return ""


def classify(slug_idx: dict[str, str], org_idx: dict[str, str],
             domain_idx: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    with open(REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = r["repo"].strip()
            key = repo.lower()
            org = key.split("/", 1)[0] if "/" in key else ""
            homepage = (r.get("homepage") or "").strip()

            host, source = "", ""
            if key in slug_idx:
                host, source = slug_idx[key], "project_list"
            elif org in org_idx:
                host, source = org_idx[org], "org_prefix"
            else:
                hd = _host_from_domain(homepage, domain_idx)
                if hd:
                    host, source = hd, "domain"

            # Emit qualified `parent/child` for hosts under a parent foundation
            rows.append({"repo": repo, "host": _qualified(host) if host else "",
                         "host_source": source})
    return rows


def main() -> None:
    argparse.ArgumentParser().parse_args()

    console.rule("[bold cyan]funding/match_repos — joining foundation lists vs top-repos")
    slug_idx, org_idx, domain_idx = build_indexes()
    console.print(f"  [dim]indexed[/dim] {len(slug_idx):,} project slugs across {len(SLUGS)} foundations")
    console.print(f"  [dim]+[/dim] {len(org_idx):,} curated org-prefix rules")
    console.print(f"  [dim]+[/dim] {len(domain_idx):,} apex domains and {len(DOMAIN_SUFFIX_HOST)} suffix rules\n")

    rows = classify(slug_idx, org_idx, domain_idx)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["repo", "host", "host_source"])
        w.writeheader()
        w.writerows(rows)
    tmp.replace(OUT_FILE)

    total = len(rows)
    matched = [r for r in rows if r["host"]]
    by_host: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r in matched:
        by_host[r["host"]] = by_host.get(r["host"], 0) + 1
        by_source[r["host_source"]] = by_source.get(r["host_source"], 0) + 1

    tbl = Table(title="[bold]Hosts matched[/bold]", show_header=True,
                header_style="bold dim", padding=(0, 1))
    tbl.add_column("host"); tbl.add_column("repos", justify="right")
    tbl.add_column("%", justify="right")
    for host in SLUGS:
        c = by_host.get(_qualified(host), 0)
        if c:
            tbl.add_row(_qualified(host), f"{c:,}", f"{100*c/total:.2f}%")
    tbl.add_section()
    tbl.add_row("[bold]matched[/bold]", f"[bold]{len(matched):,}[/bold]",
                f"[bold]{100*len(matched)/total:.2f}%[/bold]")
    tbl.add_row("[dim]unmatched[/dim]", f"[dim]{total-len(matched):,}[/dim]",
                f"[dim]{100*(total-len(matched))/total:.2f}%[/dim]")
    tbl.add_section()
    tbl.add_row("[bold]Total repos[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(tbl)
    console.print(f"\n  [dim]source breakdown:[/dim] " +
                  ", ".join(f"{k}={v:,}" for k, v in sorted(by_source.items())))
    console.print(f"  → wrote [cyan]{OUT_FILE}[/cyan]")


if __name__ == "__main__":
    main()
