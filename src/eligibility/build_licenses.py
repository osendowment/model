#!/usr/bin/env python3
"""Build data/eligibility/licenses.csv — per-repo license + OSS flag.

For every top repo (valid class-A, archived included) resolve one SPDX
license string and classify it against the OSI-approved set:

- **manual override** (highest priority) — the `license` column of
  data/eligibility/overrides.csv, a curated SPDX assertion for repos whose
  license detection fails upstream: GitHub's Licensee returns `noassertion`
  on bundled / dual / stacked LICENSE files (node, cpython, linux, icu,
  libjpeg-turbo, cairo) or the repo ships no standard LICENSE file at all
  (wmi — MIT declared only in PyPI metadata). Joined on the stable repo_id.
- **registry license** (primary) — the `license` column of each ecosystem's
  results.csv (npm/pypi/crates/cpp, populated by the per-eco fetch_licenses
  scripts), joined package → github_repo. Multiple packages can map to the
  same repo (monorepos); the most common SPDX value wins, ties break
  alphabetically.
- **GitHub license** (fallback) — data/sources/github/repos.csv `license`
  (GitHub's Licensee detection), used when no registry license is found.
- **GitLab license** (fallback) — data/sources/gitlab/repos.csv `license`
  (the GitLab project API's Licensee detection, fetched by
  src.sources.gitlab.fetch_project_data), keyed by the stable gl/ repo_id —
  the GitLab analogue of the GitHub fallback.

A source only claims the slot with a MEANINGFUL license: an unknown sentinel
(`noassertion` / `other` / `none`) never shadows a real detection from a
later source; when every source is unknown, the first sentinel is kept so
the empty verdict stays traceable.
- **`oss`** is ternary: True when the license (or any component of an SPDX
  expression) is in the OSI-approved set — data/sources/osi/oss-licenses.csv,
  the union of SPDX `isOsiApproved` and a small curated extras list (see
  src.sources.osi.fetch_licenses). False when the license is known but not
  approved. Empty when there is no license signal at all (empty /
  noassertion / other / none) — unknown is not the same as known-non-OSS.

Handles SPDX expressions (`mit or apache-2.0`, crates' slash form
`mit/apache-2.0`), suffixes (`gpl-3.0-or-later`, `apache-2.0+`) and
`with`-exceptions (`apache-2.0 with llvm-exception`): ANY approved component
makes the expression OSS.

The rollup (src.eligibility.build_eligibility) treats only `oss=True` as
eligible — unknown counts as not-OSS there, but stays distinguishable here.

Usage:
    uv run python -m src.eligibility.build_licenses
"""

import csv
import re
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import canonical_repo_map, load_top_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OSI_FILE = DATA_DIR / "sources" / "osi" / "oss-licenses.csv"
GH_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
GL_PROJECTS_FILE = DATA_DIR / "sources" / "gitlab" / "repos.csv"
OVERRIDES_FILE = DATA_DIR / "eligibility" / "overrides.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "licenses.csv"

ECOSYSTEMS = ("npm", "pypi", "crates", "cpp")

FIELDS = ["repo", "repo_id", "license", "license_source", "oss"]

# Tokens that follow an SPDX ID and modify its semantics — strip them
# before membership testing. `gpl-3.0-or-later` → `gpl-3.0`, etc.
_SPDX_SUFFIXES = ("-or-later", "-only", "+")
# Substrings used to combine licenses in an SPDX expression. We split on
# these to test each component individually. For OR/AND/dual-license
# expressions, ANY approved component → the whole expression is OSS
# (under OR you can pick the approved one; GPL-incompatible AND
# combinations are rare enough not to matter for this flag).
_SPDX_SPLITTERS = re.compile(
    r"\s+or\s+|\s+and\s+|/|,|\(|\)|\bwith\s+[a-z0-9.-]+-exception\b",
    re.IGNORECASE,
)

# Strings that mean "we genuinely don't know" — distinct from a license
# we know is not OSI-approved. GitHub returns `noassertion` when Licensee
# can't match the LICENSE file with ≥95% confidence; both registries and
# our own pipelines use the empty string to mean "no license declared".
_UNKNOWN_LICENSES = {"", "noassertion", "other", "none"}


def load_oss_approved(path: Path = OSI_FILE) -> set[str]:
    """Lowercased SPDX ids of the OSS-approved set (OSI ∪ curated extras).

    Self-heals: invokes the OSI fetcher first (no-op while the file is
    within its 90-day TTL), so the pipeline is self-bootstrapping.
    """
    from src.sources.osi.fetch_licenses import ensure as ensure_osi
    ensure_osi(verbose=True)

    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sid = (r.get("spdx_id") or "").strip().lower()
            if sid:
                out.add(sid)
    return out


def _normalise_spdx_token(tok: str) -> str:
    """Strip whitespace, parens, suffixes; return lower-cased SPDX-ish ID."""
    tok = tok.strip().strip("()").strip().lower()
    for suf in _SPDX_SUFFIXES:
        if tok.endswith(suf):
            tok = tok[: -len(suf)]
    return tok


def classify_oss(spdx: str, approved: set[str]) -> bool | None:
    """Return True / False / None for OSS status of one license string.

    - `True`  — the license (or any token in an SPDX expression) is approved.
    - `False` — the license is known but not approved.
    - `None`  — no license signal (empty, `noassertion`, `other`, `none`).
    """
    s = (spdx or "").strip().lower()
    if s in _UNKNOWN_LICENSES:
        return None
    if s in approved:
        return True
    for raw in _SPDX_SPLITTERS.split(s):
        if _normalise_spdx_token(raw) in approved:
            return True
    return False


def load_registry_licenses() -> dict[str, str]:
    """Aggregate per-eco results.csv `license` columns into a per-repo SPDX.

    For each ecosystem, joins package → github_repo → license. Slugs are
    canonicalised (rename-resolved) so they match the top-repo scope keys.
    Multiple packages mapping to the same repo: most common SPDX value wins,
    ties break alphabetically. Ecosystems whose results.csv has no `license`
    column yet (fetch_licenses not applied) are skipped.
    """
    canon = canonical_repo_map()
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
                    by_repo.setdefault(canon.get(slug, slug), []).append(lic)
    return {
        slug: sorted(Counter(lics).most_common(),
                     key=lambda kv: (-kv[1], kv[0]))[0][0]
        for slug, lics in by_repo.items()
    }


def load_license_overrides(path: Path = OVERRIDES_FILE) -> dict[str, str]:
    """{repo_id: license} — manual SPDX assertions from overrides.csv.

    The `license` column pins a repo's SPDX string when upstream detection
    fails (GitHub Licensee `noassertion` on bundled/dual/stacked LICENSE
    files, or a repo with no standard LICENSE file). Keyed on the stable
    repo_id so the assertion survives a GitHub rename, and applied above the
    registry/GitHub signals in build().
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            lic = (row.get("license") or "").strip().lower()
            if rid and lic:
                out[rid] = lic
    return out


def load_github_licenses() -> dict[str, str]:
    """{slug: license} from github/repos.csv, keyed by both the looked-up
    `repo` slug and the rename-resolved `full_name` (lowercased SPDX)."""
    out: dict[str, str] = {}
    if not GH_REPOS_FILE.exists():
        return out
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lic = (row.get("license") or "").strip().lower()
            if not lic:
                continue
            for key in ("repo", "full_name"):
                slug = (row.get(key) or "").strip().lower()
                if slug:
                    out[slug] = lic
    return out


def load_gitlab_licenses() -> dict[str, str]:
    """{gl/ repo_id: license} from gitlab/repos.csv (lowercased SPDX).

    The GitLab project API returns a `license.key` (SPDX-ish) that the fetcher
    flattens into the `license` column — the GitLab analogue of the GitHub
    Licensee fallback, keyed by the stable gl/ repo_id rather than a slug.
    """
    out: dict[str, str] = {}
    if not GL_PROJECTS_FILE.exists():
        return out
    with open(GL_PROJECTS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rid = (row.get("repo_id") or "").strip()
            lic = (row.get("license") or "").strip().lower()
            if rid.startswith("gl/") and lic:
                out[rid] = lic
    return out


def build() -> list[dict]:
    # Same scope rationale as the other eligibility builders: archived repos
    # stay in so the rollup can show WHY they are ineligible.
    eligible = load_top_repos()
    approved = load_oss_approved()
    overrides = load_license_overrides()
    registry = load_registry_licenses()
    github = load_github_licenses()
    gitlab = load_gitlab_licenses()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        rid = str(entry.repo_id).strip()
        # Source precedence: override, registry, GitHub, GitLab — but only a
        # MEANINGFUL license claims the slot: a registry "noassertion"/"other"
        # must not shadow a real GitHub/GitLab Licensee detection (glib's cpp
        # registry row says noassertion while the GitLab API reads lgpl-2.1).
        # When every source is junk/empty, keep the first junk value so the
        # "no signal" verdict stays traceable to the source that produced it.
        candidates = (
            ("override", overrides.get(rid, "")),
            ("registry", registry.get(repo, "")),
            ("github", github.get(repo, "")),
            ("gitlab", gitlab.get(rid, "")),
        )
        lic, source = "", ""
        for src, val in candidates:
            if val and val not in _UNKNOWN_LICENSES:
                lic, source = val, src
                break
        if not lic:
            for src, val in candidates:
                if val:
                    lic, source = val, src
                    break
        oss = classify_oss(lic, approved)
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "license": lic,
            "license_source": source,
            "oss": "" if oss is None else oss,
        })
    return rows


def main() -> None:
    console.print("[bold]Building licenses.csv...[/bold]\n")
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    oss_true = [r for r in rows if r["oss"] is True]
    oss_false = [r for r in rows if r["oss"] is False]
    unknown = [r for r in rows if r["oss"] == ""]

    summary = Table(title="[bold]License classification[/bold]", show_header=True,
                    header_style="bold dim", padding=(0, 1))
    summary.add_column("", style="bold")
    summary.add_column("Repos", justify="right")
    summary.add_column("%", justify="right")
    for label, n, style in (("oss=True (OSI-approved)", len(oss_true), "green"),
                            ("oss=False (known non-OSS)", len(oss_false), "red"),
                            ("oss='' (no license signal)", len(unknown), "yellow")):
        summary.add_row(f"[{style}]{label}[/{style}]", f"{n:,}",
                        f"{100 * n / total:.1f}%")
    summary.add_section()
    summary.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(summary)

    by_source = Counter(r["license_source"] for r in rows)
    console.print(f"[dim]license source — override: {by_source.get('override', 0):,} · "
                  f"registry: {by_source.get('registry', 0):,} · "
                  f"github: {by_source.get('github', 0):,} · "
                  f"none: {by_source.get('', 0):,}[/dim]")

    top = Counter(r["license"] for r in oss_true).most_common(8)
    console.print("[dim]top OSS licenses: "
                  + " · ".join(f"{lic} {n:,}" for lic, n in top) + "[/dim]")
    if oss_false:
        non = Counter(r["license"] for r in oss_false).most_common(8)
        console.print("[dim]non-OSS: "
                      + " · ".join(f"{lic} {n:,}" for lic, n in non) + "[/dim]")

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
