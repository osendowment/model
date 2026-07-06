"""Shared schema, I/O, and display for per-ecosystem EOL checks.

Each ecosystem has its own `check_eol.py` that produces `data/sources/{eco}/eol.csv`
keyed by package name. The schema is uniform so `src.value.unify_value_data` can
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

EOL_FIELDS = ["package", "is_eol", "eol_method", "eol_reason", "source", "eol_checked_at"]

# Deprecation status changes slowly; the registry-hitting checkers (npm, pypi)
# skip packages checked within this window instead of re-querying every one of
# them on every pipeline run. `error` rows are never fresh (always retried).
EOL_TTL_DAYS = 7


def now_iso() -> str:
    """UTC ISO 8601 timestamp to second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_fresh_eol(path: Path, ttl_days: int = EOL_TTL_DAYS) -> dict[str, dict]:
    """{package: row} for rows checked within `ttl_days` with a definitive source.

    A definitive source is anything except `error` — a network failure must
    never be cached as an answer (auditability), so those rows always
    re-fetch. `ttl_days <= 0` disables the skip (everything re-fetches).
    """
    if ttl_days <= 0 or not path.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    fresh: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("source") == "error":
                continue
            try:
                checked = datetime.fromisoformat(r.get("eol_checked_at", ""))
            except ValueError:
                continue
            if checked >= cutoff:
                fresh[r["package"]] = r
    return fresh


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
    for k, v in sorted(by_method.items()):
        t.add_row(f"[dim]method={k}[/dim]", f"[dim]{v:,}[/dim]")
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
