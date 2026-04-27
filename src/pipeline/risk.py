#!/usr/bin/env python3
"""Stage 3 of the pipeline — concentration + complexity + issue risk per repo.

Pipeline order (each stage feeds the next):
    1. `src.pipeline.value`       → data/value-data.csv  (universe)
    2. `src.pipeline.eligibility` → data/eligibility-data.csv  (AB ∩ OSS ∩ alive)
    3. `src.pipeline.risk`        → data/risk-data.csv  (this script)

**Input scope is restricted to eligible repos** — i.e. rows in
`eligibility-data.csv` with `eligibility=True`. Anything that isn't
licensed-OSS, that's EOL, or that 404'd on the GitHub API is excluded
before any risk metrics are even computed.

Reads:
    data/eligibility-data.csv                          — input set
    data/github/contributors/{bus-factor,hhi,contributors}.csv
                                                        (2021-2025 aggregate)
    data/github/git/loc.csv                            (most recent year)
    data/github/issues/{opened,closed}.csv             (5-year wide)

Writes:
    data/risk-data.csv  with columns:
        repo, repo_id, active_contributors, hhi_commits, bus_factor_commits,
        loc, concentration_class, complexity_class,
        issues_opened_5y, issues_closed_5y, issue_close_ratio,
        slope_opened, slope_closed, issue_trend_score,
        issue_trend, issue_debt_class.

Usage:
    uv run python -m src.pipeline.risk
"""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.params import (
    CONCENTRATION_THRESHOLDS,
    COMPLEXITY_LOC_THRESHOLDS,
    ISSUE_DEBT_THRESHOLDS,
    ISSUE_TREND_THRESHOLDS,
    YEARS,
)
from src.pipeline.repos import load_eligible_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONTRIB_DIR = DATA_DIR / "github" / "contributors"
LOC_FILE = DATA_DIR / "github" / "git" / "loc.csv"
ISSUES_DIR = DATA_DIR / "github" / "issues"
OUTPUT_FILE = DATA_DIR / "risk-data.csv"
AGG_COL = "2021-2025"
LOC_YEAR = "2025"  # most recent year in git/loc.csv

FIELDS = [
    "repo", "repo_id",
    # concentration
    "active_contributors", "hhi_commits", "bus_factor_commits", "concentration_class",
    # complexity
    "loc", "complexity_class",
    # issues — debt + trend
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio",
    "slope_opened", "slope_closed", "issue_trend_score",
    "issue_trend", "issue_debt_class",
]


def concentration_class(bus_factor: int, hhi: int) -> str:
    """Classify repo concentration risk as A/B/C/D.

    A (critical):  BF=1  and HHI >= 8000 — single maintainer, extreme concentration
    B (high risk): BF<=2 and HHI >= 5000 — tiny core, high concentration
    C (moderate):  BF<=4 and HHI >= 2500 — small team, moderate concentration
    D (healthy):   everything else        — distributed enough
    """
    if bus_factor <= CONCENTRATION_THRESHOLDS["A"]["max_bus_factor"] and hhi >= CONCENTRATION_THRESHOLDS["A"]["min_hhi"]:
        return "A"
    if bus_factor <= CONCENTRATION_THRESHOLDS["B"]["max_bus_factor"] and hhi >= CONCENTRATION_THRESHOLDS["B"]["min_hhi"]:
        return "B"
    if bus_factor <= CONCENTRATION_THRESHOLDS["C"]["max_bus_factor"] and hhi >= CONCENTRATION_THRESHOLDS["C"]["min_hhi"]:
        return "C"
    return "D"


def complexity_class(loc: int | None) -> str:
    """Classify repo complexity based on scc-counted lines of code.

    A (massive):   1M+ LOC   — enormous codebase, high cognitive load
    B (large):     100K–1M   — significant, requires dedicated team
    C (moderate):  10K–100K  — typical project, manageable but non-trivial
    D (small):     <10K LOC  — easy to audit and understand
    """
    if loc is None or loc == 0:
        return ""
    if loc >= COMPLEXITY_LOC_THRESHOLDS["A"]:
        return "A"
    if loc >= COMPLEXITY_LOC_THRESHOLDS["B"]:
        return "B"
    if loc >= COMPLEXITY_LOC_THRESHOLDS["C"]:
        return "C"
    return "D"


def issue_debt_class(opened_5y: int, close_ratio: float) -> str:
    """Classify issue-debt risk from 5-year totals.

    A (critical):   close_ratio < 0.30 AND opened_5y >= 100  — drowning in backlog
    B (high risk):  close_ratio < 0.60 AND opened_5y >= 30   — sustained debt
    C (moderate):   close_ratio < 0.85 AND opened_5y >= 10   — slight drift
    D (healthy):    close_ratio >= 0.85, OR opened_5y >= 10  — keeping up
    "" (no signal): opened_5y < min_opened_5y (issues disabled / unused)
    """
    if opened_5y < ISSUE_DEBT_THRESHOLDS["min_opened_5y"]:
        return ""
    for cls in ("A", "B", "C"):
        t = ISSUE_DEBT_THRESHOLDS[cls]
        if close_ratio < t["max_close_ratio"] and opened_5y >= t["min_opened_5y"]:
            return cls
    return "D"


def _ols_slope(years: list[int], values: list[int]) -> float:
    """Plain ordinary-least-squares slope for a small (year, value) series.
    Returns 0 if the series is too short or has no variance in years."""
    n = len(years)
    if n < 2:
        return 0.0
    mean_y = sum(years) / n
    mean_v = sum(values) / n
    var_y = sum((y - mean_y) ** 2 for y in years)
    if var_y == 0:
        return 0.0
    cov_yv = sum((y - mean_y) * (v - mean_v) for y, v in zip(years, values))
    return cov_yv / var_y


def issue_trend(opened: dict[int, int], closed: dict[int, int]
                ) -> tuple[float, float, float, str]:
    """Return (slope_opened, slope_closed, normalized_trend_score, label).

    Method: fit OLS slopes through the 5 yearly counts of opened and closed
    issues separately. Normalise the gap (slope_closed - slope_opened) by
    the mean opened-per-year volume so the score is comparable across
    project sizes. Then bucket via params.json thresholds.

    Empty label when there's not enough signal (volume floor or active-year floor).
    """
    years = sorted(YEARS)
    opened_vals = [opened.get(y, 0) for y in years]
    closed_vals = [closed.get(y, 0) for y in years]

    total_opened = sum(opened_vals)
    active_years = sum(1 for v in opened_vals if v > 0)
    if total_opened < ISSUE_TREND_THRESHOLDS["min_total_for_trend"]:
        return 0.0, 0.0, 0.0, ""
    if active_years < ISSUE_TREND_THRESHOLDS["min_active_years"]:
        return 0.0, 0.0, 0.0, ""

    s_open = _ols_slope(years, opened_vals)
    s_close = _ols_slope(years, closed_vals)

    mean_opened = total_opened / len(years)
    if mean_opened < 1:
        return s_open, s_close, 0.0, ""

    norm = (s_close - s_open) / mean_opened
    if norm >= ISSUE_TREND_THRESHOLDS["improving"]:
        label = "improving"
    elif norm <= ISSUE_TREND_THRESHOLDS["deteriorating"]:
        label = "deteriorating"
    else:
        label = "stable"
    return s_open, s_close, norm, label


def _read_issues_per_year(filename: str) -> dict[str, dict[int, int]]:
    """Return {repo_lowercased: {year: count}} from data/github/issues/<filename>."""
    path = ISSUES_DIR / filename
    out: dict[str, dict[int, int]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = (r.get("repo") or "").strip().lower()
            if not repo:
                continue
            out[repo] = {y: int(r.get(str(y)) or 0) for y in YEARS}
    return out


def _load_repo_ids() -> dict[str, str]:
    """Load repo slug → repo_id mapping for **eligible** repos only.

    Eligibility is the gate to risk: anything not eligible isn't scored.
    """
    return {e.repo: e.repo_id for e in load_eligible_repos() if e.repo_id}


def _load_locs() -> dict[str, int]:
    """Load repo → loc mapping from git/loc.csv, using the most recent year."""
    mapping: dict[str, int] = {}
    if LOC_FILE.exists():
        with open(LOC_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                val = row.get(LOC_YEAR, "")
                if val:
                    mapping[row["repo"]] = int(val)
    return mapping


def _load_contrib_metric(filename: str) -> dict[str, str]:
    """Load {repo: 2021-2025 aggregate value} from a wide per-metric CSV."""
    path = CONTRIB_DIR / filename
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            val = row.get(AGG_COL, "")
            if val:
                out[row["repo"]] = val
    return out


def aggregate() -> tuple[list[dict], int]:
    """Read contributor + LOC + issue metrics and return risk-classified rows.

    Iteration is gated by eligibility: only repos in `eligibility-data.csv`
    with `eligibility=True` get scored, even if contributor metrics exist
    for other repos (e.g. left over from a wider earlier scope).
    """
    repo_ids = _load_repo_ids()
    eligible = set(repo_ids.keys())
    repo_locs = _load_locs()

    bf_by_repo = _load_contrib_metric("bus-factor.csv")
    hhi_by_repo = _load_contrib_metric("hhi.csv")
    contribs_by_repo = _load_contrib_metric("contributors.csv")

    opened_by_repo = _read_issues_per_year("opened.csv")
    closed_by_repo = _read_issues_per_year("closed.csv")

    rows = []
    skipped = 0
    universe = (set(bf_by_repo) | set(hhi_by_repo) | set(contribs_by_repo)) & eligible
    for repo in sorted(universe):
        bf_str = bf_by_repo.get(repo, "")
        hhi_str = hhi_by_repo.get(repo, "")
        contributors_str = contribs_by_repo.get(repo, "")

        if not bf_str or not hhi_str:
            skipped += 1
            continue

        bf = int(bf_str)
        hhi = int(hhi_str)
        contributors = int(contributors_str) if contributors_str else 0
        locs = repo_locs.get(repo)

        opened = opened_by_repo.get(repo, {})
        closed = closed_by_repo.get(repo, {})
        opened_5y = sum(opened.values())
        closed_5y = sum(closed.values())
        close_ratio = (closed_5y / opened_5y) if opened_5y > 0 else 0.0
        s_open, s_close, trend_score, trend_label = issue_trend(opened, closed)

        rows.append({
            "repo": repo,
            "repo_id": repo_ids.get(repo, ""),
            "active_contributors": contributors,
            "hhi_commits": hhi,
            "bus_factor_commits": bf,
            "concentration_class": concentration_class(bf, hhi),
            "loc": locs if locs is not None else "",
            "complexity_class": complexity_class(locs),
            "issues_opened_5y": opened_5y,
            "issues_closed_5y": closed_5y,
            "issue_close_ratio": (round(close_ratio, 3) if opened_5y >= 1 else ""),
            "slope_opened": (round(s_open, 2) if opened_5y >= 1 else ""),
            "slope_closed": (round(s_close, 2) if opened_5y >= 1 else ""),
            "issue_trend_score": (round(trend_score, 4) if trend_label else ""),
            "issue_trend": trend_label,
            "issue_debt_class": issue_debt_class(opened_5y, close_ratio),
        })

    return rows, skipped


CONCENTRATION_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

CONCENTRATION_CRITERIA = {
    "A": "BF=1, HHI≥8000",
    "B": "BF≤2, HHI≥5000",
    "C": "BF≤4, HHI≥2500",
    "D": "otherwise",
}

COMPLEXITY_LABELS = {
    "A": ("massive", "red"),
    "B": ("large", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("small", "green"),
}

COMPLEXITY_CRITERIA = {
    "A": "≥1M LOC",
    "B": "100K–1M",
    "C": "10K–100K",
    "D": "<10K LOC",
}

ISSUE_DEBT_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

ISSUE_DEBT_CRITERIA = {
    "A": "ratio<0.30, ≥100/5y",
    "B": "ratio<0.60, ≥30/5y",
    "C": "ratio<0.85, ≥10/5y",
    "D": "ratio≥0.85",
}


def _print_class_table(
    title: str, rows: list[dict], field: str,
    labels: dict, criteria: dict,
) -> None:
    """Print a classification distribution table."""
    total = len(rows)
    dist = Counter(r[field] for r in rows if r[field])

    table = Table(title=f"[bold]{title}[/bold]", show_header=True,
                  header_style="bold dim", padding=(0, 1))
    table.add_column("Class", style="dim")
    table.add_column("", min_width=10)
    table.add_column("Criteria", style="dim")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")

    for cls in "ABCD":
        label, color = labels[cls]
        n = dist.get(cls, 0)
        pct = 100 * n / total if total else 0
        table.add_row(f"[{color} bold]{cls}[/{color} bold]",
                      f"[{color}]{label}[/{color}]",
                      criteria[cls],
                      f"{n:,}", f"{pct:.1f}%")

    classified = sum(dist.values())
    unclassified = total - classified
    table.add_section()
    table.add_row("[bold]–[/bold]", "[bold]classified[/bold]", "", f"[bold]{classified:,}[/bold]", "")
    if unclassified:
        table.add_row("", f"[dim]no data[/dim]", "", f"[dim]{unclassified:,}[/dim]", "")

    console.print(table)


def _print_trend_table(rows: list[dict]) -> None:
    """Print issue-trend distribution: improving / stable / deteriorating / no signal."""
    total = len(rows)
    dist = Counter(r["issue_trend"] for r in rows if r["issue_trend"])
    no_signal = total - sum(dist.values())

    table = Table(title="[bold]Issue trend (5-year slope)[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Trend", style="dim")
    table.add_column("Repos", justify="right")
    table.add_column("%", justify="right")
    for label, color in (("improving", "green"),
                         ("stable", "cyan"),
                         ("deteriorating", "red")):
        n = dist.get(label, 0)
        pct = 100 * n / total if total else 0.0
        table.add_row(f"[{color}]{label}[/{color}]", f"{n:,}", f"{pct:.1f}%")
    table.add_section()
    table.add_row("[dim]no signal[/dim]", f"[dim]{no_signal:,}[/dim]",
                  f"[dim]{100 * no_signal / total:.1f}%[/dim]" if total else "")
    console.print(table)


def main():
    console.print("[bold]Aggregating risk metrics...[/bold]\n")
    rows, skipped = aggregate()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _print_class_table("Concentration", rows, "concentration_class",
                       CONCENTRATION_LABELS, CONCENTRATION_CRITERIA)
    console.print()
    _print_class_table("Complexity", rows, "complexity_class",
                       COMPLEXITY_LABELS, COMPLEXITY_CRITERIA)
    console.print()
    _print_class_table("Issue debt", rows, "issue_debt_class",
                       ISSUE_DEBT_LABELS, ISSUE_DEBT_CRITERIA)
    console.print()
    _print_trend_table(rows)

    total = len(rows)
    console.print()
    if skipped:
        console.print(f"[dim]{skipped:,} repos skipped (missing BF/HHI)[/dim]")
    console.print(f"[dim]Written {total:,} repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
