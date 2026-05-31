#!/usr/bin/env python3
"""Build data/risk/funding.csv — funding signals per risk-scope repo.

Reads (all under data/sources/):
    github/sponsors.csv        — github_sponsors  (src.github.fetch_sponsors)
    github/funding-yml.csv     — has_funding_yml / funding_yml_platforms
                                 (src.github.fetch_funding_yml)
    floss-fund/funding-json.csv — FLOSS Fund directory export; has_funding_json
                                 is derived by matching repo URLs
                                 (src.floss_fund.funding_json)
    foundations/host-by-repo.csv — FOSS-foundation host per repo

Writes data/risk/funding.csv:
    repo, repo_id, github_sponsors, has_funding_yml, funding_yml_platforms,
    has_funding_json, foundation_host, fetched_at

`has_funding_json` is True iff the repo is registered in the FLOSS Fund
directory (no per-repo fetch). `fetched_at` is the most recent of the
contributing source rows' timestamps. No funding class is computed.

Usage:
    uv run python -m src.pipeline.risk.build_funding
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.floss_fund.directory import load_directory_repos
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.tables import load_column_by_repo, load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
SPONSORS_FILE = DATA_DIR / "sources" / "github" / "sponsors.csv"
FUNDING_YML_FILE = DATA_DIR / "sources" / "github" / "funding-yml.csv"
FLOSS_FUND_FILE = DATA_DIR / "sources" / "floss-fund" / "funding-json.csv"
FOUNDATIONS_FILE = DATA_DIR / "sources" / "foundations" / "host-by-repo.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "funding.csv"

FIELDS = ["repo", "repo_id", "github_sponsors", "has_funding_yml",
          "funding_yml_platforms", "has_funding_json", "foundation_host",
          "fetched_at"]


def _latest(*timestamps: str) -> str:
    """Most recent ISO timestamp among the args (lexical order works for ISO)."""
    return max((t for t in timestamps if t), default="")


def assemble_row(repo: str, repo_id: str, sponsors: dict, yml: dict,
                 foundation_host: str, directory_repos: set) -> dict:
    """Join one repo's signals into a funding.csv row."""
    return {
        "repo": repo,
        "repo_id": repo_id,
        "github_sponsors": (sponsors.get("github_sponsors") or "").strip(),
        "has_funding_yml": (yml.get("has_funding_yml") or "").strip(),
        "funding_yml_platforms": (yml.get("funding_yml_platforms") or "").strip(),
        "has_funding_json": "True" if repo.lower() in directory_repos else "False",
        "foundation_host": foundation_host,
        "fetched_at": _latest((sponsors.get("fetched_at") or "").strip(),
                              (yml.get("fetched_at") or "").strip()),
    }


def build() -> list[dict]:
    eligible = load_risk_repos()
    sponsors = load_rows_by_repo(SPONSORS_FILE)
    yml = load_rows_by_repo(FUNDING_YML_FILE)
    foundations = load_column_by_repo(FOUNDATIONS_FILE, "host")
    directory_repos = load_directory_repos(FLOSS_FUND_FILE)

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        rows.append(assemble_row(
            repo=repo, repo_id=entry.repo_id,
            sponsors=sponsors.get(repo, {}), yml=yml.get(repo, {}),
            foundation_host=foundations.get(repo, ""),
            directory_repos=directory_repos))
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
    table = Table(title="[bold]Funding coverage[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("github_sponsors", "has_funding_yml", "funding_yml_platforms",
                "has_funding_json", "foundation_host"):
        n = sum(1 for r in rows if r[col] and r[col] != "False")
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    fh = Counter(r["foundation_host"] for r in rows if r["foundation_host"])
    if fh:
        ftable = Table(title="\n[bold]Foundation hosts[/bold]", show_header=True,
                       header_style="bold dim", padding=(0, 1))
        ftable.add_column("Host", style="bold")
        ftable.add_column("Repos", justify="right")
        for host, n in sorted(fh.items(), key=lambda x: -x[1]):
            ftable.add_row(host, f"{n:,}")
        console.print(ftable)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
