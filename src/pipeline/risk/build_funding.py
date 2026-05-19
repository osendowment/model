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
        funding_class,             (A/B/C/D from github_sponsors — see
                                    settings.json risk_classification.funding)
        has_funding_yml,           (bool)
        funding_yml_platforms,     (comma-sep list)
        has_funding_json,          (bool — FLOSS/fund spec)
        funding_5y,                (total income 2021-2025 declared in the
                                    funding.json `funding.history`; empty
                                    when the manifest carries no history)
        foundation_host,           ("apache", "lf", "lf/cncf", ... or "")
        fetched_at                 (latest of funding-data.csv `fetched_at` and
                                    foundations file)

Period: [most recent] — values reflect last fetch by the upstream
funding/foundations collectors.

Usage:
    uv run python -m src.pipeline.risk.build_funding
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.params import FUNDING_THRESHOLDS
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.tables import load_column_by_repo, load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
FUNDING_RAW_FILE = DATA_DIR / "funding-data.csv"
FOUNDATIONS_FILE = DATA_DIR / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "funding.csv"

FIELDS = [
    "repo", "repo_id",
    "github_sponsors", "funding_class",
    "has_funding_yml", "funding_yml_platforms",
    "has_funding_json", "funding_5y", "foundation_host",
    "fetched_at",
]


def funding_class(github_sponsors: str) -> str:
    """A/B/C/D from the github_sponsors count.

    Per settings.json risk_classification.funding (upper bounds):
    A = 0 sponsors, B = 1-10, C = 11-100, D = 101+. Returns "" when the
    sponsor count is missing or unparseable — can't classify.
    """
    raw = (github_sponsors or "").strip()
    if not raw:
        return ""
    try:
        n = int(raw)
    except ValueError:
        return ""
    if n <= FUNDING_THRESHOLDS["A"]:
        return "A"
    if n <= FUNDING_THRESHOLDS["B"]:
        return "B"
    if n <= FUNDING_THRESHOLDS["C"]:
        return "C"
    return "D"


def build() -> list[dict]:
    eligible = load_risk_repos()
    raw = load_rows_by_repo(FUNDING_RAW_FILE)
    # foundation host — empty `host` means "checked, not foundation-hosted".
    foundations = load_column_by_repo(FOUNDATIONS_FILE, "host")

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        r = raw.get(repo, {})
        sponsors = (r.get("github_sponsors") or "").strip()
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "github_sponsors": sponsors,
            "funding_class": funding_class(sponsors),
            "has_funding_yml": (r.get("has_funding_yml") or "").strip(),
            "funding_yml_platforms": (r.get("funding_yml_platforms") or "").strip(),
            "has_funding_json": (r.get("has_funding_json") or "").strip(),
            "funding_5y": (r.get("funding_5y") or "").strip(),
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
        "has_funding_json", "funding_5y", "foundation_host",
    ):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    # Funding-class distribution (A/B/C/D, or — when sponsor count unknown).
    cls = Counter(r["funding_class"] or "—" for r in rows)
    ctable = Table(title="\n[bold]Funding class[/bold]",
                   show_header=True, header_style="bold dim", padding=(0, 1))
    ctable.add_column("Class", style="bold")
    ctable.add_column("Repos", justify="right")
    for c in ("A", "B", "C", "D", "—"):
        if cls.get(c):
            ctable.add_row(c, f"{cls[c]:,}")
    console.print(ctable)

    # Foundation host distribution
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
