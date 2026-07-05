#!/usr/bin/env python3
"""Build data/results.csv — the final fundable-candidate table.

The pipeline's terminal output. Every **eligible** top repo (eligibility.csv
`eligible == True`) paired with its value score (top-ecosystem download share)
and its overall risk score — the ranked shortlist the endowment funds from.
Ineligible repos (not OSS, no funding intent, company-backed, or inactive) are
excluded; filter upstream in eligibility.csv if you need the full set.

Columns:
    repo_id      GitHub numeric id (stable across renames)
    repo         canonical owner/name slug
    ecosystem    the repo's top ecosystem            (value.csv `top_eco`)
    value_score  top-ecosystem download share, %     (value.csv `top_eco_pct`)
    risk_score   overall risk score, 0-100           (risk.csv `score`)

Joins: eligibility.csv → risk.csv on the stable `repo_id`; eligibility.csv →
value.csv on the canonical `repo` slug (value.csv's `repo_id` is the
`gh/<n>` platform-qualified form, so the slug is the shared key there).

Sorted by risk_score desc (most at-risk first), then value_score desc.

Usage:
    uv run python -m src.build_results
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ELIGIBILITY_FILE = DATA_DIR / "eligibility" / "eligibility.csv"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
RISK_FILE = DATA_DIR / "risk" / "risk.csv"
OUTPUT_FILE = DATA_DIR / "results.csv"

FIELDS = ["repo_id", "repo", "ecosystem", "value_score", "risk_score"]


def _value_by_slug() -> dict[str, dict]:
    """{canonical repo slug (lower) → value.csv row}. value.csv `repo_id` is the
    `gh/<n>` platform form, so the slug is the join key shared with eligibility."""
    out: dict[str, dict] = {}
    if not VALUE_FILE.exists():
        return out
    with open(VALUE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip().lower()
            if slug:
                out[slug] = r
    return out


def _risk_score_by_id() -> dict[str, str]:
    """{repo_id → risk.csv `score`}; join on the stable numeric id."""
    out: dict[str, str] = {}
    if not RISK_FILE.exists():
        return out
    with open(RISK_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rid = (r.get("repo_id") or "").strip()
            if rid:
                out[rid] = (r.get("score") or "").strip()
    return out


def _num(x: str) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return -1.0


def build() -> list[dict]:
    value = _value_by_slug()
    risk = _risk_score_by_id()

    rows: list[dict] = []
    with open(ELIGIBILITY_FILE, encoding="utf-8") as f:
        for e in csv.DictReader(f):
            if (e.get("eligible") or "").strip() != "True":
                continue
            repo = (e.get("repo") or "").strip()
            rid = (e.get("repo_id") or "").strip()
            v = value.get(repo.lower(), {})
            rows.append({
                "repo_id": rid,
                "repo": repo,
                "ecosystem": (v.get("top_eco") or "").strip(),
                "value_score": (v.get("top_eco_pct") or "").strip(),
                "risk_score": risk.get(rid, ""),
            })
    rows.sort(key=lambda r: (_num(r["risk_score"]), _num(r["value_score"])), reverse=True)
    return rows


def main() -> None:
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    table = Table(title="[bold]Results — top fundable candidates[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("repo", style="bold")
    table.add_column("eco")
    table.add_column("value", justify="right")
    table.add_column("risk", justify="right")
    for r in rows[:10]:
        table.add_row(r["repo"], r["ecosystem"], r["value_score"], r["risk_score"])
    console.print(table)
    console.print(f"\n[dim]Wrote {len(rows):,} eligible repos × {len(FIELDS)} columns → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
