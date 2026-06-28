"""Detect data anomalies in the risk-stage CSVs.

Reads the narrow aggregate `data/risk/risk.csv` plus the per-dimension detail
CSVs under `data/risk/` (`complexity.csv`, `concentration.csv`, `security.csv`,
`funding.csv`, `workload.csv`). Looks for:
- Risk-scope repos missing from `risk.csv` (stale aggregate)
- Numeric outliers (negative complexity, very large LOC, out-of-range values)
- Percentile columns (`*_p`) outside [0, 100]
- Dimension `score` columns outside [1, 100] (a 0 score is impossible by design)
- Stale `*_fetched_at` timestamps (data > 90 days old)
- Sentinel/placeholder fill (a repo with 0 across many numeric fields)

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

from src.common.repos import load_top_repos  # noqa: E402

RISK_DIR = ROOT / "data" / "risk"
SEVERITIES = ("err", "warn", "info")
DIMENSIONS = ("complexity", "concentration", "security", "funding", "workload")

Finding = tuple[str, str, str]  # (severity, category, message)

# Per-dimension detail CSVs and the year-agnostic columns we range-check.
# scc "complexity" is a branch-keyword tally; the largest in-scope repos
# (e.g. torvalds/linux) legitimately exceed 1M, so the bound only needs to
# catch a parse blow-up, not flag monorepos. issue_close_ratio > 1 is healthy
# (closes faster than opens); only a negative value is impossible.
DIMENSION_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "complexity": {
        "loc_eoy": (0, 50_000_000),
        "sloc_eoy": (0, 50_000_000),
        "scc_complexity_eoy": (0, 10_000_000),
        "cognitive_total": (0, 50_000_000),
        "cyclomatic_total": (0, 50_000_000),
    },
    "concentration": {
        "hhi_commits_git_5y": (0, 10_000),
        "hhi_commits_git_full": (0, 10_000),
        "hhi_commits_gh_alltime": (0, 10_000),
    },
    "security": {
        "openssf_score": (0, 10),
        # torvalds/linux legitimately has ~10k CVEs in a 5y window; the cap
        # only needs to catch a parse blow-up, not flag the kernel.
        "cve_count_5y": (0, 50_000),
    },
    "workload": {
        "issue_close_ratio": (0, 100),
    },
}


def load_risk_scope() -> set[str]:
    """Repos the risk pipeline runs on — value-class A/B, non-archived, valid.

    `risk.csv` is generated from exactly this set (see
    `src.risk.aggregate_risk`), so any repo here that is *missing* from
    `risk.csv` means the file is stale and needs a re-run.
    """
    entries = load_top_repos(
        value_file=str(ROOT / "data/value/value.csv"),
        repos_file=str(ROOT / "data/sources/github/repos.csv"),
    )
    return {e.repo for e in entries}


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _to_float(repo: str, col: str, raw: str, findings: list[Finding]) -> float | None:
    """Parse a numeric cell; on failure record a 'type' finding and return None."""
    try:
        return float(raw)
    except ValueError:
        findings.append(("err", "type", f"{repo}.{col} = {raw!r} (not numeric)"))
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any anomaly is found")
    args = parser.parse_args()

    risk_scope = load_risk_scope()
    findings: list[Finding] = []

    risk_rows = _read(RISK_DIR / "risk.csv")
    in_risk = {r["repo"] for r in risk_rows}

    # 1. Risk-scope coverage — a risk-scope repo absent from risk.csv means
    #    the aggregate is stale (re-run src.risk.aggregate_risk).
    missing = risk_scope - in_risk
    if missing:
        findings.append(("warn", "coverage",
                         f"{len(missing)} risk-scope repos missing from risk.csv: "
                         + ", ".join(sorted(missing)[:5])
                         + ("..." if len(missing) > 5 else "")))

    # 2. Dimension score range — risk.csv components + each detail CSV `score`
    #    are integer percentile scores floored at 1; 0 or >100 is impossible.
    for col in (*DIMENSIONS, "score"):
        for r in risk_rows:
            v = (r.get(col) or "").strip()
            if not v:
                continue
            num = _to_float(r["repo"], col, v, findings)
            if num is not None and not 1 <= num <= 100:
                findings.append(("err", "score-range",
                                 f"{r['repo']}.{col} = {num} (outside [1, 100])"))

    # 3. Per-dimension detail CSVs: numeric ranges, percentile bounds, scores.
    for dim, ranges in DIMENSION_RANGES.items():
        rows = _read(RISK_DIR / f"{dim}.csv")
        if not rows:
            findings.append(("warn", "missing-file", f"data/risk/{dim}.csv not found or empty"))
            continue
        header = rows[0].keys()
        pct_cols = [c for c in header if c.endswith("_p")]
        for r in rows:
            repo = r.get("repo", "?")
            # explicit numeric ranges
            for col, (lo, hi) in ranges.items():
                v = (r.get(col) or "").strip()
                if not v:
                    continue
                if v.startswith("-"):
                    findings.append(("err", "negative", f"{repo}.{col} = {v}"))
                    continue
                num = _to_float(repo, col, v, findings)
                if num is not None and not lo <= num <= hi:
                    findings.append(("warn", "outlier",
                                     f"{repo}.{col} = {num} (outside [{lo}, {hi}])"))
            # generic percentile bound check: every *_p must be in [0, 100]
            for col in pct_cols:
                v = (r.get(col) or "").strip()
                if not v:
                    continue
                num = _to_float(repo, col, v, findings)
                if num is not None and not 0 <= num <= 100:
                    findings.append(("err", "pct-range",
                                     f"{repo}.{col} = {num} (percentile outside [0, 100])"))
            # dimension score: floored at 1, so 0 is an anomaly
            sv = (r.get("score") or "").strip()
            if sv:
                num = _to_float(repo, f"{dim}.score", sv, findings)
                if num is not None and not 1 <= num <= 100:
                    findings.append(("err", "score-range",
                                     f"{repo}.{dim}.score = {num} (outside [1, 100])"))

    # 4. Stale fetched_at across every detail CSV.
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=90)).isoformat()[:10]
    for dim in DIMENSIONS:
        rows = _read(RISK_DIR / f"{dim}.csv")
        if not rows:
            continue
        ts_cols = [c for c in rows[0] if c.endswith("fetched_at")]
        for r in rows:
            for col in ts_cols:
                v = (r.get(col) or "").strip()[:10]
                if v and v < cutoff:
                    findings.append(("info", "staleness",
                                     f"{r.get('repo', '?')}.{dim}.{col} = {v} (> 90 days old)"))

    # 5. Sentinel detection — a repo with 0 across many complexity numeric fields.
    numeric_fields = [
        "loc_eoy", "sloc_eoy", "scc_complexity_eoy",
        "cognitive_total", "cyclomatic_total", "churn_5y_total",
    ]
    for r in _read(RISK_DIR / "complexity.csv"):
        zero_count = sum(1 for f in numeric_fields if (r.get(f) or "").strip() == "0")
        populated = sum(1 for f in numeric_fields if (r.get(f) or "").strip())
        if populated >= 5 and zero_count >= 5:
            findings.append(("info", "many-zeros",
                             f"{r.get('repo', '?')}: {zero_count}/{populated} complexity fields = 0"))

    # 6. Report
    severity_counter = dict.fromkeys(SEVERITIES, 0)
    table = Table(title="Anomalies", show_header=True, header_style="bold dim")
    table.add_column("severity"); table.add_column("category"); table.add_column("message")
    grouped: dict[tuple[str, str], list[str]] = {}
    for sev, cat, msg in findings:
        severity_counter[sev] += 1
        grouped.setdefault((sev, cat), []).append(msg)

    for (sev, cat), msgs in sorted(grouped.items(), key=lambda x: (SEVERITIES.index(x[0][0]), x[0][1])):
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
