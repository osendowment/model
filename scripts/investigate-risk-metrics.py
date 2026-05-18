#!/usr/bin/env python3
"""One-off: investigate largest / smallest / missing risk metrics.

For every metric column in data/risk-data.csv, report the top-5 and
bottom-5 repos by value, the missing count, and flag anomalies (negative
values where they shouldn't be, zero where suspicious, duplicate columns).

    uv run python scripts/investigate-risk-metrics.py
"""

import csv
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
RISK = DATA / "risk-data.csv"

# Columns that should hold a number; everything else is identity/text/bool.
NUMERIC = {
    "total_commits_lifetime", "total_contributors_lifetime",
    "active_contributors", "hhi_commits_lifetime", "bf_commits_lifetime",
    "loc_2025_eoy", "sloc_2025_eoy", "scc_complexity_2025_eoy",
    "scc_density_2025_eoy", "cognitive_total", "cognitive_avg",
    "cognitive_max", "churn_5y_total", "hotspot_raw", "hotspot_log",
    "hotspot_percentile", "openssf_score", "cve_count_5y",
    "sast_findings_total", "sast_findings_error", "sast_findings_security",
    "github_sponsors", "stars", "forks", "watchers",
    "repo_age_years_2025_eoy", "openssf_maintained", "push_cadence_years",
    "issues_opened_5y", "issues_closed_5y", "issue_close_ratio",
    "net_new_issues_5y", "slope_opened", "slope_closed",
    "issue_trend_score", "loc_per_ac", "cve_per_ac", "nni_per_ac",
    "loc_per_ac_pctl", "cve_per_ac_pctl", "nni_per_ac_pctl",
    "workload_burden_percentile",
}
# Columns where a negative value would be a bug.
NONNEG = NUMERIC - {"net_new_issues_5y", "slope_opened", "slope_closed",
                    "issue_trend_score"}


def main() -> None:
    with open(RISK, encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [dict(zip(header, r)) for r in reader]

    total = len(rows)
    print(f"risk-data.csv: {total} rows, {len(header)} columns\n")

    # Duplicate column detection.
    dupes = [c for c, n in Counter(header).items() if n > 1]
    if dupes:
        print(f"!! DUPLICATE COLUMNS: {dupes}\n")

    for col in header:
        if col not in NUMERIC:
            continue
        vals = []  # (value, repo)
        for r in rows:
            raw = (r.get(col) or "").strip()
            if raw == "":
                continue
            try:
                vals.append((float(raw), r["repo"]))
            except ValueError:
                print(f"  !! {col}: unparseable value {raw!r} in {r['repo']}")
        missing = total - len(vals)
        if not vals:
            print(f"{col}: ALL MISSING ({missing}/{total})\n")
            continue
        vals.sort()
        neg = [v for v in vals if v[0] < 0]
        flag = ""
        if col in NONNEG and neg:
            flag = f"  !! {len(neg)} NEGATIVE"
        print(f"{col}: missing {missing}/{total}{flag}")
        print("  smallest:", ", ".join(f"{v:g}@{r}" for v, r in vals[:5]))
        print("  largest :", ", ".join(f"{v:g}@{r}" for v, r in vals[-5:]))
        if col in NONNEG and neg:
            print("  negatives:", ", ".join(f"{v:g}@{r}" for v, r in neg[:10]))
        print()


if __name__ == "__main__":
    main()
