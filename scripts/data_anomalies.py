"""Detect data anomalies in risk-data.csv and category CSVs.

Looks for:
- Eligible repos missing from category outputs
- Numeric outliers (negative complexity, very large LOC, percentile out of range)
- Inconsistent data across categories (e.g. eligible but missing in concentration)
- Stale fetched_at timestamps (data > 90 days old)
- Repos with same value across many fields (sentinel/placeholder fill)

Usage:
    uv run python scripts/data_anomalies.py
    uv run python scripts/data_anomalies.py --strict   # exit non-zero if any
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline.common.repos import load_risk_repos  # noqa: E402


def load_risk_scope() -> set[str]:
    """Repos the risk pipeline runs on — value-class A/B, non-archived, valid.

    risk-data.csv is generated from exactly this set (see
    `src.pipeline.risk.aggregate_risk`), so any repo here that is *missing*
    from risk-data.csv means the file is stale and needs a re-run. The old
    check compared against the *eligible* set (eligibility-data.csv), which
    after the value→risk rewire flagged every eligible-but-not-A/B repo as a
    false-positive gap.
    """
    entries = load_risk_repos(
        value_file=str(ROOT / "data/value-data.csv"),
        repos_file=str(ROOT / "data/github/repos.csv"),
    )
    return {e.repo for e in entries}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any anomaly is found")
    args = parser.parse_args()

    risk_scope = load_risk_scope()

    findings: list[tuple[str, str, str]] = []  # (severity, category, message)

    risk_path = ROOT / "data/risk-data.csv"
    with risk_path.open() as f:
        rows = list(csv.DictReader(f))

    in_risk = {r["repo"] for r in rows}

    # 1. Risk-scope coverage — a risk-scope repo absent from risk-data.csv
    #    means the file is stale (re-run src.pipeline.risk.aggregate_risk).
    missing = risk_scope - in_risk
    if missing:
        findings.append(("warn", "coverage",
                         f"{len(missing)} risk-scope repos missing from risk-data.csv: "
                         + ", ".join(sorted(missing)[:5]) +
                         ("..." if len(missing) > 5 else "")))

    # 2. Numeric outliers
    for r in rows:
        for f, ok_range in [
            ("loc_2025_eoy", (0, 50_000_000)),
            # scc "complexity" is a branch-keyword tally; the largest repos
            # in scope (archlinux/linux ≈ 2.6M) legitimately exceed 1M. The
            # bound only needs to catch a parse blow-up, not flag monorepos.
            ("scc_complexity_2025_eoy", (0, 10_000_000)),
            ("hhi_commits_lifetime", (0, 10_000)),
            ("openssf_score", (0, 10)),
            ("cve_count_5y", (0, 1000)),
            ("hotspot_percentile", (0, 100)),
            # issue_close_ratio > 1 is healthy — the repo closes faster than
            # issues open, or is clearing a pre-window backlog (observed up
            # to ~5 for quiet repos). Only a negative ratio is impossible;
            # the wide cap just catches a divide-by-near-zero blow-up.
            ("issue_close_ratio", (0, 100)),
        ]:
            v = (r.get(f) or "").strip()
            if not v:
                continue
            try:
                num = float(v)
            except ValueError:
                findings.append(("err", "type",
                                 f"{r['repo']}.{f} = {v!r} (not numeric)"))
                continue
            lo, hi = ok_range
            if num < lo or num > hi:
                findings.append(("warn", "outlier",
                                 f"{r['repo']}.{f} = {num} (outside [{lo}, {hi}])"))

    # 3. Stale fetched_at
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()
    for r in rows:
        for f in [c for c in r if c.endswith("fetched_at")]:
            v = (r.get(f) or "").strip()[:10]
            if not v:
                continue
            if v < cutoff[:10]:
                findings.append(("info", "staleness",
                                 f"{r['repo']}.{f} = {v} (> 90 days old)"))

    # 4. Negative scc complexity (logically impossible)
    for r in rows:
        v = (r.get("scc_complexity_2025_eoy") or "").strip()
        if v and v.startswith("-"):
            findings.append(("err", "negative", f"{r['repo']} scc_complexity={v}"))

    # 5. Same-value sentinel detection (repo with 0 in many numeric fields)
    numeric_fields = [
        "loc_2025_eoy", "sloc_2025_eoy", "scc_complexity_2025_eoy",
        "cognitive_total", "cyclomatic_total", "stars", "forks",
        "total_commits_lifetime", "active_contributors",
    ]
    for r in rows:
        zero_count = sum(1 for f in numeric_fields if (r.get(f) or "").strip() == "0")
        populated = sum(1 for f in numeric_fields if (r.get(f) or "").strip())
        if populated >= 5 and zero_count >= 5:
            findings.append(("info", "many-zeros",
                             f"{r['repo']}: {zero_count}/{populated} numeric fields = 0"))

    # 6. Report
    severity_counter = {"err": 0, "warn": 0, "info": 0}
    table = Table(title="Anomalies", show_header=True, header_style="bold dim")
    table.add_column("severity"); table.add_column("category"); table.add_column("message")
    grouped: dict[tuple[str, str], list[str]] = {}
    for sev, cat, msg in findings:
        severity_counter[sev] = severity_counter.get(sev, 0) + 1
        grouped.setdefault((sev, cat), []).append(msg)

    for (sev, cat), msgs in sorted(grouped.items(), key=lambda x: ("err warn info".split().index(x[0][0]), x[0][1])):
        sev_style = {"err": "[red]err[/red]", "warn": "[yellow]warn[/yellow]", "info": "[dim]info[/dim]"}[sev]
        sample = msgs[0]
        if len(msgs) > 1:
            sample += f" (+{len(msgs)-1} more)"
        table.add_row(sev_style, cat, sample)

    console.print(table)
    console.print(f"\nTotal: {severity_counter['err']} err, {severity_counter['warn']} warn, {severity_counter['info']} info")

    if args.strict and (severity_counter["err"] > 0 or severity_counter["warn"] > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
