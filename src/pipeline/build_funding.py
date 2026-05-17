#!/usr/bin/env python3
"""Build data/funding.csv — funding signals per risk-scope repo.

Reads:
    data/value-data.csv                     — A/B value-class set
    data/funding-data.csv                   — github_sponsors / FUNDING.yml
                                              / funding.json (raw collector output)
    data/foundations/host-by-repo.csv       — FOSS-foundation host per repo

Writes:
    data/funding.csv  with columns:
        repo, repo_id,
        github_sponsors,           (count of accounts on GH Sponsors)
        has_funding_yml,           (bool)
        funding_yml_platforms,     (comma-sep list)
        has_funding_json,          (bool — FLOSS/fund spec)
        foundation_host,           ("apache", "lf", "lf/cncf", ... or "")
        fetched_at                 (latest of funding-data.csv `fetched_at` and
                                    foundations file)

Period: [most recent] — values reflect last fetch by the upstream
funding/foundations collectors.

Usage:
    uv run python -m src.pipeline.build_funding
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
FUNDING_RAW_FILE = DATA_DIR / "funding-data.csv"
FOUNDATIONS_FILE = DATA_DIR / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "funding.csv"

FIELDS = [
    "repo", "repo_id",
    "github_sponsors", "has_funding_yml", "funding_yml_platforms",
    "has_funding_json", "foundation_host",
    "fetched_at",
]


def _load_raw_funding() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not FUNDING_RAW_FILE.exists():
        return out
    with open(FUNDING_RAW_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_foundations() -> dict[str, str]:
    """Return {repo: host} — empty `host` means "not foundation-hosted but checked"."""
    out: dict[str, str] = {}
    if not FOUNDATIONS_FILE.exists():
        return out
    with open(FOUNDATIONS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = (row.get("host") or "").strip()
    return out


def build() -> list[dict]:
    eligible = load_risk_repos()
    raw = _load_raw_funding()
    foundations = _load_foundations()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        r = raw.get(repo, {})
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "github_sponsors": (r.get("github_sponsors") or "").strip(),
            "has_funding_yml": (r.get("has_funding_yml") or "").strip(),
            "funding_yml_platforms": (r.get("funding_yml_platforms") or "").strip(),
            "has_funding_json": (r.get("has_funding_json") or "").strip(),
            "foundation_host": foundations.get(repo, ""),
            "fetched_at": (r.get("fetched_at") or "").strip(),
        })
    return rows


def main() -> None:
    console.print("[bold]Building funding.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Funding coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (
        "github_sponsors", "has_funding_yml", "funding_yml_platforms",
        "has_funding_json", "foundation_host",
    ):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    # Foundation host distribution
    from collections import Counter
    fh = Counter(r["foundation_host"] for r in rows if r["foundation_host"])
    if fh:
        ftable = Table(title="\n[bold]Foundation hosts[/bold]",
                       show_header=True, header_style="bold dim", padding=(0, 1))
        ftable.add_column("Host", style="bold")
        ftable.add_column("Repos", justify="right")
        for host, n in sorted(fh.items(), key=lambda x: -x[1]):
            ftable.add_row(host, f"{n:,}")
        console.print(ftable)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
