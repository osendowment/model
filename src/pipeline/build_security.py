#!/usr/bin/env python3
"""Build data/security.csv — security metrics per eligible repo.

Reads:
    data/eligibility-data.csv               — eligible set
    data/openssf/scores.csv                 — OpenSSF Scorecard aggregate score
    data/ossfuzz/projects.csv               — projects enrolled in OSS-Fuzz
    data/osv/cves.csv                       — CVE counts from OSV.dev (optional;
                                              empty column if file is missing)
    data/github/git/semgrep.csv             — semgrep SAST findings (optional;
                                              p/default rulepack used by default;
                                              empty columns if file is missing)
    data/depsdev/repos.csv                  — deps.dev project + Best-Practices
                                              badge (optional; provides a fresh
                                              Scorecard mirror as fall-back for
                                              missing local openssf_score)

Writes:
    data/security.csv  with columns:
        repo, repo_id,
        openssf_score,                      ([2025 EOY], 0..10) — local first,
                                            falls back to deps.dev mirror
        openssf_score_source,               "openssf_local" | "depsdev" | ""
        cve_count_5y,                       ([2021–2025], unique CVEs across
                                            mapped ecosystem packages)
        ossfuzz_enrolled,                   ([most recent], "True"/"False")
        sast_findings_total,                ([2025 EOY] semgrep total findings)
        sast_findings_error,                ([2025 EOY] high-severity only)
        sast_findings_security,             ([2025 EOY] security-category only)
        bestpractices_badge_id,             ([2026], "passing"/"silver"/"gold"/
                                            "in_progress"/"" if not enrolled)
        fetched_at

Period notes inline above. ossfuzz enrollment is binary — the project
appears in the curated `data/ossfuzz/projects.csv` list iff Google's
oss-fuzz repo lists it. Semgrep counts come from the `p/default` rulepack
row in semgrep.csv (one repo can have multiple rows for different rule
packs; we lock onto p/default for the build to keep risk.py deterministic).
deps.dev's Scorecard mirror is typically fresher than our local scan and
covers a few additional repos — we surface it only as a fall-back so
historical comparisons against the local scan stay stable.

Usage:
    uv run python -m src.pipeline.build_security
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.repos import load_eligible_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OPENSSF_SCORES_FILE = DATA_DIR / "openssf" / "scores.csv"
OSSFUZZ_FILE = DATA_DIR / "ossfuzz" / "projects.csv"
OSV_FILE = DATA_DIR / "osv" / "cves.csv"
SEMGREP_FILE = DATA_DIR / "github" / "git" / "semgrep.csv"
DEPSDEV_FILE = DATA_DIR / "depsdev" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "security.csv"

# Semgrep rule pack to surface in the build. The fetcher can write rows for
# multiple rule packs side-by-side (one row per (repo, rulepack)); we lock
# onto p/default here so risk.py picks up the same shape every run.
SEMGREP_RULEPACK = "p/default"

FIELDS = [
    "repo", "repo_id",
    "openssf_score", "openssf_score_source",
    "cve_count_5y", "ossfuzz_enrolled",
    "sast_findings_total", "sast_findings_error", "sast_findings_security",
    "bestpractices_badge_id",
    "fetched_at",
]


def _load_openssf_scores() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not OPENSSF_SCORES_FILE.exists():
        return out
    with open(OPENSSF_SCORES_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = {
                "score": (row.get("score") or "").strip(),
                "checked_at": (row.get("checked_at") or "").strip(),
            }
    return out


def _load_ossfuzz() -> set[str]:
    """Return the set of GitHub repos enrolled in OSS-Fuzz."""
    out: set[str] = set()
    if not OSSFUZZ_FILE.exists():
        return out
    with open(OSSFUZZ_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("github_repo") or "").strip().lower()
            if slug:
                out.add(slug)
    return out


def _load_osv() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not OSV_FILE.exists():
        return out
    with open(OSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_depsdev() -> dict[str, dict[str, str]]:
    """Load deps.dev rows keyed by repo. Empty if file missing."""
    out: dict[str, dict[str, str]] = {}
    if not DEPSDEV_FILE.exists():
        return out
    with open(DEPSDEV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_semgrep(rulepack: str = SEMGREP_RULEPACK) -> dict[str, dict[str, str]]:
    """Load semgrep counts for the chosen rule pack only.

    The CSV is keyed by (repo, rulepack) — same repo can have rows for
    p/default and p/security-audit side-by-side. We filter to the locked
    rule pack so the build output is deterministic across rule-pack
    experiments. Empty for repos that timed out (findings_total="").
    """
    out: dict[str, dict[str, str]] = {}
    if not SEMGREP_FILE.exists():
        return out
    with open(SEMGREP_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            if (row.get("rulepack") or "").strip() != rulepack:
                continue
            # Keep only rows where the scan succeeded (timeouts leave
            # findings_total="" per the fetcher contract).
            if (row.get("findings_total") or "").strip() == "":
                continue
            out[slug] = row
    return out


def build() -> list[dict]:
    eligible = load_eligible_repos()
    scores = _load_openssf_scores()
    fuzz = _load_ossfuzz()
    osv = _load_osv()
    semgrep = _load_semgrep()
    depsdev = _load_depsdev()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        s = scores.get(repo, {})
        o = osv.get(repo, {})
        sg = semgrep.get(repo, {})
        dd = depsdev.get(repo, {})

        # Prefer the local OpenSSF Scorecard scan; fall back to deps.dev's
        # mirror only when our local row is missing or empty. Track which
        # source won so downstream consumers can audit. The fall-back is
        # rare (≈10% of eligible repos), so we keep it strictly opt-in.
        local_score = (s.get("score") or "").strip()
        depsdev_score = (dd.get("depsdev_scorecard_overall") or "").strip()
        if local_score:
            ossf_score, ossf_source = local_score, "openssf_local"
        elif depsdev_score:
            ossf_score, ossf_source = depsdev_score, "depsdev"
        else:
            ossf_score, ossf_source = "", ""

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "openssf_score": ossf_score,
            "openssf_score_source": ossf_source,
            "cve_count_5y": (o.get("cve_count_5y") or "").strip(),
            "ossfuzz_enrolled": "True" if repo in fuzz else "False",
            "sast_findings_total": (sg.get("findings_total") or "").strip(),
            "sast_findings_error": (sg.get("findings_error") or "").strip(),
            "sast_findings_security": (sg.get("findings_security") or "").strip(),
            "bestpractices_badge_id": (
                dd.get("bestpractices_badge_id") or ""
            ).strip(),
            "fetched_at": s.get("checked_at", ""),
        })
    return rows


def main() -> None:
    console.print("[bold]Building security.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Security coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("openssf_score", "cve_count_5y", "ossfuzz_enrolled",
                "sast_findings_total", "sast_findings_error",
                "sast_findings_security", "bestpractices_badge_id"):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")

    # Source attribution for openssf_score
    src_counts = {"openssf_local": 0, "depsdev": 0, "missing": 0}
    for r in rows:
        src = r.get("openssf_score_source") or ""
        if src == "openssf_local":
            src_counts["openssf_local"] += 1
        elif src == "depsdev":
            src_counts["depsdev"] += 1
        else:
            src_counts["missing"] += 1
    console.print(table)
    console.print(
        f"[dim]openssf_score sources: "
        f"local={src_counts['openssf_local']:,} "
        f"depsdev={src_counts['depsdev']:,} "
        f"missing={src_counts['missing']:,}[/dim]"
    )

    enrolled = sum(1 for r in rows if r["ossfuzz_enrolled"] == "True")
    console.print(f"\n[dim]OSS-Fuzz enrolled: {enrolled:,} / {total:,}[/dim]")
    console.print(f"[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
