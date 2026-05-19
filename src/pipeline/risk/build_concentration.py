#!/usr/bin/env python3
"""Build data/concentration.csv — contributor-concentration metrics per risk-scope repo.

Reads:
    data/value-data.csv          — A/B value-class set (via load_risk_repos)
    data/concentration-data.csv  — lifetime totals: total_commits, total_contributors,
                                   active_contributors (non-bot distinct), bus_factor,
                                   hhi, fetched_at. The per-year wide CSVs under
                                   data/github/contributors/ were deleted; this is
                                   now the sole concentration input.

Writes:
    data/concentration.csv  with columns:
        repo, repo_id,
        total_commits_lifetime,       (lifetime /commits Link-header count)
        total_contributors_lifetime,  (lifetime /contributors?anon=true Link-header count)
        active_contributors,          (distinct non-bot contributors — floor for repos
                                       where the API caps at 5 000)
        hhi_commits_lifetime,         (HHI 0-10 000 over lifetime commits)
        bf_commits_lifetime,          (bus factor over lifetime commits)
        concentration_class,          (A/B/C/D — joint bus-factor + HHI test,
                                       settings.json risk_classification.concentration)
        fetched_at

Usage:
    uv run python -m src.pipeline.risk.build_concentration
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.params import CONCENTRATION_THRESHOLDS
from src.pipeline.common.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
LIFETIME_FILE = DATA_DIR / "concentration-data.csv"
OUTPUT_FILE = DATA_DIR / "concentration.csv"

FIELDS = [
    "repo", "repo_id",
    "total_commits_lifetime", "total_contributors_lifetime",
    "active_contributors",
    "hhi_commits_lifetime", "bf_commits_lifetime",
    "concentration_class",
    "fetched_at",
]


def concentration_class(bus_factor: str, hhi: str) -> str:
    """A/B/C/D from bus factor + HHI — A is the worst (most concentrated).

    Per settings.json risk_classification.concentration, a repo is class X
    when ``bus_factor <= max_bus_factor[X]`` AND ``hhi >= min_hhi[X]``,
    tested strictest-first (A, then B, then C); D when none match. Both
    signals must agree: concentration risk needs a fragile bus factor *and*
    commit-share dominance — a repo with many contributors is not flagged
    just because one of them commits often, and vice versa.

    Returns "" when either input is missing or unparseable — can't classify.
    """
    bf_s = (bus_factor or "").strip()
    hhi_s = (hhi or "").strip()
    if not bf_s or not hhi_s:
        return ""
    try:
        bf = float(bf_s)
        h = float(hhi_s)
    except ValueError:
        return ""
    for cls in ("A", "B", "C"):
        t = CONCENTRATION_THRESHOLDS[cls]
        if bf <= t["max_bus_factor"] and h >= t["min_hhi"]:
            return cls
    return "D"


def _load_lifetime() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not LIFETIME_FILE.exists():
        return out
    with open(LIFETIME_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def build() -> list[dict]:
    eligible = load_risk_repos()
    lifetime = _load_lifetime()

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        lt = lifetime.get(repo, {})
        hhi = (lt.get("hhi") or "").strip()
        bf = (lt.get("bus_factor") or "").strip()
        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "total_commits_lifetime": (lt.get("total_commits") or "").strip(),
            "total_contributors_lifetime": (lt.get("total_contributors") or "").strip(),
            "active_contributors": (lt.get("active_contributors") or "").strip(),
            "hhi_commits_lifetime": hhi,
            "bf_commits_lifetime": bf,
            "concentration_class": concentration_class(bf, hhi),
            "fetched_at": (lt.get("fetched_at") or "").strip(),
        })
    return rows


def main() -> None:
    console.print("[bold]Building concentration.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Concentration coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (
        "total_commits_lifetime", "total_contributors_lifetime",
        "active_contributors",
        "hhi_commits_lifetime", "bf_commits_lifetime",
        "concentration_class",
    ):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    # concentration_class distribution (A/B/C/D, or — when unclassifiable).
    cls = Counter(r["concentration_class"] or "—" for r in rows)
    ctable = Table(title="\n[bold]Concentration class[/bold]",
                   show_header=True, header_style="bold dim", padding=(0, 1))
    ctable.add_column("Class", style="bold")
    ctable.add_column("Repos", justify="right")
    for label in ("A", "B", "C", "D", "—"):
        ctable.add_row(label, f"{cls.get(label, 0):,}")
    console.print(ctable)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
