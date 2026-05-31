#!/usr/bin/env python3
"""Determine eligibility for GitHub repos based on OSS license + EOL status.

Reads:
- data/sources/github/search/top-repos.csv — license per GitHub repo
- data/sources/{eco}/results.csv + data/sources/{eco}/eol.csv for each of npm/pypi/crates/cpp
  — joined to map github_repo → list of constituent packages and their
  is_eol flags. A repo is is_eol=True iff *every* package mapped to it is
  is_eol=True (handles monorepos / cross-ecosystem polyglot projects).

A repo is treated as alive (is_eol=False) if it has no constituent
packages in any per-eco results.csv (e.g. it's a random GitHub repo not
in our value pipeline — we have no EOL signal for it).

Final eligibility = is_oss AND NOT is_eol. Writes data/eligibility/eligibility.csv.

Usage:
    uv run python -m src.eligibility.classify_eligibility
"""

import csv
import re
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.tables import load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
HOST_FILE = DATA_DIR / "sources" / "foundations" / "host-by-repo.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
GH_USERS_FILE = DATA_DIR / "sources" / "github" / "users.csv"
OSI_FILE = DATA_DIR / "sources" / "osi" / "oss-licenses.csv"
LLM_OWNERSHIP_FILE = DATA_DIR / "sources" / "llms" / "claude-github-ownership.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "eligibility.csv"

ECOSYSTEMS = ("npm", "pypi", "crates", "cpp")

# `data/sources/github/repos.csv` is the sole source of repo-level truth — no
# fallbacks to top-repos.csv. Run `src.sources.github.fetch_repo_owner_data` first
# to populate it. Repos absent from that file are absent from eligibility.
# Schema: input is already AB ∩ gh_valid=True from value-data.csv,
# so `valid_repo` is implicit and dropped. `is_eol` is reserved but
# left empty until the per-eco EOL signals are wired through.
FIELDS = [
    # repo identity + project trademark
    "repo", "repo_id", "repo_url", "repo_tm",
    "value_class",
    # github owner (login + GH-API type) + display + trademark
    "user", "user_id", "user_type", "user_name", "user_url", "user_tm",
    # eligibility signals
    "license", "is_oss", "is_eol", "host",
    "eligibility",
]

# OSS-approved licenses are loaded from `data/sources/osi/oss-licenses.csv`,
# produced by `src.sources.osi.fetch_licenses`. Strict OSS only — the file is
# the union of SPDX `isOsiApproved` and a small hand-curated extras
# list (curl, ftl, libpng-2.0, mit-cmu, psf-2.0, blessing) for
# licenses universally treated as software OSS but never reviewed by
# OSI. **FSF-libre is intentionally excluded** because it covers
# content licenses (CC-BY, CC0, GFDL) that are free for documents but
# aren't software OSS. The file is auto-fetched on first use if
# missing or older than 90 days, so the pipeline is self-bootstrapping.
def _load_oss_approved() -> set[str]:
    # Self-heal: invoke the fetcher (no-op when file is fresh).
    from src.sources.osi.fetch_licenses import ensure as _ensure_osi
    _ensure_osi(verbose=True)

    out: set[str] = set()
    with open(OSI_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid = (r.get("spdx_id") or "").strip().lower()
            if sid:
                out.add(sid)
    return out


OSS_APPROVED: set[str] = _load_oss_approved()


# Tokens that follow an SPDX ID and modify its semantics — strip them
# before membership testing. `gpl-3.0-or-later` → `gpl-3.0`, etc.
_SPDX_SUFFIXES = ("-or-later", "-only", "+")
# Substrings used to combine licenses in an SPDX expression. We split on
# these to test each component individually. For OR/AND/dual-license
# expressions, ANY approved component → the whole expression is OSS
# (under OR you can pick the approved one; under AND every license is
# also separately OSS-compatible since GPL-incompat AND combinations are
# extremely rare and would still cleanly satisfy "is_oss").
_SPDX_SPLITTERS = re.compile(
    r"\s+or\s+|\s+and\s+|/|,|\(|\)|\bwith\s+[a-z0-9.-]+-exception\b",
    re.IGNORECASE,
)


def _normalise_spdx_token(tok: str) -> str:
    """Strip whitespace, parens, suffixes; return lower-cased SPDX-ish ID."""
    tok = tok.strip().strip("()").strip().lower()
    for suf in _SPDX_SUFFIXES:
        if tok.endswith(suf):
            tok = tok[: -len(suf)]
    return tok


# Strings that mean "we genuinely don't know" — distinct from a license
# we know is not OSI. GitHub returns `noassertion` when Licensee can't
# match the LICENSE file with ≥95% confidence; both registries and our
# own pipelines use the empty string to mean "no license declared".
_UNKNOWN_LICENSES = {"", "noassertion", "other", "none"}


def classify_oss(spdx: str) -> bool | None:
    """Return True / False / None for OSS status.

    Membership = OSI-approved ∪ EXTRAS (curl, ftl, libpng-2.0,
    mit-cmu, psf-2.0, blessing — see `src.sources.osi.fetch_licenses.EXTRAS`).
    Strict OSS: content licenses like CC-BY and CC0 are non-OSS here
    even though FSF treats them as free.

    - `True`  — license (or any token in an SPDX expression) is in the set.
    - `False` — license is known but not in the set (e.g. proprietary,
                non-libre data licenses).
    - `None`  — we don't have a license signal at all (empty, `noassertion`,
                `other`, `none`). Surfaces as empty string in the CSV.

    Handles SPDX expressions (`mit or apache-2.0`), suffixes
    (`gpl-3.0-or-later`, `apache-2.0+`), and `with`-exceptions
    (`apache-2.0 with llvm-exception or mit`).
    """
    s = (spdx or "").strip().lower()
    if s in _UNKNOWN_LICENSES:
        return None
    if s in OSS_APPROVED:
        return True
    for raw in _SPDX_SPLITTERS.split(s):
        if _normalise_spdx_token(raw) in OSS_APPROVED:
            return True
    return False


def is_oss_license(spdx: str) -> bool:
    """Back-compat alias — returns True only when `classify_oss` says True."""
    return classify_oss(spdx) is True


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
        results = DATA_DIR / "sources" / eco / "results.csv"
        eol = DATA_DIR / "sources" / eco / "eol.csv"
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

    Produced by `src.sources.foundations.match_repos`. Repos absent from this file
    (or with empty host) get host="".
    """
    if not HOST_FILE.exists():
        return {}
    return {
        r["repo"].strip().lower(): (r.get("host") or "").strip()
        for r in csv.DictReader(open(HOST_FILE, encoding="utf-8"))
        if (r.get("host") or "").strip()
    }


def load_user_meta() -> dict[str, dict]:
    """Load login → {name, blog, html_url} from github/users.csv.

    Populated by `src.sources.github.fetch_repo_owner_data`. Used to populate
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


# Eligibility is scoped to AB-class repos only. C/D have an order of
# magnitude more rows but proportionally less funding-decision value, and
# extending coverage there blows up the GitHub API budget without changing
# the funding shortlist. Tighten/loosen here if the model's scope shifts.
ELIGIBLE_CLASSES: frozenset[str] = frozenset({"A", "B"})


def load_eligible_value_rows() -> list[dict]:
    """Load the AB ∩ gh_valid=True rows from value-data.csv.

    The value pipeline's final step has already verified each row's
    GitHub repo via the API and set `gh_valid` accordingly. We just
    consume that here — no further validity check.
    """
    if not VALUE_FILE.exists():
        raise SystemExit(
            f"missing {VALUE_FILE} — run "
            "`uv run python -m src.value.unify_value_data` first"
        )
    out: list[dict] = []
    with open(VALUE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("class") or "").strip() not in ELIGIBLE_CLASSES:
                continue
            if (r.get("gh_valid") or "").strip().lower() != "true":
                continue
            slug = (r.get("github_repo") or "").strip().lower()
            if slug and "/" in slug:
                out.append(r)
    return out


def load_trademarks() -> tuple[dict[str, str], dict[str, str]]:
    """Load (user_tm_idx, repo_tm_idx) from `claude-github-ownership.csv`.

    The combined ownership table carries both repo-level (`repo_tm`) and
    user-level (`user_tm`) trademark names per row. We index user TMs by
    `user` and repo TMs by `repo`.
    """
    user_tm: dict[str, str] = {}
    repo_tm: dict[str, str] = {}
    if not LLM_OWNERSHIP_FILE.exists():
        return user_tm, repo_tm
    with open(LLM_OWNERSHIP_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip().lower()
            login = (r.get("user") or "").strip().lower()
            r_tm = (r.get("repo_tm") or "").strip()
            u_tm = (r.get("user_tm") or "").strip()
            if slug and r_tm:
                repo_tm[slug] = r_tm
            if login and u_tm:
                user_tm.setdefault(login, u_tm)
    return user_tm, repo_tm


def load_repo_registry_license() -> dict[str, str]:
    """Aggregate per-eco results.csv `license` columns into a per-repo SPDX.

    For each ecosystem (npm/pypi/crates/cpp), reads `data/sources/{eco}/results.csv`
    and joins package → github_repo → license. Multiple packages map to the
    same repo (monorepos, multiple-bind crates) — we pick the most common
    SPDX value across them, breaking ties alphabetically.

    Used as the **primary** license source for eligibility; GitHub API
    license falls back when no per-eco result is found.
    """
    by_repo: dict[str, list[str]] = {}
    for eco in ECOSYSTEMS:
        results = DATA_DIR / "sources" / eco / "results.csv"
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
    """Build eligibility for AB ∩ gh_valid=True repos from value-data.

    Input is already pre-validated by the value pipeline's final step
    (it ran `fetch_and_persist` against the GitHub API and set
    `gh_valid` per row). Eligibility just consumes those rows and joins
    in the licence / EOL / foundation-host / trademark info.

    `is_eol` is True/False/"" — True iff every constituent package across
    every ecosystem is is_eol=True; False if any is alive; "" if the repo
    has no constituent packages with EOL signal at all.

    Eligibility rule:  is_oss is True  AND  is_eol is not True
    """
    eol_idx = load_repo_eol_index()
    host_idx = load_repo_host_index()
    user_meta = load_user_meta()
    registry_lic = load_repo_registry_license()
    user_tm_idx, repo_tm_idx = load_trademarks()

    # GitHub API record per repo (login, ids, license fallback, homepage)
    gh_repos = load_rows_by_repo(GH_REPOS_FILE)

    rows: list[dict] = []
    for v in load_eligible_value_rows():
        slug = v["github_repo"].strip()
        slug_lc = slug.lower()
        gh = gh_repos.get(slug_lc, {})

        # License: registry first, GH-API fallback, lower-cased SPDX
        license_lc = registry_lic.get(slug_lc) or (gh.get("license") or "").strip().lower()
        oss_state = classify_oss(license_lc)         # True / False / None
        is_oss_csv: bool | str = "" if oss_state is None else oss_state

        # is_eol: True/False from the per-eco eol.csv join, or "" when the
        # repo has no constituent packages tracked (e.g. random GH repo).
        eol_state = eol_idx.get(slug_lc)             # True / False / None
        is_eol_csv: bool | str = "" if eol_state is None else eol_state

        eligibility = (oss_state is True) and (eol_state is not True)

        login = (gh.get("owner_login") or "").lower()
        u_meta = user_meta.get(login, {})

        rows.append({
            # repo + project-level trademark
            "repo": slug,
            "repo_id": gh.get("repo_id", ""),
            "repo_url": (gh.get("homepage") or "").strip(),
            "repo_tm": repo_tm_idx.get(slug_lc, ""),
            "value_class": v.get("class", ""),
            # github owner identity + display + trademark
            "user": gh.get("owner_login", ""),
            "user_id": gh.get("owner_id", ""),
            "user_type": gh.get("owner_type", ""),
            "user_name": u_meta.get("name", ""),
            "user_url": u_meta.get("blog", ""),
            "user_tm": user_tm_idx.get(login, ""),
            # eligibility signals
            "license": license_lc,
            "is_oss": is_oss_csv,
            "is_eol": is_eol_csv,
            "host": host_idx.get(slug_lc, ""),
            "eligibility": eligibility,
        })

    return rows


def write_eligibility(new_rows: list[dict]) -> None:
    """Write `eligibility-data.csv` from scratch.

    Source of truth is `data/sources/github/repos.csv`, so each run produces the
    complete table — no upsert/merge with prior eligibility output.

    Sort: by `value_class` ascending (A → B → C → D), then by repo slug.
    Repos with no value_class (not in value-data.csv) go to the bottom.
    """
    def _sort_key(r: dict) -> tuple:
        cls = (r.get("value_class") or "").strip()
        # `~` sorts after every alphanumeric class so empty bubbles to end.
        cls_key = cls if cls else "~"
        return (cls_key, r["repo"].lower())

    sorted_rows = sorted(new_rows, key=_sort_key)
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
    # Input is already AB ∩ gh_valid=True, so no `valid_repo` to count.
    # is_oss is ternary: True / False / "" (unknown).
    oss_rows = [r for r in rows if r["is_oss"] is True]
    non_oss_rows = [r for r in rows if r["is_oss"] is False]
    unknown_oss_rows = [r for r in rows if r["is_oss"] == ""]
    eol_rows = [r for r in rows if r["is_eol"] is True]
    eligible_rows = [r for r in rows if r["eligibility"]]

    # Summary table
    summary = Table(title="[bold]Eligibility Summary[/bold]", show_header=True,
                    header_style="bold dim", padding=(0, 1))
    summary.add_column("", style="dim")
    summary.add_column("Repos", justify="right")
    summary.add_column("%", justify="right")
    summary.add_row("[green]Eligible[/green]", f"[green]{len(eligible_rows):,}[/green]",
                    f"[green]{100 * len(eligible_rows) / total:.1f}%[/green]")
    summary.add_row("[dim]  is_oss=True[/dim]", f"{len(oss_rows):,}",
                    f"{100 * len(oss_rows) / total:.1f}%")
    summary.add_row("[dim]  is_oss=False (known non-OSI)[/dim]",
                    f"{len(non_oss_rows):,}",
                    f"{100 * len(non_oss_rows) / total:.1f}%")
    summary.add_row("[yellow]  is_oss='' (unknown)[/yellow]",
                    f"[yellow]{len(unknown_oss_rows):,}[/yellow]",
                    f"[yellow]{100 * len(unknown_oss_rows) / total:.1f}%[/yellow]")
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
    non_table = Table(title="[bold]Non-OSS Licenses[/bold]", show_header=True,
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
