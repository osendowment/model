#!/usr/bin/env python3
"""Determine eligibility for GitHub repos based on OSS license + EOL status.

Reads:
- data/github/search/top-repos.csv — license per GitHub repo
- data/{eco}/results.csv + data/{eco}/eol.csv for each of npm/pypi/crates/cpp
  — joined to map github_repo → list of constituent packages and their
  is_eol flags. A repo is is_eol=True iff *every* package mapped to it is
  is_eol=True (handles monorepos / cross-ecosystem polyglot projects).

A repo is treated as alive (is_eol=False) if it has no constituent
packages in any per-eco results.csv (e.g. it's a random GitHub repo not
in our value pipeline — we have no EOL signal for it).

Final eligibility = is_oss AND NOT is_eol. Writes data/eligibility-data.csv.

Usage:
    python -m src.eligibility
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPOS_FILE = DATA_DIR / "github" / "search" / "top-repos.csv"
HOST_FILE = DATA_DIR / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "eligibility-data.csv"

ECOSYSTEMS = ("npm", "pypi", "crates", "cpp")

FIELDS = ["repo", "repo_id", "user", "user_id", "user_type", "license", "is_oss", "is_eol", "host", "tm_owner", "tm_owner_type", "eligibility"]

# OSI-approved licenses — GitHub API license keys
# https://opensource.org/licenses
OSI_APPROVED: set[str] = {
    # Permissive
    "mit",
    "mit-0",
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "bsd-3-clause-clear",
    "isc",
    "0bsd",
    "zlib",
    "unlicense",
    "bsl-1.0",          # Boost Software License
    "ncsa",
    "postgresql",
    "upl-1.0",          # Universal Permissive License
    "ms-pl",            # Microsoft Public License
    "mulanpsl-2.0",     # MulanPSL
    "eupl-1.2",         # European Union Public License
    "afl-3.0",          # Academic Free License
    "artistic-2.0",
    "ofl-1.1",          # SIL Open Font License
    # Copyleft
    "gpl-2.0",
    "gpl-3.0",
    "agpl-3.0",
    "lgpl-2.1",
    "lgpl-3.0",
    "mpl-2.0",
    "epl-1.0",
    "epl-2.0",
    # Public domain dedications
    "cc0-1.0",
    # Weak copyleft / others approved by OSI
    "wtfpl",
}

# Not OSI-approved:
#   "other"           — unknown, can't determine
#   ""                — no license detected
#   "cc-by-4.0"       — Creative Commons (not OSI)
#   "cc-by-sa-4.0"    — Creative Commons (not OSI)
#   "gfdl-1.3"        — GNU Free Documentation License (not OSI)


def load_repo_eol_index() -> dict[str, bool]:
    """Compute per-repo is_eol from per-ecosystem eol.csv + results.csv.

    For each ecosystem, joins {package: github_repo} from results.csv with
    {package: is_eol} from eol.csv. Aggregates across ecosystems: a
    github_repo is EOL iff every constituent package (across all 4
    ecosystems) is is_eol=True. Repos with no constituent packages are
    absent from the index.
    """
    by_repo: dict[str, list[bool]] = {}
    for eco in ECOSYSTEMS:
        results = DATA_DIR / eco / "results.csv"
        eol = DATA_DIR / eco / "eol.csv"
        if not results.exists() or not eol.exists():
            continue
        repo_by_pkg: dict[str, str] = {}
        with open(results, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pkg = r.get("package", "")
                slug = (r.get("github_repo") or "").strip().lower()
                if pkg and slug:
                    repo_by_pkg[pkg] = slug
        with open(eol, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                slug = repo_by_pkg.get(r.get("package", ""))
                if not slug:
                    continue
                by_repo.setdefault(slug, []).append(r.get("is_eol") == "True")
    return {repo: all(flags) for repo, flags in by_repo.items()}


def load_repo_host_index() -> dict[str, str]:
    """Load repo → foundation slug from foundations/host-by-repo.csv.

    Produced by `src.foundations.match_repos`. Repos absent from this file
    (or with empty host) get host="".
    """
    if not HOST_FILE.exists():
        return {}
    return {
        r["repo"].strip().lower(): (r.get("host") or "").strip()
        for r in csv.DictReader(open(HOST_FILE, encoding="utf-8"))
        if (r.get("host") or "").strip()
    }


def build_eligibility() -> list[dict]:
    """Read top-repos.csv + value-data.csv + host-by-repo.csv and classify each repo.

    `host` is the slug of the FOSS foundation hosting the project (apache,
    cncf, eclipse, lf, numfocus, sfc) or empty if none — populated by
    `src.foundations.match_repos`.

    # TODO: check if project trademark is owned by a company or corporate nonprofit
    #       — projects with corporate-held trademarks may not be truly community-owned
    """
    eol_idx = load_repo_eol_index()
    host_idx = load_repo_host_index()
    rows = []
    with open(REPOS_FILE, encoding="utf-8") as f:
        for repo in csv.DictReader(f):
            license_key = repo.get("license", "")
            is_oss = license_key in OSI_APPROVED
            key = repo["repo"].lower()
            is_eol = eol_idx.get(key, False)
            host = host_idx.get(key, "")

            rows.append({
                "repo": repo["repo"],
                "repo_id": repo["repo_id"],
                "user": repo.get("user_name", ""),
                "user_id": repo.get("user_id", ""),
                "user_type": repo.get("user_type", ""),
                "license": license_key,
                "is_oss": is_oss,
                "is_eol": is_eol,
                "host": host,
                "tm_owner": "",
                "tm_owner_type": "",
                "eligibility": is_oss and not is_eol,
            })

    return rows


def upsert(new_rows: list[dict]) -> None:
    """Upsert rows into eligibility-data.csv, keyed by repo_id."""
    existing: dict[str, dict] = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Backfill columns that predate later runs
                row.setdefault("is_eol", "False")
                row.setdefault("host", "")
                existing[row["repo_id"]] = row

    for row in new_rows:
        existing[str(row["repo_id"])] = row

    sorted_rows = sorted(existing.values(), key=lambda r: r["repo"])

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted_rows)


def main():
    console.print("[bold]Building eligibility table...[/bold]\n")
    rows = build_eligibility()
    upsert(rows)

    total = len(rows)
    oss_rows = [r for r in rows if r["is_oss"]]
    non_oss_rows = [r for r in rows if not r["is_oss"]]
    eol_rows = [r for r in rows if r["is_eol"]]
    eligible_rows = [r for r in rows if r["eligibility"]]

    # Summary table
    summary = Table(title="[bold]Eligibility Summary[/bold]", show_header=True,
                    header_style="bold dim", padding=(0, 1))
    summary.add_column("", style="dim")
    summary.add_column("Repos", justify="right")
    summary.add_column("%", justify="right")
    summary.add_row("[green]Eligible[/green]", f"[green]{len(eligible_rows):,}[/green]",
                    f"[green]{100 * len(eligible_rows) / total:.1f}%[/green]")
    summary.add_row("[dim]  is_oss[/dim]", f"{len(oss_rows):,}",
                    f"{100 * len(oss_rows) / total:.1f}%")
    summary.add_row("[red]  is_eol[/red]", f"[red]{len(eol_rows):,}[/red]",
                    f"[red]{100 * len(eol_rows) / total:.1f}%[/red]")
    summary.add_row("[red]Not eligible[/red]", f"[red]{total - len(eligible_rows):,}[/red]",
                    f"[red]{100 * (total - len(eligible_rows)) / total:.1f}%[/red]")
    summary.add_section()
    summary.add_row("[bold]Total repos[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(summary)
    console.print()

    # OSS license breakdown
    oss_licenses = Counter(r["license"] for r in oss_rows)
    lic_table = Table(title="[bold]OSS Licenses[/bold]", show_header=True,
                      header_style="bold dim", padding=(0, 1))
    lic_table.add_column("License", min_width=16)
    lic_table.add_column("Repos", justify="right")
    lic_table.add_column("%", justify="right")

    other_count = 0
    other_licenses = 0
    for lic, count in oss_licenses.most_common():
        pct = 100 * count / total
        if pct < 0.25:
            other_count += count
            other_licenses += 1
        else:
            lic_table.add_row(lic or "[dim](empty)[/dim]", f"{count:,}", f"{pct:.1f}%")

    if other_count:
        lic_table.add_row(f"[dim]{other_licenses} others[/dim]",
                          f"[dim]{other_count:,}[/dim]",
                          f"[dim]{100 * other_count / total:.1f}%[/dim]")

    lic_table.add_section()
    lic_table.add_row("[bold]Total OSS[/bold]", f"[bold]{len(oss_rows):,}[/bold]",
                      f"[bold]{100 * len(oss_rows) / total:.1f}%[/bold]")
    console.print(lic_table)
    console.print()

    # Non-OSS breakdown
    non_oss_licenses = Counter(r["license"] for r in non_oss_rows)
    non_table = Table(title="[bold]Non-OSS / Unknown[/bold]", show_header=True,
                      header_style="bold dim", padding=(0, 1))
    non_table.add_column("License", min_width=16)
    non_table.add_column("Repos", justify="right")
    non_table.add_column("%", justify="right")

    other_count = 0
    other_licenses = 0
    for lic, count in non_oss_licenses.most_common():
        pct = 100 * count / total
        if pct < 0.25:
            other_count += count
            other_licenses += 1
        else:
            non_table.add_row(lic or "[dim](none)[/dim]", f"{count:,}", f"{pct:.1f}%")

    if other_count:
        non_table.add_row(f"[dim]{other_licenses} others[/dim]",
                          f"[dim]{other_count:,}[/dim]",
                          f"[dim]{100 * other_count / total:.1f}%[/dim]")

    non_table.add_section()
    non_table.add_row("[bold]Total non-OSS[/bold]", f"[bold]{len(non_oss_rows):,}[/bold]",
                      f"[bold]{100 * len(non_oss_rows) / total:.1f}%[/bold]")
    console.print(non_table)
    console.print()

    console.print(f"[dim]Written {total:,} repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
