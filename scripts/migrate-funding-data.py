"""One-time: split data/risk/funding-data.csv into source-folder CSVs.

    data/risk/funding-data.csv
      → data/sources/github/sponsors.csv     (repo, repo_id, github_sponsors,
                                               sponsors_status, fetched_at)
      → data/sources/github/funding-yml.csv  (repo, repo_id, has_funding_yml,
                                               funding_yml_platforms,
                                               funding_yml_github, fetched_at)

Preserves fetched values + timestamps (no GitHub re-query). `funding_yml_github`
is left empty (repopulated on next real fetch). has_funding_json / funding_5y /
funding_sources / funding_class are dropped (has_funding_json is derived from
the FLOSS Fund export at build time). Then removes funding-data.csv.

Usage:
    uv run python scripts/migrate-funding-data.py
"""
from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()
DATA = Path("data")
SRC = DATA / "risk" / "funding-data.csv"
SPONSORS = DATA / "sources" / "github" / "sponsors.csv"
YML = DATA / "sources" / "github" / "funding-yml.csv"

SPONSORS_FIELDS = ["repo", "repo_id", "github_sponsors", "sponsors_status", "fetched_at"]
YML_FIELDS = ["repo", "repo_id", "has_funding_yml", "funding_yml_platforms",
              "funding_yml_github", "fetched_at"]


def split_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    sponsors, yml = [], []
    for r in rows:
        sponsors.append({
            "repo": r.get("repo", ""), "repo_id": r.get("repo_id", ""),
            "github_sponsors": r.get("github_sponsors", ""),
            "sponsors_status": "ok", "fetched_at": r.get("fetched_at", "")})
        yml.append({
            "repo": r.get("repo", ""), "repo_id": r.get("repo_id", ""),
            "has_funding_yml": r.get("has_funding_yml", ""),
            "funding_yml_platforms": r.get("funding_yml_platforms", ""),
            "funding_yml_github": "", "fetched_at": r.get("fetched_at", "")})
    return sponsors, yml


def _write(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["repo"]))


def main() -> None:
    if not SRC.exists():
        console.print(f"[yellow]{SRC} not found — nothing to migrate.[/yellow]")
        return
    with open(SRC, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    sponsors, yml = split_rows(rows)
    _write(SPONSORS, SPONSORS_FIELDS, sponsors)
    _write(YML, YML_FIELDS, yml)
    console.print(f"[green]Wrote[/green] {len(sponsors)} → {SPONSORS}")
    console.print(f"[green]Wrote[/green] {len(yml)} → {YML}")
    subprocess.run(["git", "rm", "-q", str(SRC)], check=False)
    console.print(f"[green]Removed[/green] {SRC}")


if __name__ == "__main__":
    main()
