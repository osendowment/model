#!/usr/bin/env python3
"""Build data/preview/repos.csv — the final cross-stage rollup.

The pipeline's terminal output. **Every top repo** (every row of
`data/eligibility/eligibility.csv` — the full Value → Risk → Eligibility
population, archived repos included), joined with its Value, Risk, and
Eligibility signals plus two derived columns. Unlike a filtered shortlist,
ineligible/incomplete repos stay in the table with their `eligible` flag
visible — filter on it downstream if you want only fundable candidates.

Columns:
    repo              canonical slug (first column — the row's identity for
                      human readers; the preview workbook links it to the
                      repo's home page)
    language          primary language, lowercased. GitHub repos from
                      `data/sources/github/repos.csv`; GitLab repos fall back
                      to `data/sources/gitlab/repos.csv` (GitLab Languages API).
                      Blank for any repo not fetched on either host.
    platform          repo host, `github` / `gitlab`    (value.csv `platform`;
                      falls back to the repo_id prefix when the value row is
                      missing)
    ecosystem         top ecosystem                     (value.csv `top_eco`)
    top_eco_pkg       top package of the repo in `top_eco` (value.csv `top_eco_pkg`)
    top_eco_pct       PageRank percentile in top_eco     (value.csv)
    pr_score          cross-ecosystem dependency mass, 0-100 (value.csv)
    openssf_crit      OpenSSF criticality score, 0-100   (value.csv)
    eco_crit          ecosyste.ms critical flag, 0/100   (value.csv)
    value_score       0-100 value blend of the four preceding
                      components                         (value.csv `value_score`)
    concentration, complexity, security, workload        (risk.csv components)
    risk_score        overall risk score, 0-100          (risk.csv `risk_score`)
    score             sqrt(value_score * risk_score) — the geometric mean of
                      the two 0-100 scores, itself 0-100 and deliberately
                      UNNORMALIZED (absolute, comparable across runs; the
                      current top row lands ≈76). Computed for every row with
                      both value_score and risk_score present, regardless of
                      eligibility.
    oss, intent, nonprofit, active                        (eligibility.csv components)
    eligible          oss AND intent AND nonprofit AND active
    priority          dense rank (1, 2, 3, …) by score desc, among eligible
                      rows only — blank for ineligible rows and any row
                      missing a score.
    repo_id           stable platform-qualified id (`gh/<n>` / `gl/<nickname>-<n>`)
                      — last column; every join key. Kept in this CSV for
                      traceability, but the preview workbook drops the column
                      from the repos sheet after deriving the hyperlinks.

All score-like numeric columns (`top_eco_pct`, `pr_score`, `openssf_crit`, `value_score`,
the four risk components, `risk_score`) are rounded to 2 decimal places for
this preview output — `eco_crit` is a 0/100 flag, not a score, and is left as
its raw upstream value. `score` itself is always computed from the
FULL-PRECISION upstream `value_score`/`risk_score`, before rounding, so
rounding the displayed columns never shifts the ranking.

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
import math
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
    ["repo", "language", "platform", "ecosystem", "top_eco_pkg", "top_eco_pct",
     "pr_score", "openssf_crit", "eco_crit", "value_score"]
    + RISK_COMPONENTS
    + ["risk_score", "score"]
    + ELIGIBILITY_COMPONENTS
    + ["eligible", "priority", "repo_id"]
)


def _num(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _round2(x: str) -> str:
    """Round a numeric string to 2 decimal places; blank/unparseable -> ""."""
    n = _num(x)
    return f"{n:.2f}" if n is not None else ""


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

        platform = (v.get("platform") or "").strip().lower()
        if not platform:
            platform = {"gh": "github", "gl": "gitlab"}.get(rid.split("/", 1)[0], "")

        row = {
            "repo": repo,
            "language": language.lower(),
            "platform": platform,
            "ecosystem": (v.get("top_eco") or "").strip(),
            "top_eco_pkg": (v.get("top_eco_pkg") or "").strip(),
            "top_eco_pct": _round2(v.get("top_eco_pct") or ""),
            "pr_score": _round2(v.get("pr_score") or ""),
            "openssf_crit": _round2(v.get("openssf_crit") or ""),
            "eco_crit": (v.get("eco_crit") or "").strip(),
            "value_score": _round2(value_score),
            **{col: _round2(r.get(col) or "") for col in RISK_COMPONENTS},
            "risk_score": _round2(risk_score),
            "score": "",
            **{col: (e.get(col) or "").strip() for col in ELIGIBILITY_COMPONENTS},
            "eligible": (e.get("eligible") or "").strip(),
            "priority": "",
            "repo_id": rid,
        }
        rows.append(row)

    # score: sqrt(value_score * risk_score) — geometric mean on the same
    # absolute 0-100 scale as its inputs, no per-run normalization. Computed
    # for every row with both inputs present, regardless of eligibility.
    for row in rows:
        raw = raw_by_id.get(row["repo_id"])
        if raw is not None:
            row["score"] = f"{math.sqrt(raw):.2f}"

    # priority: dense 1,2,3… rank by score desc, eligible + scored rows only.
    # Ranked on the full-precision value*risk product, NOT the displayed 2dp
    # score — rounding creates dozens of ties whose order would then hinge on
    # the repo-name tie-break, so a rename could silently reorder neighbors.
    ranked = sorted(
        (row for row in rows if row["eligible"] == "True" and row["score"]),
        key=lambda row: (-raw_by_id[row["repo_id"]], row["repo"]),
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
