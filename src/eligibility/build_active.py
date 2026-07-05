#!/usr/bin/env python3
"""Build data/eligibility/active.csv — is each top repo still alive?

active = NOT eol AND NOT archived (with the live-upstream-mirror exemption).

- **`eol`** — the manual end-of-life call from data/eligibility/overrides.csv
  (`eol` column, True = project is end-of-life; empty/absent = alive). The
  per-ecosystem registry EOL signals (data/sources/<eco>/eol.csv, produced by
  src.sources.<eco>.check_eol) are advisory inputs to that manual call — they
  flag *packages* (deprecations, yanks), while `eol` is a per-repo verdict.
- **`archived`** — the GitHub `archived` flag from
  data/sources/github/repos.csv (src.sources.github.fetch_repo_owner_data).
- **`mirror`** — live-upstream GitHub mirrors (e.g. bminor/glibc mirrors
  sourceware.org) carry the archived flag on the mirror while the real
  upstream is alive; they are exempt from the archived drop (same set
  load_top_repos uses — see src.common.repos.load_live_upstream_mirrors).

Usage:
    uv run python -m src.eligibility.build_active
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_live_upstream_mirrors, load_top_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OVERRIDES_FILE = DATA_DIR / "eligibility" / "overrides.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "active.csv"

FIELDS = ["repo", "repo_id", "eol", "archived", "mirror", "active"]


def load_eol_overrides(path: Path = OVERRIDES_FILE) -> dict[str, bool]:
    """{repo_lower: eol} from the stage overrides (`eol` column).

    Only rows with a non-empty `eol` cell are returned — True marks a
    project declared end-of-life, an explicit False pins a repo alive.
    """
    out: dict[str, bool] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            eol = (row.get("eol") or "").strip()
            if repo and eol:
                out[repo] = eol == "True"
    return out


def build() -> list[dict]:
    # Eligibility scope includes archived repos (skip_archived=False) —
    # this builder is exactly where they surface as active=False instead
    # of being silently dropped from the stage output.
    eligible = load_top_repos(skip_archived=False)
    eol_overrides = load_eol_overrides()
    mirrors = load_live_upstream_mirrors()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        eol = eol_overrides.get(repo.lower(), False)
        mirror = repo in mirrors
        active = (not eol) and (not entry.archived or mirror)
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "eol": eol,
            "archived": entry.archived,
            "mirror": mirror,
            "active": active,
        })
    return rows


def main() -> None:
    console.print("[bold]Building active.csv...[/bold]\n")
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Activity[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("", style="bold")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")
    for label, pred, style in (
            ("active", lambda r: r["active"], "green"),
            ("eol (override)", lambda r: r["eol"], "red"),
            ("archived", lambda r: r["archived"], "red"),
            ("archived but mirror-exempt", lambda r: r["archived"] and r["mirror"], "yellow")):
        n = sum(1 for r in rows if pred(r))
        table.add_row(f"[{style}]{label}[/{style}]", f"{n:,}", f"{100 * n / total:.1f}%")
    table.add_section()
    table.add_row("[bold]Total[/bold]", f"[bold]{total:,}[/bold]", "")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
