#!/usr/bin/env python3
"""Validation check — A/B-class projects whose repo/git URL is invalid.

The risk pipeline can only assess a project if its `value.csv` row resolves to
a **reachable** source — a live GitHub repo or a cloneable git URL. This check
lists every A/B (risk-scope) project whose `git_valid` column is not `True`,
i.e. either:

  - **no target** — neither `repo` nor `git_url` resolved (the project
    lives on a non-git forge we never mapped: Mercurial/SVN/bzr, etc.), or
  - **dead target** — a `repo`/`git_url` that failed validation (404 /
    `git ls-remote` failed).

These are the projects that **require a manual override** in
`data/value/overrides.csv` (a corrected `repo`, a canonical `git_url`,
or a `valid` pin). The check exits non-zero when any are found, so it can gate
CI / a pipeline run.

Usage:
    uv run python -m src.value.check_ab_git_urls
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
RISK_CLASSES = ("A", "B")


def find_invalid_ab(value_file: Path = VALUE_FILE) -> list[dict]:
    """Return A/B rows whose `git_valid` != 'True', annotated with a `_reason`."""
    out: list[dict] = []
    if not value_file.exists():
        return out
    with open(value_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("class") or "").strip() not in RISK_CLASSES:
                continue
            if (row.get("git_valid") or "").strip() == "True":
                continue
            gh = (row.get("repo") or "").strip()
            gu = (row.get("git_url") or "").strip()
            if not gh and not gu:
                reason = "no repo/url resolved"
            elif (row.get("git_valid") or "").strip() == "False":
                reason = "target unreachable"
            else:
                reason = "not yet validated"
            row["_reason"] = reason
            out.append(row)
    return out


def main() -> int:
    console.rule("[bold]A/B git-URL validation check[/bold]")
    console.print(f"[dim]source: {VALUE_FILE}[/dim]\n")
    rows = find_invalid_ab()

    if not rows:
        console.print("[bold green]✓ All A/B projects have a valid repo/git URL.[/bold green]")
        return 0

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1),
                  title=f"[bold]{len(rows)} A/B project(s) needing a manual override[/bold]")
    table.add_column("package")
    table.add_column("eco")
    table.add_column("git_valid")
    table.add_column("target (repo / git_url)")
    table.add_column("reason")
    for r in sorted(rows, key=lambda r: ((r.get("top_eco") or ""), (r.get("top_eco_pkg") or ""))):
        target = (r.get("repo") or "").strip() or (r.get("git_url") or "").strip() or "—"
        table.add_row(
            (r.get("top_eco_pkg") or "").strip() or "?",
            (r.get("top_eco") or "").strip(),
            (r.get("git_valid") or "").strip() or "(empty)",
            target,
            r["_reason"],
        )
    console.print(table)
    console.print(
        f"\n[yellow]Add a corrected repo / canonical git_url (or a valid "
        f"pin) for each in data/value/overrides.csv, then re-run "
        f"unify → verify_git_urls → build_validation to refresh git_valid.[/yellow]"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
