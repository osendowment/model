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

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
HOST_FILE = DATA_DIR / "foundations" / "host-by-repo.csv"
GH_REPOS_FILE = DATA_DIR / "github" / "repos.csv"
GH_USERS_FILE = DATA_DIR / "github" / "users.csv"
OUTPUT_FILE = DATA_DIR / "eligibility-data.csv"

ECOSYSTEMS = ("npm", "pypi", "crates", "cpp")

# `data/github/repos.csv` is the sole source of repo-level truth — no
# fallbacks to top-repos.csv. Run `src.github.fetch_repo_owner_data` first
# to populate it. Repos absent from that file are absent from eligibility.
FIELDS = ["repo", "repo_id", "valid_repo", "user", "user_id", "user_type",
          "license", "is_oss", "is_eol", "host",
          "repo_url", "repo_owner", "repo_owner_url", "repo_owner_type",
          "tm_owner", "tm_owner_type", "eligibility"]

# OSI-approved licenses — keys are lower-cased SPDX IDs (e.g. `mit`,
# `apache-2.0`). Values stored in `eligibility-data.csv` keep the canonical
# SPDX casing (`MIT`, `Apache-2.0`); membership checks lower-case before
# lookup. https://opensource.org/licenses
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
    "blueoak-1.0.0",    # Blue Oak Model License (OSI-approved Oct 2024)
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


def load_repo_meta() -> dict[str, dict]:
    """Load repo → {homepage} from github/repos.csv.

    Populated by `src.github.fetch_repo_owner_data` for AB-class repos
    (90-day TTL). Used to populate `repo_url`. Returns empty dict if file
    doesn't exist yet — `repo_url` falls back to top-repos.csv's homepage.
    """
    if not GH_REPOS_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip().lower()
            if slug:
                out[slug] = r
    return out


def load_user_meta() -> dict[str, dict]:
    """Load login → {name, blog, html_url} from github/users.csv.

    Populated by `src.github.fetch_repo_owner_data`. Used to populate
    `repo_owner` (display name) and `repo_owner_url` (blog).
    """
    if not GH_USERS_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    with open(GH_USERS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            login = (r.get("login") or "").strip().lower()
            if login:
                out[login] = r
    return out


def load_repo_registry_license() -> dict[str, str]:
    """Aggregate per-eco results.csv `license` columns into a per-repo SPDX.

    For each ecosystem (npm/pypi/crates/cpp), reads `data/{eco}/results.csv`
    and joins package → github_repo → license. Multiple packages map to the
    same repo (monorepos, multiple-bind crates) — we pick the most common
    SPDX value across them, breaking ties alphabetically.

    Used as the **primary** license source for eligibility; GitHub API
    license falls back when no per-eco result is found.
    """
    from collections import Counter
    by_repo: dict[str, list[str]] = {}
    for eco in ECOSYSTEMS:
        results = DATA_DIR / eco / "results.csv"
        if not results.exists():
            continue
        with open(results, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "license" not in (reader.fieldnames or []):
                continue  # ecosystem hasn't been enriched yet — skip
            for r in reader:
                slug = (r.get("github_repo") or "").strip().lower()
                lic = (r.get("license") or "").strip().lower()
                if slug and lic:
                    by_repo.setdefault(slug, []).append(lic)
    # Per repo: pick the most common SPDX, break ties alphabetically.
    return {
        slug: sorted(Counter(lics).most_common(),
                     key=lambda kv: (-kv[1], kv[0]))[0][0]
        for slug, lics in by_repo.items()
    }


def build_eligibility() -> list[dict]:
    """Build eligibility rows directly from `data/github/repos.csv`.

    No fallbacks: if a repo isn't in `repos.csv`, it's not in eligibility —
    we deliberately reject any repo we haven't verified against the GitHub
    API. For repos that GitHub returned 404 on, the fetcher writes a sparse
    row with `valid=False`; those become rows here with `valid_repo=False`,
    `is_oss=False`, `eligibility=False`.

    # TODO: check if project trademark is owned by a company or corporate nonprofit
    #       — projects with corporate-held trademarks may not be truly community-owned
    """
    if not GH_REPOS_FILE.exists():
        raise SystemExit(
            f"missing {GH_REPOS_FILE} — run "
            "`uv run python -m src.github.fetch_repo_owner_data` first"
        )

    eol_idx = load_repo_eol_index()
    host_idx = load_repo_host_index()
    user_meta = load_user_meta()

    rows: list[dict] = []
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip()
            if not slug:
                continue
            valid = (r.get("valid") or "").lower() == "true"

            if not valid:
                # 404 / disappeared — record the slug + valid=False, leave
                # everything else empty. Not eligible by definition.
                rows.append({
                    "repo": slug,
                    "repo_id": "",
                    "valid_repo": False,
                    "user": "", "user_id": "", "user_type": "",
                    "license": "",
                    "is_oss": False,
                    "is_eol": eol_idx.get(slug.lower(), False),
                    "host": host_idx.get(slug.lower(), ""),
                    "repo_url": "",
                    "repo_owner": "", "repo_owner_url": "", "repo_owner_type": "",
                    "tm_owner": "", "tm_owner_type": "",
                    "eligibility": False,
                })
                continue

            # repos.csv stores GitHub's `spdx_id` (canonical case: "MIT",
            # "Apache-2.0"). Preserve casing in the output; lower-case only
            # for the OSI membership check.
            license_spdx = (r.get("license") or "").strip()
            is_oss = license_spdx.lower() in OSI_APPROVED
            is_eol = eol_idx.get(slug.lower(), False)
            host = host_idx.get(slug.lower(), "")

            login = (r.get("owner_login") or "").lower()
            u_meta = user_meta.get(login, {})

            rows.append({
                "repo": slug,
                "repo_id": r.get("repo_id", ""),
                "valid_repo": True,
                "user": r.get("owner_login", ""),
                "user_id": r.get("owner_id", ""),
                "user_type": r.get("owner_type", ""),
                "license": license_spdx,
                "is_oss": is_oss,
                "is_eol": is_eol,
                "host": host,
                "repo_url": (r.get("homepage") or "").strip(),
                "repo_owner": u_meta.get("name", ""),
                "repo_owner_url": u_meta.get("blog", ""),
                "repo_owner_type": "",
                "tm_owner": "",
                "tm_owner_type": "",
                "eligibility": is_oss and not is_eol,
            })

    return rows


def write_eligibility(new_rows: list[dict]) -> None:
    """Write `eligibility-data.csv` from scratch.

    Source of truth is `data/github/repos.csv`, so each run produces the
    complete table — no upsert/merge with prior eligibility output.
    """
    sorted_rows = sorted(new_rows, key=lambda r: r["repo"].lower())
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_FILE.with_suffix(OUTPUT_FILE.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_rows)
    tmp.replace(OUTPUT_FILE)


def main():
    console.print("[bold]Building eligibility table...[/bold]\n")
    rows = build_eligibility()
    write_eligibility(rows)

    total = len(rows)
    invalid_rows = [r for r in rows if not r["valid_repo"]]
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
    summary.add_row("[red]  invalid_repo (404)[/red]",
                    f"[red]{len(invalid_rows):,}[/red]",
                    f"[red]{100 * len(invalid_rows) / total:.1f}%[/red]")
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
