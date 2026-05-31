#!/usr/bin/env python3
"""Build data/risk/complexity.csv — codebase complexity per risk-scope repo.

Reads (long-format, sha-pinned):
    data/value/value.csv                     — A/B value-class set
    data/sources/github/git/commits-years.csv       — per (repo, year) last_sha + commits
    data/sources/git/scc.csv                        — long: scc metrics per (repo, sha)
    data/sources/git/lizard.csv                     — long: lizard metrics per (repo, sha)
    data/sources/github/git/churn.csv               — for `churn_5y_total` (hotspot inputs)

Writes:
    data/risk/complexity.csv  with columns:
        repo, repo_id,
        loc_2025_eoy, sloc_2025_eoy,
        scc_complexity_2025_eoy, scc_density_2025_eoy,
        cognitive_total, cognitive_avg, cognitive_max,
        cyclomatic_total, cyclomatic_avg, cyclomatic_max,
        loc_year   (year of snapshot used: "2021"…"2025", or "" if missing)
        churn_5y_total,
        hotspot_raw,           # churn × complexity (linear)
        hotspot_log,           # log10(churn+1) × log10(complexity+1)
        hotspot_log_p,         # risk percentile of hotspot_log (higher = riskier)
        loc_2025_eoy_p,        # risk percentile of loc_2025_eoy
        scc_complexity_2025_eoy_p,
        cognitive_max_p,
        cyclomatic_max_p,
        churn_5y_total_p,
        complexity_p           # geometric mean of loc_2025_eoy_p + cyclomatic_max_p

Percentile system (0-100, higher = riskier):
    Each _p column is a worst-pinned CDF percentile within the repos that have
    a non-missing value for that metric. The worst value maps to 100.
    complexity_p = geometric mean of loc_2025_eoy_p and cyclomatic_max_p,
    available only when both component _p's are present.

Period: [2025 EOY] = scc / lizard analysis of the last commit on the
default branch ≤ 2025-12-31. We pick, per repo, the most-recent year
≤ 2025 where commits-years.csv has a `last_sha` populated AND
`commits > 0`. If no year has a usable sha, the row is left empty —
we don't fall back to HEAD or to a stale sha.

Metric mapping:
    scc.loc                      → loc_2025_eoy
    scc.sloc                     → sloc_2025_eoy
    scc.complexity               → scc_complexity_2025_eoy
    scc.complexity_density       → scc_density_2025_eoy
    lizard.cognitive_total       → cognitive_total
    lizard.cognitive_avg         → cognitive_avg
    lizard.cognitive_max         → cognitive_max
    lizard.cyclomatic_total      → cyclomatic_total
    lizard.cyclomatic_avg        → cyclomatic_avg
    lizard.cyclomatic_max        → cyclomatic_max  (per-function McCabe)

Hotspot folding (Adam Tornhill, "Code as a Crime Scene"):
bug-prone code = high churn ∩ high complexity. We translate that to
repo-level by joining 5y churn (added+deleted) with the EOY-2025 scc
complexity snapshot. `hotspot_log` is the canonical Tornhill score —
log-scaling tames the extreme right tail (apache/airflow vs
hukkin/tomli are 4-5 orders of magnitude apart on the linear scale).
Empty when either input is missing.

Usage:
    uv run python -m src.risk.build_complexity
"""

from __future__ import annotations

import csv
import math
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.sources.git.long_format import read as read_long
from src.common.percentiles import add_percentiles
from src.common.repos import load_risk_repos
from src.common.tables import load_rows_by_repo

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
GIT_LONG_DIR = DATA_DIR / "sources" / "git"
COMMITS_YEARS_FILE = DATA_DIR / "sources" / "github" / "git" / "commits-years.csv"
SCC_FILE = GIT_LONG_DIR / "scc.csv"
LIZARD_FILE = GIT_LONG_DIR / "lizard.csv"
CHURN_FILE = DATA_DIR / "sources" / "github" / "git" / "churn.csv"
OUTPUT_FILE = DATA_DIR / "risk" / "complexity.csv"

SCC_METRICS = ["loc", "sloc", "complexity", "complexity_density"]
LIZARD_METRICS = [
    "cognitive_total", "cognitive_avg", "cognitive_max",
    "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
]

FIELDS = [
    "repo", "repo_id",
    "loc_2025_eoy", "sloc_2025_eoy",
    "scc_complexity_2025_eoy", "scc_density_2025_eoy",
    "cognitive_total", "cognitive_avg", "cognitive_max",
    "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
    "loc_year",
    "churn_5y_total",
    "hotspot_raw", "hotspot_log", "hotspot_log_p",
    "loc_2025_eoy_p", "scc_complexity_2025_eoy_p", "cognitive_max_p",
    "cyclomatic_max_p", "churn_5y_total_p",
    "score",
]

def _per_year_shas(commits_years_file: Path) -> dict[str, dict[int, str]]:
    """Return {repo: {year_int: last_sha}} for usable (sha, year) pairs.

    A pair is "usable" when ``last_sha`` is non-empty AND ``commits > 0``.
    Years are clamped to 2021..2025. Repos with no usable year get no key.
    """
    by_repo: dict[str, dict[int, str]] = {}
    if not commits_years_file.exists():
        return {}
    with open(commits_years_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            last_sha = (row.get("last_sha") or "").strip()
            if not last_sha:
                continue
            yr_raw = (row.get("year") or "").strip()
            if yr_raw == "HEAD":
                # Dormant repo (no commits 2021-2025). resolve_head writes
                # this pseudo-row. Bucket as year=0 so the walk-down loop
                # in build() can pick it up as a last-resort fallback.
                by_repo.setdefault(slug, {})[0] = last_sha
                continue
            try:
                year = int(yr_raw)
                commits = int((row.get("commits") or "0").strip())
            except ValueError:
                continue
            if commits <= 0:
                continue
            if 2021 <= year <= 2025:
                by_repo.setdefault(slug, {})[year] = last_sha
    return by_repo


def _is_nonzero(val: str) -> bool:
    """True iff `val` parses as a non-zero number. Empty/zero/junk → False."""
    if not val:
        return False
    try:
        return float(val) != 0
    except ValueError:
        return False


def _to_int(s: str) -> int:
    """Parse '12345' (or '') to int. Empty / unparseable → 0."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _hotspot_log(churn: int, complexity: int) -> float:
    """Tornhill log-scaled hotspot: log10(churn+1) * log10(complexity+1)."""
    if churn <= 0 or complexity <= 0:
        return 0.0
    return math.log10(churn + 1) * math.log10(complexity + 1)


def build() -> list[dict]:
    eligible = load_risk_repos()

    # 1. Build per-repo year→sha lookup from commits-years.csv.
    per_year = _per_year_shas(COMMITS_YEARS_FILE)

    # 2. Index long-format rows as (repo, sha) → {metric: value}.
    #    We can't use project_to_wide here because we may need to walk back
    #    through multiple shas per repo: scc occasionally records loc=0 from
    #    a shallow/failed checkout, which we treat as "not measured" and skip
    #    to the next-most-recent year's snapshot.
    scc_rows = read_long(SCC_FILE)
    lizard_rows = read_long(LIZARD_FILE)

    # (repo, sha) → {metric: value}
    scc_idx: dict[tuple[str, str], dict[str, str]] = {}
    for (r, s, m), row in scc_rows.items():
        if m in SCC_METRICS:
            scc_idx.setdefault((r, s), {})[m] = row["value"]

    lizard_idx: dict[tuple[str, str], dict[str, str]] = {}
    for (r, s, m), row in lizard_rows.items():
        if m in LIZARD_METRICS:
            lizard_idx.setdefault((r, s), {})[m] = row["value"]

    # 3. Load 5y churn (hotspot input).
    churn_by_repo = load_rows_by_repo(CHURN_FILE)

    rows: list[dict] = []

    for entry in eligible:
        repo = entry.repo
        year_to_sha = per_year.get(repo, {})

        # Walk 2025→2021. Pick the most-recent year whose sha has scc loc>0.
        # If none qualifies, leave the row empty (no HEAD fallback).
        scc_vals: dict[str, str] = {}
        lz_vals: dict[str, str] = {}
        year_label = ""
        for y in (2025, 2024, 2023, 2022, 2021, 0):
            sha = year_to_sha.get(y)
            if not sha:
                continue
            candidate = scc_idx.get((repo, sha), {})
            if _is_nonzero(candidate.get("loc", "")):
                scc_vals = candidate
                lz_vals = lizard_idx.get((repo, sha), {})
                year_label = "HEAD" if y == 0 else str(y)
                break

        # Hotspot: combine churn × scc_complexity_2025_eoy.
        churn_row = churn_by_repo.get(repo, {})
        churn_total = _to_int(churn_row.get("churn_5y_total"))
        cx_value = _to_int(scc_vals.get("complexity"))
        churn_known = bool(churn_row)
        cx_known = bool(scc_vals.get("complexity"))

        churn_out = str(churn_total) if churn_known else ""
        hotspot_raw_val = ""
        hotspot_log_val = ""
        if churn_known and cx_known:
            raw = churn_total * cx_value
            log_val = _hotspot_log(churn_total, cx_value)
            hotspot_raw_val = str(raw)
            hotspot_log_val = f"{log_val:.4f}"

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "loc_2025_eoy": scc_vals.get("loc", ""),
            "sloc_2025_eoy": scc_vals.get("sloc", ""),
            "scc_complexity_2025_eoy": scc_vals.get("complexity", ""),
            "scc_density_2025_eoy": scc_vals.get("complexity_density", ""),
            "cognitive_total": lz_vals.get("cognitive_total", ""),
            "cognitive_avg": lz_vals.get("cognitive_avg", ""),
            "cognitive_max": lz_vals.get("cognitive_max", ""),
            "cyclomatic_total": lz_vals.get("cyclomatic_total", ""),
            "cyclomatic_avg": lz_vals.get("cyclomatic_avg", ""),
            "cyclomatic_max": lz_vals.get("cyclomatic_max", ""),
            "loc_year": year_label,
            "churn_5y_total": churn_out,
            "hotspot_raw": hotspot_raw_val,
            "hotspot_log": hotspot_log_val,
        })

    add_percentiles(
        rows,
        pctl_specs=[
            ("hotspot_log", True), ("loc_2025_eoy", True),
            ("scc_complexity_2025_eoy", True), ("cognitive_max", True),
            ("cyclomatic_max", True), ("churn_5y_total", True),
        ],
        composite_cols=["loc_2025_eoy_p", "cyclomatic_max_p"],
        dim_col="score",
    )

    return rows


def main() -> None:
    console.print("[bold]Building complexity.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Complexity coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in ("loc_2025_eoy", "sloc_2025_eoy",
                "scc_complexity_2025_eoy", "scc_density_2025_eoy",
                "cognitive_total", "cognitive_avg", "cognitive_max",
                "cyclomatic_total", "cyclomatic_avg", "cyclomatic_max",
                "churn_5y_total",
                "hotspot_raw", "hotspot_log", "hotspot_log_p",
                "score"):
        n = sum(1 for r in rows if r[col])
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    # Year-fallback breakdown
    year_dist = Table(title="\n[bold]LOC snapshot year used[/bold]",
                      show_header=True, header_style="bold dim", padding=(0, 1))
    year_dist.add_column("Year", style="bold")
    year_dist.add_column("Repos", justify="right")
    year_dist.add_column("%", justify="right")
    yc = Counter(r["loc_year"] or "(none)" for r in rows)
    for y in ("2025", "2024", "2023", "2022", "2021", "(none)"):
        n = yc.get(y, 0)
        pct = 100 * n / total if total else 0
        year_dist.add_row(y, f"{n:,}", f"{pct:.1f}%")
    console.print(year_dist)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
