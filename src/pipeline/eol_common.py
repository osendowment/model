"""Shared schema, I/O, and display for per-ecosystem EOL checks.

Each ecosystem has its own `check_eol.py` that produces `data/{eco}/eol.csv`
keyed by package name. The schema is uniform so `src.pipeline.value` can
merge a single `is_eol` column into `value-data.csv`.

Schema:
    package          — package name (the same key as in {eco}/results.csv)
    is_eol           — True / False
    eol_method       — registry-level signal id, e.g. `npm_deprecated`,
                       `pypi_inactive`, `crates_yanked`, or `unsupported`
    eol_reason       — human-readable reason / evidence string
                       (deprecation message, classifier name, etc.)
    source           — `registry` (live API), `db-dump` (local mirror),
                       `not_found` (404), `error` (network), `unsupported`
    eol_checked_at   — UTC ISO 8601 timestamp of the check (per row)
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

EOL_FIELDS = ["package", "is_eol", "eol_method", "eol_reason", "source", "eol_checked_at"]


def now_iso() -> str:
    """UTC ISO 8601 timestamp to second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_eol(path: Path, rows: list[dict]) -> None:
    """Write rows to `path` using the canonical EOL schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EOL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def display_summary(console: Console, ecosystem: str, rows: list[dict]) -> None:
    """Print a rich summary: counts by method/source + sample of EOL rows."""
    by_method: dict[str, int] = {}
    by_source: dict[str, int] = {}
    eol_count = 0
    for r in rows:
        by_method[r["eol_method"]] = by_method.get(r["eol_method"], 0) + 1
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["is_eol"] in (True, "True"):
            eol_count += 1

    t = Table(title=f"[bold]{ecosystem} EOL summary[/bold]", header_style="bold dim")
    t.add_column("metric")
    t.add_column("count", justify="right")
    t.add_row("total packages", f"{len(rows):,}")
    t.add_row("[red]is_eol=True[/red]", f"[red]{eol_count:,}[/red]")
    for k, v in sorted(by_source.items()):
        t.add_row(f"[dim]source={k}[/dim]", f"[dim]{v:,}[/dim]")
    console.print(t)

    eol_rows = [r for r in rows if r["is_eol"] in (True, "True")]
    if eol_rows:
        s = Table(title=f"EOL packages ({len(eol_rows)})", header_style="dim")
        s.add_column("package", style="cyan")
        s.add_column("method")
        s.add_column("reason", style="dim", overflow="fold", max_width=40)
        for r in sorted(eol_rows, key=lambda x: x["package"])[:30]:
            s.add_row(r["package"], r["eol_method"], r["eol_reason"][:80])
        console.print(s)
