#!/usr/bin/env python3
"""Aggregate data/eligibility/eligibility.csv — the eligibility rollup.

Joins the per-dimension eligibility builds onto the top-repo scope (valid
class-A, archived included) and derives one `eligible` verdict per repo:

    oss       ← licenses.csv  `oss`     — OSI-approved license
                (unknown license, oss="", counts as NOT OSS here; the
                 ternary detail stays auditable in licenses.csv)
    intent    ← funding.csv   `intent`  — any funding signal (missing → False)
    nonprofit ← funding.csv   `nonprofit` — not company-backed (missing → True)
    active    ← active.csv    `active`  — not EOL, not archived

    eligible = oss AND intent AND nonprofit AND active

(All per-signal detail columns stay in the per-dimension CSVs.)

Usage:
    uv run python -m src.eligibility.build_eligibility
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.repos import load_top_repos
from src.common.tables import load_column_by_id

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
LICENSES_FILE = DATA_DIR / "eligibility" / "licenses.csv"
FUNDING_FILE = DATA_DIR / "eligibility" / "funding.csv"
ACTIVE_FILE = DATA_DIR / "eligibility" / "active.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "eligibility.csv"

FIELDS = ["repo", "repo_id", "oss", "intent", "nonprofit", "active", "eligible"]


def build() -> list[dict]:
    # The per-dimension CSVs are joined by the stable repo_id, like every
    # other id-capable join in the pipeline — a slug join would only be
    # safe while the intermediates are freshly co-generated; across a
    # GitHub rename with a stale intermediate it would misalign the flags.
    eligible_scope = load_top_repos(skip_archived=False)
    oss = load_column_by_id(LICENSES_FILE, "oss")
    intent = load_column_by_id(FUNDING_FILE, "intent")
    nonprofit = load_column_by_id(FUNDING_FILE, "nonprofit")
    active = load_column_by_id(ACTIVE_FILE, "active")

    rows: list[dict] = []
    for entry in eligible_scope:
        repo = entry.repo
        rid = str(entry.repo_id)
        flags = {
            "oss": oss.get(rid, "") == "True",
            "intent": intent.get(rid, "") == "True",
            # Missing funding row means "no corporate backer known" → True,
            # matching build_funding's default.
            "nonprofit": nonprofit.get(rid, "True") != "False",
            "active": active.get(rid, "") == "True",
        }
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            **flags,
            "eligible": all(flags.values()),
        })
    return rows


def main() -> None:
    console.print("[bold]Building eligibility.csv...[/bold]\n")
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    n_eligible = sum(1 for r in rows if r["eligible"])
    table = Table(title="[bold]Eligibility[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Check", style="bold")
    table.add_column("True", justify="right")
    table.add_column("%", justify="right")
    table.add_column("sole blocker", justify="right")
    for flag in ("oss", "intent", "nonprofit", "active"):
        n = sum(1 for r in rows if r[flag])
        # Repos failing ONLY this check — what fixing it alone would unlock.
        sole = sum(1 for r in rows
                   if not r[flag] and all(r[f] for f in
                                          ("oss", "intent", "nonprofit", "active")
                                          if f != flag))
        table.add_row(flag, f"{n:,}", f"{100 * n / total:.1f}%", f"{sole:,}")
    table.add_section()
    table.add_row("[bold green]eligible[/bold green]",
                  f"[bold green]{n_eligible:,}[/bold green]",
                  f"[bold green]{100 * n_eligible / total:.1f}%[/bold green]", "")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} repos × {len(FIELDS)} columns → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
