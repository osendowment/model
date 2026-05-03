#!/usr/bin/env python3
"""Build data/security.csv — security metrics per eligible repo.

Reads:
    data/eligibility-data.csv               — eligible set
    data/openssf/scores.csv                 — OpenSSF Scorecard aggregate score
    data/ossfuzz/projects.csv               — projects enrolled in OSS-Fuzz
    data/osv/cves.csv                       — CVE counts from OSV.dev (optional;
                                              empty column if file is missing)

Writes:
    data/security.csv  with columns:
        repo, repo_id,
        openssf_score,                      ([2025 EOY], 0..10)
        cve_count_5y,                       ([2021–2025], unique CVEs across
                                            mapped ecosystem packages)
        ossfuzz_enrolled,                   ([most recent], "True"/"False")
        fetched_at

Period notes inline above. ossfuzz enrollment is binary — the project
appears in the curated `data/ossfuzz/projects.csv` list iff Google's
oss-fuzz repo lists it.

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
OUTPUT_FILE = DATA_DIR / "security.csv"

FIELDS = [
    "repo", "repo_id",
    "openssf_score", "cve_count_5y", "ossfuzz_enrolled",
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


def build() -> list[dict]:
    eligible = load_eligible_repos()
    scores = _load_openssf_scores()
    fuzz = _load_ossfuzz()
    osv = _load_osv()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        s = scores.get(repo, {})
        o = osv.get(repo, {})
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "openssf_score": s.get("score", ""),
            "cve_count_5y": (o.get("cve_count_5y") or "").strip(),
            "ossfuzz_enrolled": "True" if repo in fuzz else "False",
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
    for col in ("openssf_score", "cve_count_5y", "ossfuzz_enrolled"):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    enrolled = sum(1 for r in rows if r["ossfuzz_enrolled"] == "True")
    console.print(f"\n[dim]OSS-Fuzz enrolled: {enrolled:,} / {total:,}[/dim]")
    console.print(f"[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
