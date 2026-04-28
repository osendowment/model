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
    data/eligibility-data.csv          — input set (eligible repos only)
    data/concentration-data.csv        — per-repo BF/HHI/total_commits/total_contributors
    data/github/git/loc.csv            — most recent year
    data/github/issues/{opened,closed}.csv  — 5-year wide
    data/openssf/scores.csv            — Scorecard scores

Writes:
    data/risk-data.csv  with columns:
        repo, repo_id, total_commits, total_contributors,
        hhi_commits, bf_commits, concentration_class,
        loc, complexity_class,
        openssf_score, security_class,
        issues_opened_5y, issues_closed_5y, issue_close_ratio,
        slope_opened, slope_closed, issue_trend_score,
        issue_trend, issue_debt_class,
        risk_class.

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
    SECURITY_THRESHOLDS,
    YEARS,
)
from src.pipeline.repos import load_eligible_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONCENTRATION_FILE = DATA_DIR / "concentration-data.csv"
FUNDING_FILE = DATA_DIR / "funding-data.csv"
LOC_FILE = DATA_DIR / "github" / "git" / "loc.csv"
ISSUES_DIR = DATA_DIR / "github" / "issues"
OPENSSF_FILE = DATA_DIR / "openssf" / "scores.csv"
GH_REPOS_FILE = DATA_DIR / "github" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "risk-data.csv"
LOC_YEAR = "2025"  # most recent year in git/loc.csv

FIELDS = [
    "repo", "repo_id", "created_at",
    # concentration (lifetime BF/HHI from data/concentration-data.csv)
    "total_commits", "total_contributors",
    "hhi_commits", "bf_commits", "concentration_class",
    # complexity
    "loc", "complexity_class",
    # security (OpenSSF Scorecard)
    "openssf_score", "security_class",
    # funding (GitHub Sponsors + FUNDING.yml + funding.json)
    "github_sponsors", "funding_sources", "funding_class",
    # issues — debt + trend
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio",
    "slope_opened", "slope_closed", "issue_trend_score",
    "issue_trend", "issue_debt_class",
    # rollup (worst across populated dimensions)
    "risk_class",
]

CLASS_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


def concentration_class(bus_factor: int | None, hhi: int | None) -> str:
    """Classify repo concentration risk as A/B/C/D. Empty if either input missing.

    A (critical):  BF=1  and HHI >= 8000 — single maintainer, extreme concentration
    B (high risk): BF<=2 and HHI >= 5000 — tiny core, high concentration
    C (moderate):  BF<=4 and HHI >= 2500 — small team, moderate concentration
    D (healthy):   everything else        — distributed enough
    """
    if bus_factor is None or hhi is None:
        return ""
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


def security_class(openssf_score: float | None) -> str:
    """Classify based on the OpenSSF Scorecard score (0-10).

    A (critical):  score <= 3 — severe security hygiene gaps
    B (high risk): score <= 5
    C (moderate):  score <= 7
    D (healthy):   score >  7
    "":            no scorecard data available
    """
    if openssf_score is None:
        return ""
    if openssf_score <= SECURITY_THRESHOLDS["A"]:
        return "A"
    if openssf_score <= SECURITY_THRESHOLDS["B"]:
        return "B"
    if openssf_score <= SECURITY_THRESHOLDS["C"]:
        return "C"
    return "D"


def rollup_risk_class(*classes: str) -> str:
    """Cross-dimension rollup: worst (lowest letter) across populated classes.

    A < B < C < D. Empty inputs are ignored. Empty result if no input is populated.
    A class-A on ANY dimension makes the repo class-A overall — risk is the
    worst-case across dimensions, not an average.
    """
    populated = [c for c in classes if c]
    if not populated:
        return ""
    return min(populated, key=lambda c: CLASS_RANK[c])


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


def _read_openssf_scores() -> dict[str, float]:
    """Return {repo_lowercased: score} from data/openssf/scores.csv."""
    if not OPENSSF_FILE.exists():
        return {}
    out: dict[str, float] = {}
    with open(OPENSSF_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            repo = (r.get("repo") or "").strip().lower()
            score = (r.get("score") or "").strip()
            if not repo or not score:
                continue
            try:
                out[repo] = float(score)
            except ValueError:
                continue
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


def _load_repo_created_at() -> dict[str, str]:
    """Load {repo_lowercased: created_at} from data/github/repos.csv.

    `created_at` is the GitHub repo creation timestamp (ISO 8601 UTC).
    Empty if the file is missing or the repo isn't in it.
    """
    out: dict[str, str] = {}
    if not GH_REPOS_FILE.exists():
        return out
    with open(GH_REPOS_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            slug = (r.get("repo") or "").strip().lower()
            ts = (r.get("created_at") or "").strip()
            if slug and ts:
                out[slug] = ts
    return out


def _load_funding_data() -> dict[str, dict[str, str]]:
    """Load per-repo funding signals from data/funding-data.csv.

    Returns {repo_lowercased: {github_sponsors, funding_sources, funding_class}}.
    Missing repos => empty class downstream.
    """
    out: dict[str, dict[str, str]] = {}
    if not FUNDING_FILE.exists():
        return out
    with open(FUNDING_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            if not repo:
                continue
            out[repo] = {
                "github_sponsors": (row.get("github_sponsors") or "").strip(),
                "funding_sources": (row.get("funding_sources") or "").strip(),
                "funding_class": (row.get("funding_class") or "").strip(),
            }
    return out


def _load_concentration_data() -> dict[str, dict[str, str]]:
    """Load per-repo concentration metrics from data/concentration-data.csv.

    Returns {repo_lowercased: {bus_factor, hhi, total_commits, total_contributors}}.
    Single source of truth for BF/HHI now that the contributor fetcher uses
    /contributors lifetime data and writes one row per repo here. The older
    per-year wide CSVs at data/github/contributors/ are no longer read.
    """
    out: dict[str, dict[str, str]] = {}
    if not CONCENTRATION_FILE.exists():
        return out
    with open(CONCENTRATION_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            repo = (row.get("repo") or "").strip().lower()
            if not repo:
                continue
            out[repo] = {
                "bus_factor": (row.get("bus_factor") or "").strip(),
                "hhi": (row.get("hhi") or "").strip(),
                "total_commits": (row.get("total_commits") or "").strip(),
                "total_contributors": (row.get("total_contributors") or "").strip(),
            }
    return out


def aggregate() -> tuple[list[dict], dict[str, int]]:
    """Read all dimensions and return one row per **eligible** repo.

    Iterates over the full eligible universe — a missing dimension just
    leaves that class column empty rather than dropping the repo. The
    rollup `risk_class` is the worst class across whatever dimensions
    we did manage to populate.

    Returns (rows, coverage_counts) where `coverage_counts` reports how
    many of the eligible repos got each dimension classified.
    """
    repo_ids = _load_repo_ids()
    eligible = sorted(repo_ids.keys())
    repo_locs = _load_locs()

    concentration_by_repo = _load_concentration_data()
    funding_by_repo = _load_funding_data()
    created_at_by_repo = _load_repo_created_at()

    opened_by_repo = _read_issues_per_year("opened.csv")
    closed_by_repo = _read_issues_per_year("closed.csv")
    openssf_by_repo = _read_openssf_scores()

    coverage = {
        "concentration": 0, "complexity": 0, "security": 0,
        "funding": 0, "issue_debt": 0, "issue_trend": 0, "any": 0,
    }
    rows: list[dict] = []

    for repo in eligible:
        conc_row = concentration_by_repo.get(repo, {})
        bf_str = conc_row.get("bus_factor", "")
        hhi_str = conc_row.get("hhi", "")
        total_commits_str = conc_row.get("total_commits", "")
        total_contribs_str = conc_row.get("total_contributors", "")
        bf = int(bf_str) if bf_str else None
        hhi = int(hhi_str) if hhi_str else None

        # Concentration only fires when both BF and HHI are present
        conc_cls = concentration_class(bf, hhi) if (bf is not None and hhi is not None) else ""

        locs = repo_locs.get(repo)
        comp_cls = complexity_class(locs)

        score = openssf_by_repo.get(repo)
        sec_cls = security_class(score)

        funding_row = funding_by_repo.get(repo, {})
        github_sponsors = funding_row.get("github_sponsors", "")
        funding_sources = funding_row.get("funding_sources", "")
        fund_cls = funding_row.get("funding_class", "")

        opened = opened_by_repo.get(repo, {})
        closed = closed_by_repo.get(repo, {})
        opened_5y = sum(opened.values())
        closed_5y = sum(closed.values())
        close_ratio = (closed_5y / opened_5y) if opened_5y > 0 else 0.0
        s_open, s_close, trend_score, trend_label = issue_trend(opened, closed)
        debt_cls = issue_debt_class(opened_5y, close_ratio)

        risk_cls = rollup_risk_class(conc_cls, comp_cls, sec_cls, fund_cls, debt_cls)

        if conc_cls: coverage["concentration"] += 1
        if comp_cls: coverage["complexity"] += 1
        if sec_cls:  coverage["security"] += 1
        if fund_cls: coverage["funding"] += 1
        if debt_cls: coverage["issue_debt"] += 1
        if trend_label: coverage["issue_trend"] += 1
        if risk_cls: coverage["any"] += 1

        rows.append({
            "repo": repo,
            "repo_id": repo_ids.get(repo, ""),
            "created_at": created_at_by_repo.get(repo, ""),
            "total_commits": total_commits_str,
            "total_contributors": total_contribs_str,
            "hhi_commits": hhi if hhi is not None else "",
            "bf_commits": bf if bf is not None else "",
            "concentration_class": conc_cls,
            "loc": locs if locs is not None else "",
            "complexity_class": comp_cls,
            "openssf_score": score if score is not None else "",
            "security_class": sec_cls,
            "github_sponsors": github_sponsors,
            "funding_sources": funding_sources,
            "funding_class": fund_cls,
            "issues_opened_5y": opened_5y,
            "issues_closed_5y": closed_5y,
            "issue_close_ratio": (round(close_ratio, 3) if opened_5y >= 1 else ""),
            "slope_opened": (round(s_open, 2) if opened_5y >= 1 else ""),
            "slope_closed": (round(s_close, 2) if opened_5y >= 1 else ""),
            "issue_trend_score": (round(trend_score, 4) if trend_label else ""),
            "issue_trend": trend_label,
            "issue_debt_class": debt_cls,
            "risk_class": risk_cls,
        })

    return rows, coverage


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

SECURITY_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

SECURITY_CRITERIA = {
    "A": "score ≤ 3",
    "B": "score ≤ 5",
    "C": "score ≤ 7",
    "D": "score > 7",
}

FUNDING_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

FUNDING_CRITERIA = {
    "A": "0 sources, 0 sponsors",
    "B": "1 src OR ≤5 sponsors",
    "C": "2-3 src OR 6-50 spr",
    "D": "≥4 src OR ≥50 spr OR funding.json",
}

ROLLUP_LABELS = {
    "A": ("critical", "red"),
    "B": ("high risk", "yellow"),
    "C": ("moderate", "cyan"),
    "D": ("healthy", "green"),
}

ROLLUP_CRITERIA = {
    "A": "any dim = A",
    "B": "any dim ≥ B (no A)",
    "C": "any dim ≥ C (no A/B)",
    "D": "all dims = D",
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


def _print_coverage(coverage: dict[str, int], total: int) -> None:
    """Per-dimension coverage table — how many of the eligible repos got
    each class column populated."""
    table = Table(title="[bold]Dimension coverage[/bold] (out of eligible)",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Dimension", style="bold")
    table.add_column("Classified", justify="right")
    table.add_column("Coverage", justify="right")
    for dim in ("concentration", "complexity", "security", "funding",
                "issue_debt", "issue_trend", "any"):
        n = coverage.get(dim, 0)
        pct = 100 * n / total if total else 0
        style = "bold" if dim == "any" else ""
        table.add_row(f"[{style}]{dim}[/{style}]" if style else dim,
                      f"{n:,}", f"{pct:.1f}%")
    console.print(table)


def main():
    console.print("[bold]Aggregating risk metrics...[/bold]\n")
    rows, coverage = aggregate()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    _print_coverage(coverage, total)
    console.print()
    _print_class_table("Concentration", rows, "concentration_class",
                       CONCENTRATION_LABELS, CONCENTRATION_CRITERIA)
    console.print()
    _print_class_table("Complexity", rows, "complexity_class",
                       COMPLEXITY_LABELS, COMPLEXITY_CRITERIA)
    console.print()
    _print_class_table("Security (OpenSSF)", rows, "security_class",
                       SECURITY_LABELS, SECURITY_CRITERIA)
    console.print()
    _print_class_table("Funding", rows, "funding_class",
                       FUNDING_LABELS, FUNDING_CRITERIA)
    console.print()
    _print_class_table("Issue debt", rows, "issue_debt_class",
                       ISSUE_DEBT_LABELS, ISSUE_DEBT_CRITERIA)
    console.print()
    _print_trend_table(rows)
    console.print()
    _print_class_table("[bold]Risk class[/bold] (worst across dimensions)",
                       rows, "risk_class",
                       ROLLUP_LABELS, ROLLUP_CRITERIA)

    console.print(f"\n[dim]Written {total:,} eligible repos → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
