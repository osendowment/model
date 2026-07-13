#!/usr/bin/env python3
"""Build data/risk/workload.csv — maintainer-workload metrics per risk-scope repo.

Reads:
    data/value/value.csv                                 — A/B value-class set
    data/sources/github/repos.csv                               — created_at, has_issues, pushed_at
    data/sources/git/commits-years.csv                   — per (repo, year) commits
    data/sources/openssf/checks.csv                             — per-check Scorecard scores
    data/sources/github/issues.csv                              — long: repo, repo_id, year, metric, value
                                                          (metric ∈ {opened_issues, closed_issues})
    data/risk/complexity.csv                                 — loc_eoy per repo
    data/risk/security.csv                                   — cve_count_5y per repo
    data/risk/concentration.csv                              — active_contributors_git_5y per repo

Writes:
    data/risk/workload.csv  with columns:
        repo, repo_id,
        repo_age_years,         (years between created_at and EOY of the last complete year)
        active_contributors_git_5y,  (windowed AC — from concentration.csv)
        dormant,                         (1 if no real active contributors in the
                                          window — AC was 0, so AC=1 was assumed
                                          for the per-AC ratios below; else 0)
        openssf_maintained,              (Scorecard "Maintained" sub-check, 0-10 or "")
        has_issues,                      (bool from GH /repos)
        push_cadence_years,              (count of window years with ≥1 commit, 0..len(years))
        pushed_at,                       (ISO 8601, GH API)
        issues_opened_5y,
        issues_closed_5y,
        issue_close_ratio,               (closed_5y / opened_5y, 3 dp)
        net_new_issues_5y,               (opened_5y − closed_5y)
        slope_opened,                    (OLS slope of yearly opened, 2 dp)
        slope_closed,
        issue_trend_score,               (vol-normalised slope_closed - slope_opened)
        loc_per_ac,                      (loc_eoy / AC; AC=1 when dormant)
        cve_per_ac,                      (cve_count_5y / AC; AC=1 when dormant)
        nni_per_ac,                      (net_new_issues_5y / AC; AC=1 when dormant)
        loc_per_ac_p,                    (risk percentile of loc_per_ac)
        cve_per_ac_p,
        nni_per_ac_p,
        issue_close_ratio_p,
        issue_trend_score_p,
        score,                           (geometric mean of the three per-AC percentiles)
        fetched_at

Notes:
    score is the geometric mean of loc_per_ac_p, cve_per_ac_p, nni_per_ac_p. A
    repo with no fetched issues has a blank nni_per_ac; its nni_per_ac_p is
    neutral-filled to 50 so loc + cve still produce a score (see build()).
    Repos with zero active contributors (`dormant = 1`) are scored with AC=1
    rather than left blank, so every top repo gets a workload score.
    build_workload must run after build_complexity, build_security,
    and build_concentration.

    Issue figures require EVERY window year present for BOTH opened_issues
    and closed_issues (`issues_fetched` in build()) — a repo missing even one
    year (fetch failure or not-yet-attempted) gets every issue-derived column
    blank rather than treating the missing year as a confirmed 0. This keeps
    a fetch gap from silently corrupting net_new_issues_5y (feeds the SCORED
    nni_per_ac), issue_close_ratio, and the OLS slopes.

Periods (the window + EOY anchor come from settings.json `years`):
    repo_age_years: years between created_at and EOY of LAST_COMPLETE_YEAR.
    push_cadence_years, issues_*: the settings `years` window.
    active_contributors_git_5y: distinct non-bot contributors who authored a
      commit in the window, from the git-clone method (the windowed AC
      concentration.csv now provides — GitHub's /contributors API cannot
      window, so the earlier lifetime-AC fallback is retired).

Usage:
    uv run python -m src.risk.build_workload
"""

import csv
import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.params import LAST_COMPLETE_YEAR, YEARS
from src.common.percentiles import add_percentiles
from src.common.repos import load_top_repos
from src.common.tables import load_column_by_id, load_rows_by_id

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
COMMITS_YEARS_FILE = DATA_DIR / "sources" / "git" / "commits-years.csv"
OPENSSF_CHECKS_FILE = DATA_DIR / "sources" / "openssf" / "checks.csv"
# Issue metrics come from two host-specific fetchers writing the same
# long-format contract; the loader merges them (repo_id-keyed, disjoint sets).
ISSUES_FILES = (DATA_DIR / "sources" / "github" / "issues.csv",
                DATA_DIR / "sources" / "gitlab" / "issues.csv")
OUTPUT_FILE = DATA_DIR / "risk" / "workload.csv"
COMPLEXITY_FILE = DATA_DIR / "risk" / "complexity.csv"
SECURITY_FILE = DATA_DIR / "risk" / "security.csv"
CONCENTRATION_FILE = DATA_DIR / "risk" / "concentration.csv"

# Risk-stage window + EOY snapshot anchor — both from settings (src/settings.json).
EOY = datetime.date(LAST_COMPLETE_YEAR, 12, 31)

FIELDS = [
    "repo", "repo_id",
    "repo_age_years",
    "active_contributors_git_5y",
    "dormant",
    "openssf_maintained",
    "has_issues",
    "push_cadence_years", "pushed_at",
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio", "issue_close_ratio_p",
    "net_new_issues_5y",
    "slope_opened", "slope_closed", "issue_trend_score", "issue_trend_score_p",
    "loc_per_ac", "loc_per_ac_p",
    "cve_per_ac", "cve_per_ac_p",
    "nni_per_ac", "nni_per_ac_p",
    "score",
    "fetched_at",
]


def _load_commits_years() -> dict[str, dict[int, int]]:
    """Return {repo: {year: commits}}."""
    out: dict[str, dict[int, int]] = {}
    if not COMMITS_YEARS_FILE.exists():
        return out
    with open(COMMITS_YEARS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo_id") or "").strip()   # join on stable id, not name
            if not slug:
                continue
            try:
                y = int((row.get("year") or "").strip())
                c = int((row.get("commits") or "0").strip())
            except ValueError:
                continue
            out.setdefault(slug, {})[y] = c
    return out


def _load_openssf_maintained() -> dict[str, str]:
    out: dict[str, str] = {}
    if not OPENSSF_CHECKS_FILE.exists():
        return out
    with open(OPENSSF_CHECKS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo_id") or "").strip()   # join on stable id, not name
            if not slug:
                continue
            v = (row.get("Maintained") or "").strip()
            if v != "":
                out[slug] = v
    return out


def _load_issues_long(*paths: Path) -> dict[str, dict[str, dict[int, int]]]:
    """Project long-format issues.csv files → wide-by-metric for the build's use.

    Returns {metric: {repo_id: {year: count}}} where metric ∈ {opened_issues,
    closed_issues}. A year is present ONLY if it was actually fetched — no
    backfilling missing years to 0. `fetch_issue_metrics.py` only ever writes
    a row for a cell it successfully fetched; a cell that failed (network
    error, after retries) is simply absent, identical in shape to a cell that
    was never attempted. Treating "absent" as "genuinely 0" would silently
    let a fetch failure masquerade as a confirmed-zero year — `build()`'s
    `issues_fetched` gate below requires every window year to be present
    before trusting any issue figure derived from this data. Unknown metrics
    are ignored.
    """
    out: dict[str, dict[str, dict[int, int]]] = {m: {} for m in _ISSUE_METRICS}
    for path in paths:
        _load_one_issues_file(path, out)
    return out


_ISSUE_METRICS = ("opened_issues", "closed_issues")


def _load_one_issues_file(path: Path, out: dict) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo_id") or "").strip()   # join on stable id, not name
            metric = (row.get("metric") or "").strip()
            year_s = (row.get("year") or "").strip()
            value = (row.get("value") or "").strip()
            if not slug or metric not in _ISSUE_METRICS or not year_s or value == "":
                continue
            try:
                y = int(year_s)
                v = int(value)
            except ValueError:
                continue
            out[metric].setdefault(slug, {})[y] = v


def _num(value: str) -> float | None:
    """Parse a CSV cell to float. Empty / unparseable → None."""
    s = (value or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ols_slope(years: list[int], values: list[int]) -> float:
    """OLS slope. Returns 0 for series shorter than 2 or with no year variance."""
    n = len(years)
    if n < 2:
        return 0.0
    mean_y = sum(years) / n
    mean_v = sum(values) / n
    var_y = sum((y - mean_y) ** 2 for y in years)
    if var_y == 0:
        return 0.0
    cov = sum((y - mean_y) * (v - mean_v) for y, v in zip(years, values))
    return cov / var_y


def _repo_age_years(created_at_iso: str) -> str:
    """Years (1 dp) between repo creation and 2025-12-31. Empty if invalid."""
    if not created_at_iso:
        return ""
    try:
        # GH timestamps are ISO 8601 UTC, e.g. "2018-03-15T08:42:11Z"
        d = datetime.datetime.fromisoformat(created_at_iso.replace("Z", "+00:00")).date()
    except ValueError:
        return ""
    days = (EOY - d).days
    if days < 0:
        return "0"
    return f"{days / 365.25:.1f}"


def build() -> list[dict]:
    eligible = load_top_repos()

    # All these join on the stable repo_id (below), so a rename never drops data.
    repos = load_rows_by_id(REPOS_FILE)
    commits_years = _load_commits_years()
    maintained = _load_openssf_maintained()
    issues = _load_issues_long(*ISSUES_FILES)
    opened = issues["opened_issues"]
    closed = issues["closed_issues"]

    # Cross-dimension inputs for the workload class.
    loc_by_repo = load_column_by_id(COMPLEXITY_FILE, "loc_eoy")
    cve_by_repo = load_column_by_id(SECURITY_FILE, "cve_count_5y")
    ac_by_repo = load_column_by_id(CONCENTRATION_FILE, "active_contributors_git_5y")

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        rid = str(entry.repo_id)
        meta = repos.get(rid, {})

        # Age
        created_at = (meta.get("created_at") or "").strip()
        age = _repo_age_years(created_at)

        # has_issues
        hi_raw = (meta.get("has_issues") or "").strip()
        has_issues = hi_raw if hi_raw else ""

        # Push cadence: years with ≥1 commit in 2021-2025
        cy = commits_years.get(rid, {})
        cadence = sum(1 for y in YEARS if cy.get(y, 0) > 0) if cy else ""
        cadence_val = str(cadence) if cadence != "" else ""

        # OpenSSF maintained
        openssf_maintained = maintained.get(rid, "")

        # Issues. `issues_fetched` requires EVERY window year to be present for
        # BOTH opened and closed — a repo with even one missing year (a fetch
        # that failed, or was never attempted) stays entirely blank rather than
        # having that one year treated as a confirmed 0. Partial coverage
        # silently corrupts net_new_issues_5y (feeds the SCORED nni_per_ac
        # metric), issue_close_ratio, and both OLS slopes if a missing year is
        # ever mistaken for a real zero — so this is an all-or-nothing gate.
        op = opened.get(rid, {})
        cl = closed.get(rid, {})
        issues_fetched = all(y in op for y in YEARS) and all(y in cl for y in YEARS)
        if issues_fetched:
            op_vals = [op[y] for y in YEARS]
            cl_vals = [cl[y] for y in YEARS]
            op_5y = sum(op_vals)
            cl_5y = sum(cl_vals)
            ratio = round(cl_5y / op_5y, 3) if op_5y > 0 else ""
            net_new_issues = op_5y - cl_5y

            s_open = _ols_slope(YEARS, op_vals)
            s_close = _ols_slope(YEARS, cl_vals)
            mean_op = op_5y / len(YEARS) if op_5y else 0
            trend_score = round((s_close - s_open) / mean_op, 4) if mean_op >= 1 else ""
            slope_opened_out = round(s_open, 2) if op_5y >= 1 else ""
            slope_closed_out = round(s_close, 2) if op_5y >= 1 else ""
        else:
            op_5y = cl_5y = net_new_issues = ""
            ratio = trend_score = slope_opened_out = slope_closed_out = ""

        ac_raw = ac_by_repo.get(rid, "")
        ac_f = _num(ac_raw)
        loc_f = _num(loc_by_repo.get(rid, ""))
        cve_f = _num(cve_by_repo.get(rid, ""))
        # A repo with zero active contributors in the window (dormant / bot-only)
        # has no real maintainer to divide the burden across; rather than leave
        # it unscored, attribute the whole burden to a single notional maintainer
        # (AC = 1) and flag it `dormant = 1`. ac_f is None only if the AC fetch is
        # genuinely absent (never in scope, since concentration is 100%) — then
        # leave the ratios blank and dormant unknown.
        if ac_f == 0:
            dormant, ac_eff = "1", 1.0
        elif ac_f and ac_f > 0:
            dormant, ac_eff = "0", ac_f
        else:
            dormant, ac_eff = "", None
        if ac_eff:
            row_loc_per_ac = round(loc_f / ac_eff, 4) if loc_f is not None else ""
            row_cve_per_ac = round(cve_f / ac_eff, 4) if cve_f is not None else ""
            row_nni_per_ac = round(net_new_issues / ac_eff, 4) if issues_fetched else ""
        else:
            row_loc_per_ac = row_cve_per_ac = row_nni_per_ac = ""

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "repo_age_years": age,
            "active_contributors_git_5y": ac_raw,
            "dormant": dormant,
            "openssf_maintained": openssf_maintained,
            "has_issues": has_issues,
            "push_cadence_years": cadence_val,
            "pushed_at": (meta.get("pushed_at") or "").strip(),
            "issues_opened_5y": op_5y,
            "issues_closed_5y": cl_5y,
            "issue_close_ratio": ratio,
            "net_new_issues_5y": net_new_issues,
            "slope_opened": slope_opened_out,
            "slope_closed": slope_closed_out,
            "issue_trend_score": trend_score,
            "loc_per_ac": row_loc_per_ac,
            "cve_per_ac": row_cve_per_ac,
            "nni_per_ac": row_nni_per_ac,
            "fetched_at": (meta.get("fetched_at") or "").strip(),
        })

    add_percentiles(
        rows,
        pctl_specs=[
            ("loc_per_ac", True), ("cve_per_ac", True), ("nni_per_ac", True),
            ("issue_close_ratio", False), ("issue_trend_score", False),
        ],
        composite_cols=["loc_per_ac_p", "cve_per_ac_p", "nni_per_ac_p"],
        dim_col="score",
        # A repo whose issues were never fetched has a blank nni_per_ac. When its
        # LOC and CVE burdens are both present, treat the unknown issue-backlog
        # burden as neutral (median percentile 50) so the row still scores; if
        # LOC or CVE is also missing the row stays blank (no lone 50). This
        # covers ANY repo with unfetched issues — GitLab repos are fetched too,
        # by src.sources.gitlab.fetch_issue_metrics, and merged in by repo_id.
        neutral_fill={"nni_per_ac_p": 50},
        # Percentiles rank the whole top-repo population (github + gitlab
        # together); platform does not matter.
    )
    return rows


def main() -> None:
    console.print("[bold]Building workload.csv...[/bold]\n")
    rows = build()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    table = Table(title="[bold]Workload coverage[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Field", style="bold")
    table.add_column("Populated", justify="right")
    table.add_column("Coverage", justify="right")
    for col in (
        "repo_age_years", "active_contributors_git_5y",
        "openssf_maintained", "has_issues", "push_cadence_years", "pushed_at",
        "issue_close_ratio", "net_new_issues_5y", "issue_trend_score",
        "loc_per_ac", "cve_per_ac", "nni_per_ac",
        "score",
    ):
        n = sum(1 for r in rows if r[col] not in ("", None))
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)

    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
