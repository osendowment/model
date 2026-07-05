#!/usr/bin/env python3
"""Apply the OpenSSF criticality score onto value.csv as `openssf_crit` + `score`.

Roll-up step, run after `unify_value_data` / `build_validation` (both rebuild
value.csv without these columns' values). Reads the per-repo score fetched by
`src.sources.openssf.criticality` and joins it onto every GitHub row of
value.csv by repo id (value.csv `gh/<id>` ↔ criticality.csv id), with a
canonical-slug fallback for rows whose id is missing on either side.

Two columns are stamped:
  - `openssf_crit` — the OpenSSF criticality score (0–1).
  - `score` — a 0–100 value blend, criticality-dominant:
        score = 0.8·(openssf_crit·100) + 0.2·top_eco_pct
    (weights in settings.json → value_score). top_eco_pct is the repo's
    PageRank percentile in its top ecosystem; the blend rewards genuinely
    critical projects over foundational-but-quiet micro-deps. See docs/value.md.

Only rows with a successfully fetched score (`status = ok`) get values —
an error row must never masquerade as a real 0. Non-GitHub rows stay blank
(the tool is GitHub-only), and `score` is blank wherever `openssf_crit` is.

Coverage contract: **every valid class-A GitHub repo must end up with a
non-empty `openssf_crit`** — that is the fetcher's scope (archived included),
so a blank there means a missing/failed fetch, never "not applicable".
This script reports violations; `scripts/pipeline_health.py` enforces the
same rule as a pipeline gate.

Idempotent: re-running re-joins from the same inputs.

Usage:
    uv run python -m src.value.apply_criticality
"""

from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import (
    VALUE_SCORE_CENTRALITY_WEIGHT,
    VALUE_SCORE_CRIT_WEIGHT,
)
from src.value.unify_value_data import OUTPUT_FILE, write_value_data

console = Console()

ROOT = Path(__file__).resolve().parents[2]
CRITICALITY_FILE = ROOT / "data" / "sources" / "openssf" / "criticality.csv"


def load_criticality(path: Path = CRITICALITY_FILE) -> tuple[dict, dict]:
    """({repo_id: score}, {slug: score}) from status=ok criticality rows.

    Ids are the canonical `gh/<id>` form on both sides of the join
    (criticality.csv and value.csv), so no prefix juggling here."""
    by_id: dict[str, str] = {}
    by_slug: dict[str, str] = {}
    if not path.exists():
        return by_id, by_slug
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip().lower() != "ok":
                continue
            score = (row.get("criticality_score") or "").strip()
            if not score:
                continue
            rid = (row.get("repo_id") or "").strip()
            slug = (row.get("repo") or "").strip().lower()
            if rid:
                by_id[rid] = score
            if slug:
                by_slug[slug] = score
    return by_id, by_slug


def compute_score(openssf_crit: float, top_eco_pct: float) -> float:
    """value.csv `score` — 0–100 blend of criticality and ecosystem centrality.

    openssf_crit is 0–1; ·100 puts it on the same 0–100 scale as top_eco_pct
    (already a percentile). With the default 0.8/0.2 weights the result is
    bounded to [0, 100]."""
    return (VALUE_SCORE_CRIT_WEIGHT * (openssf_crit * 100.0)
            + VALUE_SCORE_CENTRALITY_WEIGHT * top_eco_pct)


def apply(value_file: Path = OUTPUT_FILE,
          criticality_file: Path = CRITICALITY_FILE) -> list[dict]:
    """Join `openssf_crit` + `score` onto value.csv rows in place; return rows."""
    by_id, by_slug = load_criticality(criticality_file)
    with open(value_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    matched = 0
    for row in rows:
        row["openssf_crit"] = ""
        row["score"] = ""
        if (row.get("platform") or "").strip().lower() != "github":
            continue
        rid = (row.get("repo_id") or "").strip()
        slug = (row.get("repo") or "").strip().lower()
        crit = by_id.get(rid) if rid else None
        if crit is None:
            crit = by_slug.get(slug)
        if crit is not None:
            row["openssf_crit"] = crit
            try:
                tep = float(row.get("top_eco_pct") or 0.0)
            except ValueError:
                tep = 0.0
            row["score"] = f"{compute_score(float(crit), tep):.2f}"
            matched += 1

    write_value_data(rows, value_file)
    return rows


def report(rows: list[dict]) -> list[str]:
    """Print coverage; return the violating repos (valid A github, blank)."""
    gate = [r for r in rows
            if (r.get("platform") or "").lower() == "github"
            and (r.get("git_valid") or "") == "True"
            and (r.get("class") or "") == "A"]
    violations = [r["repo"] for r in gate if not (r.get("openssf_crit") or "").strip()]

    filled_all = sum(1 for r in rows if (r.get("openssf_crit") or "").strip())
    scores = [float(r["score"]) for r in rows if (r.get("score") or "").strip()]
    table = Table(title="[bold]value.csv openssf_crit / score coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Scope", style="bold")
    table.add_column("Filled", justify="right")
    table.add_column("Total", justify="right")
    table.add_row("all rows", f"{filled_all:,}", f"{len(rows):,}")
    table.add_row("valid class-A github (gate)",
                  f"{len(gate) - len(violations):,}", f"{len(gate):,}",
                  style="bold")
    console.print(table)
    if scores:
        console.print(f"[dim]score  min/mean/max: {min(scores):.1f} / "
                      f"{sum(scores) / len(scores):.1f} / {max(scores):.1f}[/dim]")

    if violations:
        console.print(f"[red]GATE VIOLATED — {len(violations)} valid class-A "
                      f"github repo(s) without an openssf_crit score:[/red]")
        for repo in violations[:20]:
            console.print(f"  [red]{repo}[/red]")
        if len(violations) > 20:
            console.print(f"  [red]… and {len(violations) - 20} more[/red]")
    else:
        console.print("[green]Gate satisfied: every valid class-A github repo "
                      "has an openssf_crit score.[/green]")
    return violations


def main() -> None:
    console.print("[bold]Applying criticality + score onto value.csv...[/bold]\n")
    rows = apply()
    report(rows)


if __name__ == "__main__":
    main()
