#!/usr/bin/env python3
"""Build data/preview/repos.csv — the final cross-stage rollup.

The pipeline's terminal output. **Every top repo** (every row of
`data/eligibility/eligibility.csv` — the full Value → Risk → Eligibility
population, archived repos included), joined with its Value, Risk, and
Eligibility signals plus two derived columns. Unlike a filtered shortlist,
ineligible/incomplete repos stay in the table with their `eligible` flag
visible — filter on it downstream if you want only fundable candidates.

Columns:
    repo_id           stable platform-qualified id (`gh/<n>` / `gl/<host>-<n>`)
    repo              canonical slug
    language          primary language, lowercased. GitHub repos from
                      `data/sources/github/repos.csv`; GitLab repos fall back
                      to `data/sources/gitlab/repos.csv` (GitLab Languages API).
                      Blank for any repo not fetched on either host.
    ecosystem         top ecosystem                     (value.csv `top_eco`)
    openssf_crit      OpenSSF criticality score, 0-100   (value.csv)
    eco_crit          ecosyste.ms critical flag, 0/1     (value.csv)
    top_eco_pct       PageRank percentile in top_eco     (value.csv)
    value_score       0-100 value blend                  (value.csv `value_score`)
    concentration, complexity, security, workload        (risk.csv components)
    risk_score        overall risk score, 0-100          (risk.csv `risk_score`)
    oss, intent, nonprofit, active                        (eligibility.csv components)
    eligible          oss AND intent AND nonprofit AND active
    score             value_score * risk_score, scaled so the highest row = 100.
                      Computed for every row with both value_score and
                      risk_score present, regardless of eligibility.
    priority          dense rank (1, 2, 3, …) by score desc, among eligible
                      rows only — blank for ineligible rows and any row
                      missing a score.

Joins: every file keys on the stable `repo_id` — eligibility.csv's population
(all top repos) drives the row set; value.csv / risk.csv / github/repos.csv /
gitlab/repos.csv are joined onto it. A repo missing from a joined file (e.g. an
archived repo absent from risk.csv, or a GitHub repo absent from
gitlab/repos.csv) leaves those columns blank rather than dropping the row.

Sorted by `score` desc (blank last), then `repo` — so row order matches
`priority` order for the scored/eligible subset.

Usage:
    uv run python -m src.build_results
"""

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.common.tables import load_rows_by_id

console = Console()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ELIGIBILITY_FILE = DATA_DIR / "eligibility" / "eligibility.csv"
VALUE_FILE = DATA_DIR / "value" / "value.csv"
RISK_FILE = DATA_DIR / "risk" / "risk.csv"
GITHUB_REPOS_FILE = DATA_DIR / "sources" / "github" / "repos.csv"
GITLAB_REPOS_FILE = DATA_DIR / "sources" / "gitlab" / "repos.csv"
OUTPUT_FILE = DATA_DIR / "preview" / "repos.csv"

RISK_COMPONENTS = ["concentration", "complexity", "security", "workload"]
ELIGIBILITY_COMPONENTS = ["oss", "intent", "nonprofit", "active"]

FIELDS = (
    ["repo_id", "repo", "language", "ecosystem", "openssf_crit", "eco_crit",
     "top_eco_pct", "value_score"]
    + RISK_COMPONENTS
    + ["risk_score"]
    + ELIGIBILITY_COMPONENTS
    + ["eligible", "score", "priority"]
)


def _num(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build() -> list[dict]:
    value_by_id = load_rows_by_id(VALUE_FILE)
    risk_by_id = load_rows_by_id(RISK_FILE)
    language_by_id = load_rows_by_id(GITHUB_REPOS_FILE)
    gitlab_language_by_id = load_rows_by_id(GITLAB_REPOS_FILE)

    with open(ELIGIBILITY_FILE, encoding="utf-8") as f:
        eligibility_rows = list(csv.DictReader(f))

    rows: list[dict] = []
    raw_by_id: dict[str, float] = {}
    for e in eligibility_rows:
        rid = (e.get("repo_id") or "").strip()
        repo = (e.get("repo") or "").strip()
        v = value_by_id.get(rid, {})
        r = risk_by_id.get(rid, {})
        g = language_by_id.get(rid, {})
        # GitHub language first; GitLab repos (gl/ ids, absent from github/repos.csv)
        # fall back to their GitLab Languages API result in gitlab/repos.csv.
        language = (g.get("language") or "").strip()
        if not language:
            language = (gitlab_language_by_id.get(rid, {}).get("language") or "").strip()

        value_score = (v.get("value_score") or "").strip()
        risk_score = (r.get("risk_score") or "").strip()
        vs_num, rs_num = _num(value_score), _num(risk_score)
        if vs_num is not None and rs_num is not None:
            raw_by_id[rid] = vs_num * rs_num

        row = {
            "repo_id": rid,
            "repo": repo,
            "language": language.lower(),
            "ecosystem": (v.get("top_eco") or "").strip(),
            "openssf_crit": (v.get("openssf_crit") or "").strip(),
            "eco_crit": (v.get("eco_crit") or "").strip(),
            "top_eco_pct": (v.get("top_eco_pct") or "").strip(),
            "value_score": value_score,
            **{col: (r.get(col) or "").strip() for col in RISK_COMPONENTS},
            "risk_score": risk_score,
            **{col: (e.get(col) or "").strip() for col in ELIGIBILITY_COMPONENTS},
            "eligible": (e.get("eligible") or "").strip(),
            "score": "",
            "priority": "",
        }
        rows.append(row)

    # score: value_score * risk_score, rescaled so the highest raw product in
    # the table maps to 100. Computed for every row with both inputs present,
    # regardless of eligibility.
    max_raw = max(raw_by_id.values()) if raw_by_id else None
    if max_raw:
        for row in rows:
            raw = raw_by_id.get(row["repo_id"])
            if raw is not None:
                row["score"] = f"{raw / max_raw * 100:.2f}"

    # priority: dense 1,2,3… rank by score desc, eligible + scored rows only.
    ranked = sorted(
        (row for row in rows if row["eligible"] == "True" and row["score"]),
        key=lambda row: (-float(row["score"]), row["repo"]),
    )
    for i, row in enumerate(ranked, start=1):
        row["priority"] = str(i)

    # Row order: scored rows first (score desc, matching priority order),
    # then everything else by repo for determinism.
    def _sort_key(row: dict) -> tuple:
        s = row["score"]
        return (s == "", -float(s) if s else 0.0, row["repo"])

    rows.sort(key=_sort_key)
    return rows


def main() -> None:
    rows = build()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    table = Table(title="[bold]Results — top repos by priority[/bold]",
                  show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("repo", style="bold")
    table.add_column("eco")
    table.add_column("value", justify="right")
    table.add_column("risk", justify="right")
    table.add_column("score", justify="right")
    table.add_column("pri", justify="right")
    for r in rows[:10]:
        table.add_row(r["repo"], r["ecosystem"], r["value_score"], r["risk_score"],
                      r["score"], r["priority"])
    console.print(table)

    eligible = sum(1 for r in rows if r["eligible"] == "True")
    scored = sum(1 for r in rows if r["score"])
    console.print(
        f"\n[dim]{len(rows):,} top repos · {eligible:,} eligible · "
        f"{scored:,} scored (value_score + risk_score present) → {OUTPUT_FILE}[/dim]"
    )


if __name__ == "__main__":
    main()
