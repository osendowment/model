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
    code      ← risk/complexity.csv `loc_eoy` — the measured source snapshot
                has code (loc > 0). Archived stubs whose default branch was
                stripped to a README measure loc=0 and read False — there is
                no code to fund. A missing measurement also reads False
                (complexity covers 100% of the scope, so that only blocks
                genuinely unmeasured repos; the auditable detail — loc_eoy,
                loc_year — stays in complexity.csv).

    eligible = oss AND intent AND nonprofit AND active AND code

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
VALUE_FILE = DATA_DIR / "value" / "value.csv"
RISK_FILE = DATA_DIR / "risk" / "risk.csv"
COMPLEXITY_FILE = DATA_DIR / "risk" / "complexity.csv"
OUTPUT_FILE = DATA_DIR / "eligibility" / "eligibility.csv"

# The eligibility verdict flags, in column order. `eligible` = AND of all.
FLAGS = ("oss", "intent", "nonprofit", "active", "code")

# Signal-completeness components. A component counts only when its cell is
# non-empty AND non-zero — a blank is a missing signal, and a 0 (e.g. an
# explicit eco_crit=False, or a zero risk dimension) is a null contribution.
VALUE_COMPONENTS = ("openssf_crit", "eco_crit", "top_eco_pct")            # value.csv
RISK_COMPONENTS = ("concentration", "complexity", "security", "workload")  # risk.csv

FIELDS = ["repo", "repo_id", *FLAGS, "eligible",
          "value_comps", "risk_comps", "complete"]


def _is_positive(val: str) -> bool:
    """True iff `val` parses as a number > 0. Blank / zero / junk → False."""
    try:
        return float((val or "").strip() or 0) > 0
    except ValueError:
        return False


def _count_nonzero(values) -> int:
    """How many of `values` are present AND non-zero (blank / 0 don't count)."""
    n = 0
    for v in values:
        s = (v or "").strip()
        if not s:
            continue
        try:
            if float(s) != 0.0:
                n += 1
        except ValueError:
            n += 1  # a non-numeric non-empty flag still counts as present
    return n


def build() -> list[dict]:
    # The per-dimension CSVs are joined by the stable repo_id, like every
    # other id-capable join in the pipeline — a slug join would only be
    # safe while the intermediates are freshly co-generated; across a
    # GitHub rename with a stale intermediate it would misalign the flags.
    eligible_scope = load_top_repos()
    oss = load_column_by_id(LICENSES_FILE, "oss")
    intent = load_column_by_id(FUNDING_FILE, "intent")
    nonprofit = load_column_by_id(FUNDING_FILE, "nonprofit")
    active = load_column_by_id(ACTIVE_FILE, "active")
    loc_eoy = load_column_by_id(COMPLEXITY_FILE, "loc_eoy")
    value_cols = {c: load_column_by_id(VALUE_FILE, c) for c in VALUE_COMPONENTS}
    risk_cols = {c: load_column_by_id(RISK_FILE, c) for c in RISK_COMPONENTS}

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
            "code": _is_positive(loc_eoy.get(rid, "")),
        }
        # Signal completeness: how many value / risk components carry a real
        # (non-empty, non-zero) value, and whether the repo is fully covered.
        value_comps = _count_nonzero(value_cols[c].get(rid, "") for c in VALUE_COMPONENTS)
        risk_comps = _count_nonzero(risk_cols[c].get(rid, "") for c in RISK_COMPONENTS)
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            **flags,
            "eligible": all(flags.values()),
            "value_comps": value_comps,
            "risk_comps": risk_comps,
            "complete": value_comps >= 2 and risk_comps == 4,
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
    for flag in FLAGS:
        n = sum(1 for r in rows if r[flag])
        # Repos failing ONLY this check — what fixing it alone would unlock.
        sole = sum(1 for r in rows
                   if not r[flag] and all(r[f] for f in FLAGS if f != flag))
        table.add_row(flag, f"{n:,}", f"{100 * n / total:.1f}%", f"{sole:,}")
    table.add_section()
    table.add_row("[bold green]eligible[/bold green]",
                  f"[bold green]{n_eligible:,}[/bold green]",
                  f"[bold green]{100 * n_eligible / total:.1f}%[/bold green]", "")
    console.print(table)

    # Signal completeness (value_comps ≥ 2 AND risk_comps = 4).
    incomplete = [r for r in rows if not r["complete"]]
    low_value = sum(1 for r in incomplete if r["value_comps"] < 2)
    low_risk = sum(1 for r in incomplete if r["risk_comps"] < 4)
    console.print(
        f"\n[bold]complete[/bold] (value_comps≥2 & risk_comps=4): "
        f"[green]{total - len(incomplete):,}[/green] / {total:,}  |  "
        f"[yellow]incomplete {len(incomplete):,}[/yellow] "
        f"(value_comps<2: {low_value:,}, risk_comps<4: {low_risk:,})")

    console.print(f"\n[dim]Wrote {total:,} repos × {len(FIELDS)} columns → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
