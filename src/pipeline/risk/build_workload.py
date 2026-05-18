#!/usr/bin/env python3
"""Build data/workload.csv — maintainer-workload metrics per risk-scope repo.

Reads:
    data/value-data.csv                                 — A/B value-class set
    data/github/repos.csv                               — created_at, has_issues, pushed_at
    data/github/contributors/contributors.csv           — wide per-year + 2021-2025 (lifetime aggregate)
    data/github/git/commits-years.csv                   — per (repo, year) commits
    data/openssf/checks.csv                             — per-check Scorecard scores
    data/github/issues.csv                              — long: repo, repo_id, year, metric, value
                                                          (metric ∈ {opened_issues, closed_issues})

Writes:
    data/workload.csv  with columns:
        repo, repo_id,
        repo_age_years,                  ([2025 EOY])
        repo_age_years_2025_eoy,         (years between created_at and 2025-12-31)
        active_maintainers_lifetime,     (lifetime distinct contributors — see note)
        openssf_maintained,              (Scorecard "Maintained" sub-check, 0-10 or "")
        has_issues,                      (bool from GH /repos)
        push_cadence_years,              (count of years 2021-2025 with ≥1 commit, 0-5)
        pushed_at,                       (ISO 8601, GH API)
        issues_opened_5y,
        issues_closed_5y,
        issue_close_ratio,               (closed_5y / opened_5y, 3 dp)
        slope_opened,                    (OLS slope of yearly opened, 2 dp)
        slope_closed,
        issue_trend_score,               (vol-normalised slope_closed - slope_opened)
        fetched_at

Periods:
    repo_age_years_2025_eoy: years between created_at and 2025-12-31.
    push_cadence_years, issues_*: 2021-2025 window.
    active_maintainers_lifetime: lifetime distinct contributors. Roadmap
      target was [2021–2025] but the contributors fetcher uses
      /contributors (lifetime only), not /stats/contributors (per-year,
      202-pathology). For repos created post-2020 lifetime ≈ 5y.
      Documented gap.

Usage:
    uv run python -m src.pipeline.risk.build_workload
"""

import csv
import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.pipeline.common.repos import load_risk_repos

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
REPOS_FILE = DATA_DIR / "github" / "repos.csv"
CONTRIB_FILE = DATA_DIR / "github" / "contributors" / "contributors.csv"
COMMITS_YEARS_FILE = DATA_DIR / "github" / "git" / "commits-years.csv"
OPENSSF_CHECKS_FILE = DATA_DIR / "openssf" / "checks.csv"
ISSUES_FILE = DATA_DIR / "github" / "issues.csv"
OUTPUT_FILE = DATA_DIR / "workload.csv"

YEARS = list(range(2021, 2026))  # 2021..2025
EOY_2025 = datetime.date(2025, 12, 31)

FIELDS = [
    "repo", "repo_id",
    "repo_age_years_2025_eoy",
    "active_maintainers_lifetime",
    "openssf_maintained",
    "has_issues",
    "push_cadence_years", "pushed_at",
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio",
    "slope_opened", "slope_closed", "issue_trend_score",
    "fetched_at",
]


def _hazen_percentiles(values: list[float]) -> list[float]:
    """Percentile-rank each value via the Hazen plotting position.

    pct = 100 * (rank - 0.5) / n, with tied values sharing the average of
    their ranks. The result is strictly within (0, 100) — never exactly 0
    or 100 — so a geometric mean taken over these percentiles cannot
    collapse to 0. Higher value → higher percentile. Empty input → [].
    """
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    pctls = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, tie-averaged
        pct = 100.0 * (avg_rank - 0.5) / n
        for k in range(i, j + 1):
            pctls[indexed[k][0]] = pct
        i = j + 1
    return pctls


def _geometric_mean(values: list[float]) -> float:
    """Geometric mean (∏ v)^(1/n). Assumes every value > 0; [] → 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def _quartile_classes(scores: list[float]) -> list[str]:
    """Assign A/B/C/D by equal-count quartiles of `scores` (higher = worse).

    Sorted descending, the highest-scoring 25% get 'A', then 'B', 'C', 'D'.
    When n is not divisible by 4 each class holds ⌊n/4⌋ or ⌈n/4⌉ members.
    Empty input → [].
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    labels = ["A", "B", "C", "D"]
    out = [""] * n
    for p, idx in enumerate(order):  # p: 0-based rank, 0 = highest score
        out[idx] = labels[min(3, p * 4 // n)]
    return out


def compute_workload_classes(metrics: list[dict]) -> dict[str, dict]:
    """Compute per-maintainer burden ratios, percentiles, and the class.

    `metrics` — one dict per repo with keys `repo`, `loc`, `cve`, `nni`,
    `ac`. `loc`/`cve`/`ac` are floats or None (None = the underlying
    metric is missing); `nni` is always a float and may be negative.

    Returns {repo: {...}} with these keys per repo:
        loc_per_ac, cve_per_ac, nni_per_ac,
        loc_per_ac_pctl, cve_per_ac_pctl, nni_per_ac_pctl,
        workload_burden_percentile, workload_class
    A repo is classified only when loc, cve, nni, and ac are all present
    AND ac > 0; otherwise every value is the empty string "".
    """
    keys = ("loc_per_ac", "cve_per_ac", "nni_per_ac",
            "loc_per_ac_pctl", "cve_per_ac_pctl", "nni_per_ac_pctl",
            "workload_burden_percentile", "workload_class")
    out: dict[str, dict] = {m["repo"]: {k: "" for k in keys} for m in metrics}

    # 1. Keep only repos with all four inputs present and ac > 0.
    classifiable: list[dict] = []
    for m in metrics:
        loc, cve, nni, ac = m["loc"], m["cve"], m["nni"], m["ac"]
        if loc is None or cve is None or nni is None or ac is None or ac <= 0:
            continue
        classifiable.append({
            "repo": m["repo"],
            "loc_per_ac": loc / ac,
            "cve_per_ac": cve / ac,
            "nni_per_ac": nni / ac,
        })
    if not classifiable:
        return out

    # 2. Hazen-percentile each ratio across the classifiable set.
    loc_p = _hazen_percentiles([c["loc_per_ac"] for c in classifiable])
    cve_p = _hazen_percentiles([c["cve_per_ac"] for c in classifiable])
    nni_p = _hazen_percentiles([c["nni_per_ac"] for c in classifiable])

    # 3. Geometric mean of the three percentiles → burden score.
    burden = [_geometric_mean([loc_p[i], cve_p[i], nni_p[i]])
              for i in range(len(classifiable))]

    # 4. Equal-count quartile class (A = highest burden).
    classes = _quartile_classes(burden)

    # 5. Emit.
    for i, c in enumerate(classifiable):
        out[c["repo"]] = {
            "loc_per_ac": round(c["loc_per_ac"], 4),
            "cve_per_ac": round(c["cve_per_ac"], 4),
            "nni_per_ac": round(c["nni_per_ac"], 4),
            "loc_per_ac_pctl": round(loc_p[i], 2),
            "cve_per_ac_pctl": round(cve_p[i], 2),
            "nni_per_ac_pctl": round(nni_p[i], 2),
            "workload_burden_percentile": round(burden[i], 2),
            "workload_class": classes[i],
        }
    return out


def _load_repo_meta() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not REPOS_FILE.exists():
        return out
    with open(REPOS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            out[slug] = row
    return out


def _load_wide_year(path: Path, year_col: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            v = (row.get(year_col) or "").strip()
            if v:
                out[slug] = v
    return out


def _load_commits_years() -> dict[str, dict[int, int]]:
    """Return {repo: {year: commits}}."""
    out: dict[str, dict[int, int]] = {}
    if not COMMITS_YEARS_FILE.exists():
        return out
    with open(COMMITS_YEARS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
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
            slug = (row.get("repo") or "").strip().lower()
            if not slug:
                continue
            v = (row.get("Maintained") or "").strip()
            if v != "":
                out[slug] = v
    return out


def _load_issues_long(path: Path) -> dict[str, dict[str, dict[int, int]]]:
    """Project long-format issues.csv → wide-by-metric for the build's use.

    Returns {metric: {repo: {year: count}}} where metric ∈ {opened_issues,
    closed_issues}. Years missing from the file default to 0 in the inner
    dict (matches the previous wide-loader behaviour where blank cells
    became 0). Unknown metrics are ignored.
    """
    METRICS = ("opened_issues", "closed_issues")
    out: dict[str, dict[str, dict[int, int]]] = {m: {} for m in METRICS}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = (row.get("repo") or "").strip().lower()
            metric = (row.get("metric") or "").strip()
            year_s = (row.get("year") or "").strip()
            value = (row.get("value") or "").strip()
            if not slug or metric not in METRICS or not year_s or value == "":
                continue
            try:
                y = int(year_s)
                v = int(value)
            except ValueError:
                continue
            out[metric].setdefault(slug, {})[y] = v
    # Backfill missing years with 0 to mirror the old wide-loader's behaviour:
    # `int(row.get(str(y)) or 0)` made blank-or-missing cells equal to 0, and
    # downstream sums/slopes treat 0 the same way. Doing this here keeps the
    # build code below identical to the original.
    for metric in METRICS:
        for repo, year_map in out[metric].items():
            for y in YEARS:
                year_map.setdefault(y, 0)
    return out


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
    days = (EOY_2025 - d).days
    if days < 0:
        return "0"
    return f"{days / 365.25:.1f}"


def build() -> list[dict]:
    eligible = load_risk_repos()

    repos = _load_repo_meta()
    # Despite the column name "2021-2025", the contributors fetcher writes
    # lifetime counts here (see module docstring).
    contribs_lifetime = _load_wide_year(CONTRIB_FILE, "2021-2025")
    commits_years = _load_commits_years()
    maintained = _load_openssf_maintained()
    issues = _load_issues_long(ISSUES_FILE)
    opened = issues["opened_issues"]
    closed = issues["closed_issues"]

    rows: list[dict] = []
    for entry in eligible:
        repo = entry.repo
        meta = repos.get(repo, {})

        # Age
        created_at = (meta.get("created_at") or "").strip()
        age = _repo_age_years(created_at)

        # has_issues
        hi_raw = (meta.get("has_issues") or "").strip()
        has_issues = hi_raw if hi_raw else ""

        # Push cadence: years with ≥1 commit in 2021-2025
        cy = commits_years.get(repo, {})
        cadence = sum(1 for y in YEARS if cy.get(y, 0) > 0) if cy else ""
        if cadence == "":
            cadence_val = ""
        else:
            cadence_val = str(cadence)

        # OpenSSF maintained
        openssf_maintained = maintained.get(repo, "")

        # Issues
        op = opened.get(repo, {})
        cl = closed.get(repo, {})
        op_5y = sum(op.values())
        cl_5y = sum(cl.values())
        ratio = round(cl_5y / op_5y, 3) if op_5y > 0 else ""

        op_vals = [op.get(y, 0) for y in YEARS]
        cl_vals = [cl.get(y, 0) for y in YEARS]
        s_open = _ols_slope(YEARS, op_vals)
        s_close = _ols_slope(YEARS, cl_vals)
        mean_op = op_5y / len(YEARS) if op_5y else 0
        if mean_op >= 1:
            trend_score = round((s_close - s_open) / mean_op, 4)
        else:
            trend_score = ""

        rows.append({
            "repo": repo,
            "repo_id": entry.repo_id,
            "repo_age_years_2025_eoy": age,
            "active_maintainers_lifetime": contribs_lifetime.get(repo, ""),
            "openssf_maintained": openssf_maintained,
            "has_issues": has_issues,
            "push_cadence_years": cadence_val,
            "pushed_at": (meta.get("pushed_at") or "").strip(),
            "issues_opened_5y": op_5y,
            "issues_closed_5y": cl_5y,
            "issue_close_ratio": ratio,
            "slope_opened": (round(s_open, 2) if op_5y >= 1 else ""),
            "slope_closed": (round(s_close, 2) if op_5y >= 1 else ""),
            "issue_trend_score": trend_score,
            "fetched_at": (meta.get("fetched_at") or "").strip(),
        })
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
        "repo_age_years_2025_eoy", "active_maintainers_lifetime",
        "openssf_maintained",
        "has_issues", "push_cadence_years", "pushed_at",
        "issue_close_ratio", "issue_trend_score",
    ):
        n = sum(1 for r in rows if r[col] not in ("", None))
        pct = 100 * n / total if total else 0
        table.add_row(col, f"{n:,}", f"{pct:.1f}%")
    console.print(table)
    console.print(f"\n[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")


if __name__ == "__main__":
    main()
