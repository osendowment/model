"""Pipeline health check — internal consistency of the value/risk pipeline.

Verifies the *derived* CSVs are in sync with their builders and inputs. It
catches the class of staleness bug where an upstream file is regenerated
but a downstream file is not re-run (e.g. complexity.csv changing without
build_workload being re-run — a real bug found mid-development).

Complements `scripts/data_anomalies.py`, which checks metric *values* for
outliers/types; this script checks *consistency*.

Checks:
  1. Each dimension CSV — the four risk dimensions (complexity /
     concentration / security / workload) plus the eligibility-stage builds
     (funding / licenses / active) — matches its `build_<dim>.build()`.
  2. data/risk/risk.csv matches `aggregate_risk.aggregate()` and
     data/eligibility/eligibility.csv matches `build_eligibility.build()`.
  3. data/value/value.csv matches `unify_value_data` (class assignments).
  4. Score-forming component columns are 100%-populated across the risk-scope
     set (a blank = a failed upstream fetch, since edge cases are imputed).
  5. Completeness rule: a component score / the overall risk score is present
     only if ALL of its scored inputs are present (no partial scores on disk).
  6. Long-format git files have no duplicate (repo, sha, metric) keys.

Usage:
    uv run python scripts/pipeline_health.py
    uv run python scripts/pipeline_health.py --strict   # exit 1 if unhealthy
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (label, ok, detail)
Result = tuple[str, bool, str]


def _norm(rows: list[dict]) -> dict[str, dict[str, str]]:
    """list[dict] → {repo: {col: str-value}}, None rendered as ''."""
    return {
        r["repo"]: {k: ("" if v is None else str(v)) for k, v in r.items()}
        for r in rows
    }


def _read_csv_by_repo(path: Path) -> dict[str, dict[str, str]]:
    with open(path, encoding="utf-8") as f:
        return {r["repo"]: dict(r) for r in csv.DictReader(f)}


def _cell_diffs(built: dict, disk: dict) -> int:
    return sum(
        1
        for repo in built
        for k in set(built[repo]) | set(disk[repo])
        if built[repo].get(k, "") != disk[repo].get(k, "")
    )


def check_dimension_csvs() -> list[Result]:
    """Each dimension CSV must equal its builder's current output.

    Covers the four risk dimensions plus the eligibility-stage builds
    (funding / licenses / active, under data/eligibility/).
    """
    from src.eligibility import build_active, build_funding, build_licenses
    from src.risk import (
        build_complexity,
        build_concentration,
        build_security,
        build_workload,
    )

    builders = [
        ("complexity", build_complexity, ROOT / "data" / "risk" / "complexity.csv"),
        ("concentration", build_concentration, ROOT / "data" / "risk" / "concentration.csv"),
        ("security", build_security, ROOT / "data" / "risk" / "security.csv"),
        ("workload", build_workload, ROOT / "data" / "risk" / "workload.csv"),
        ("funding", build_funding, ROOT / "data" / "eligibility" / "funding.csv"),
        ("licenses", build_licenses, ROOT / "data" / "eligibility" / "licenses.csv"),
        ("active", build_active, ROOT / "data" / "eligibility" / "active.csv"),
    ]
    out: list[Result] = []
    for name, mod, path in builders:
        built = _norm(mod.build())
        disk = _read_csv_by_repo(path)
        if set(built) != set(disk):
            out.append((f"{name}.csv", False,
                        f"repo set differs (builder {len(built)}, disk {len(disk)})"))
            continue
        diffs = _cell_diffs(built, disk)
        out.append((f"{name}.csv", diffs == 0,
                    "in sync" if diffs == 0
                    else f"{diffs} stale cells — re-run build_{name}"))
    return out


def check_risk_data() -> list[Result]:
    """risk.csv must equal aggregate_risk's join of the dimension CSVs."""
    from src.risk.aggregate_risk import aggregate

    rows = aggregate()
    built = _norm(rows)
    disk = _read_csv_by_repo(ROOT / "data" / "risk" / "risk.csv")
    if set(built) != set(disk):
        return [("risk.csv", False,
                 f"repo set differs (builder {len(built)}, disk {len(disk)})")]
    diffs = _cell_diffs(built, disk)
    return [("risk.csv", diffs == 0,
             "in sync" if diffs == 0
             else f"{diffs} stale cells — re-run aggregate_risk")]


def check_eligibility_data() -> list[Result]:
    """eligibility.csv must equal build_eligibility's join of the stage CSVs."""
    from src.eligibility.build_eligibility import build

    built = _norm(build())
    disk = _read_csv_by_repo(ROOT / "data" / "eligibility" / "eligibility.csv")
    if set(built) != set(disk):
        return [("eligibility.csv", False,
                 f"repo set differs (builder {len(built)}, disk {len(disk)})")]
    diffs = _cell_diffs(built, disk)
    return [("eligibility.csv", diffs == 0,
             "in sync" if diffs == 0
             else f"{diffs} stale cells — re-run build_eligibility")]


def check_value_data() -> list[Result]:
    """value.csv class assignments must match a fresh unify run."""
    from src.value.unify_value_data import (
        ECOSYSTEMS,
        aggregate_by_repo,
        collect_ecosystem,
    )

    all_rows: list[dict] = []
    for eco in ECOSYSTEMS:
        rows, _ = collect_ecosystem(eco)
        all_rows.extend(rows)
    built = aggregate_by_repo(all_rows)
    disk = list(csv.DictReader(open(ROOT / "data" / "value" / "value.csv", encoding="utf-8")))

    if len(built) != len(disk):
        return [("value.csv", False,
                 f"row count differs (unify {len(built)}, disk {len(disk)})")]

    def cls_by_repo(rows: list[dict]) -> dict[str, str]:
        seen = Counter((r.get("repo") or "").strip().lower() for r in rows)
        return {
            (r.get("repo") or "").strip().lower(): r.get("class", "")
            for r in rows
            if (r.get("repo") or "").strip()
            and seen[(r.get("repo") or "").strip().lower()] == 1
        }

    b, d = cls_by_repo(built), cls_by_repo(disk)
    mismatch = sum(1 for g in set(b) & set(d) if b[g] != d[g])
    return [("value.csv", mismatch == 0,
             f"class assignments in sync ({len(set(b) & set(d)):,} repos)"
             if mismatch == 0 else f"{mismatch} class mismatches — re-run unify")]


# Columns that MUST be populated for EVERY risk-scope repo because they form a
# component's risk score. A blank here is a real coverage gap (a failed upstream
# fetch), not a modelling choice — edge cases (dormant / bot-only / new repos)
# are imputed by the builder, so the only way these go blank is missing data.
# This is the guard that catches "late joiner" gaps: a fetcher auto-scopes to
# load_top_repos() but smart-skips cached repos, so a repo that joins class-A
# scope after the last fetch silently misses its scores until a forced re-fetch.
# Extend per dimension as 100%-coverage guarantees are added.
# workload is included: a zero-active-contributor repo is scored with AC=1
# (flagged `dormant`) rather than abstaining, so every top repo gets a score.
SCORE_COMPONENT_COVERAGE: dict[str, list[str]] = {
    "risk/concentration.csv": ["bf_commits_git_5y", "hhi_commits_git_5y", "score"],
    "risk/complexity.csv": ["score"],
    "risk/security.csv": ["score"],
    "eligibility/funding.csv": ["score"],
    "risk/workload.csv": ["score"],
}


def check_score_component_coverage() -> list[Result]:
    """Score-forming component columns must be 100% across the risk-scope set.

    For concentration, the builder imputes dormant / bot-only / new repos
    (bus factor 1, HHI 10000), so a blank `bf_commits_git_5y` / `hhi_commits_git_5y`
    / `score` means an upstream git fetch failed — surfaced here as the gap to
    fix (raise the fetch `--timeout` or re-run the fetcher for the listed repos).
    """
    from src.common.repos import load_top_repos

    repos = [e.repo for e in load_top_repos()]
    n = len(repos)
    out: list[Result] = []
    for fname, cols in SCORE_COMPONENT_COVERAGE.items():
        disk = _read_csv_by_repo(ROOT / "data" / fname)
        for col in cols:
            missing = [r for r in repos
                       if not str(disk.get(r, {}).get(col, "")).strip()]
            present = n - len(missing)
            ok = not missing
            if ok:
                detail = f"{present}/{n} (100%)"
            else:
                shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
                detail = f"{present}/{n} — {len(missing)} missing: {shown}"
            out.append((f"{fname}:{col}", ok, detail))
    return out


# A score may be present ONLY if all of its scored inputs are present. For each
# component CSV the inputs are the `composite_cols` its builder geometric-means
# (src/risk/build_<dim>.py); for risk.csv they are the four component scores
# aggregate_risk geometric-means. The producers already enforce this (a missing
# input blanks the score), so any score-with-a-missing-input on disk is a real
# inconsistency — a hand-edit or a builder/aggregator regression.
SCORE_INPUTS: dict[str, list[str]] = {
    "risk/concentration.csv": ["bf_commits_git_5y_p", "hhi_commits_git_5y_p"],
    "risk/complexity.csv":    ["loc_eoy_p", "cyclomatic_max_p"],
    "risk/security.csv":      ["openssf_score_p", "cve_score"],
    "eligibility/funding.csv": ["gh_sponsorships_p", "oc_avg_funding_p"],
    "risk/workload.csv":      ["loc_per_ac_p", "cve_per_ac_p", "nni_per_ac_p"],
    "risk/risk.csv":          ["concentration", "complexity", "security", "workload"],
}


def check_score_input_completeness() -> list[Result]:
    """A component score / the risk score may be present only if ALL its scored
    inputs are present.

    Enforces the completeness rule end to end: each component score is the
    geometric mean of its `composite_cols` (blanked when any is missing), and the
    overall risk score is the geometric mean of the four component scores (blanked
    when any component is missing). A row carrying a score while one of its inputs
    is blank violates the rule.
    """
    out: list[Result] = []
    for fname, inputs in SCORE_INPUTS.items():
        path = ROOT / "data" / fname
        if not path.exists():
            out.append((f"{fname} score⟸inputs", False, "file missing"))
            continue
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        scored = [r for r in rows if str(r.get("score", "")).strip()]
        bad = [r.get("repo", "?") for r in scored
               if not all(str(r.get(c, "")).strip() for c in inputs)]
        ok = not bad
        if ok:
            detail = f"{len(scored):,} scored rows — every input present"
        else:
            shown = ", ".join(bad[:5]) + (" …" if len(bad) > 5 else "")
            detail = f"{len(bad)} score(s) with a missing input: {shown}"
        out.append((f"{fname} score⟸inputs", ok, detail))
    return out


def check_long_format_keys() -> list[Result]:
    """Long-format git files must have unique (repo, sha, metric) keys."""
    out: list[Result] = []
    for path in sorted(glob.glob(str(ROOT / "data" / "sources" / "git" / "*.csv"))):
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        if not rows or not {"repo", "commit_sha", "metric"} <= set(rows[0]):
            continue  # not a long-format file
        keys = Counter((r["repo"], r["commit_sha"], r["metric"]) for r in rows)
        dups = sum(1 for v in keys.values() if v > 1)
        name = Path(path).name
        out.append((f"git/{name}", dups == 0,
                    f"{len(rows):,} rows, keys unique" if dups == 0
                    else f"{dups} duplicate (repo,sha,metric) keys"))
    return out


def check_value_criticality() -> list[Result]:
    """Every valid class-A GitHub row in value.csv carries a criticality score.

    The `criticality` column is filled by `src.value.apply_criticality` from
    the OpenSSF criticality fetch, whose scope is exactly this set (archived
    included) — so a blank is a missing/failed fetch or a skipped apply step,
    never "not applicable".
    """
    disk = list(csv.DictReader(open(ROOT / "data" / "value" / "value.csv",
                                    encoding="utf-8")))
    gate = [r for r in disk
            if (r.get("platform") or "").lower() == "github"
            and (r.get("valid") or "") == "True"
            and (r.get("class") or "") == "A"]
    blank = [r["repo"] for r in gate if not (r.get("criticality") or "").strip()]
    if blank:
        sample = ", ".join(blank[:3]) + ("…" if len(blank) > 3 else "")
        return [("value.csv:criticality", False,
                 f"{len(gate) - len(blank)}/{len(gate)} — {len(blank)} blank: {sample}")]
    return [("value.csv:criticality", True,
             f"{len(gate)}/{len(gate)} valid class-A github rows scored")]


# Source CSVs whose rows the builders join by the stable repo_id. A fetcher
# rewrite that drops or blanks the column silently blanks entire dimensions
# (this happened: contributor-commits, commits-years, and the GitHub
# contributor file were all clobbered by schema-unaware rewrites in one run).
ID_JOINED_SOURCES = [
    "sources/git/contributor-commits.csv",
    "sources/git/scc.csv",
    "sources/git/lizard.csv",
    "sources/git/openssf.csv",
    "sources/git/semgrep.csv",
    "sources/github/contributor-commits.csv",
    "sources/github/git/commits-years.csv",
    "sources/github/git/churn.csv",
    "sources/github/issues.csv",
    "sources/osv/cves.csv",
]


def check_source_repo_id_integrity() -> list[Result]:
    """Every id-joined source CSV keeps its repo_id column populated for
    in-scope repos.

    Two failure modes, both observed in the wild:
      1. header lost the repo_id column entirely (schema-unaware rewrite);
      2. rows for in-scope repos carry a blank repo_id (id map lagged a
         rename), so the id-keyed builder joins silently drop them.
    Out-of-scope rows may legitimately be blank (legacy/orphan slugs), so
    the gate only covers slugs in the current risk scope.
    """
    from src.common.repos import load_top_repos

    scope = {e.repo for e in load_top_repos(skip_archived=False)}
    out: list[Result] = []
    for rel in ID_JOINED_SOURCES:
        path = ROOT / "data" / rel
        if not path.exists():
            out.append((f"{rel}:repo_id", False, "file missing"))
            continue
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "repo_id" not in (reader.fieldnames or []):
                out.append((f"{rel}:repo_id", False,
                            "repo_id column MISSING from header (schema clobbered)"))
                continue
            blank: set[str] = set()
            for row in reader:
                slug = (row.get("repo") or "").strip().lower()
                if slug in scope and not (row.get("repo_id") or "").strip():
                    blank.add(slug)
        if blank:
            sample = ", ".join(sorted(blank)[:3]) + ("…" if len(blank) > 3 else "")
            out.append((f"{rel}:repo_id", False,
                        f"{len(blank)} in-scope repo(s) with blank repo_id: {sample}"))
        else:
            out.append((f"{rel}:repo_id", True, "in-scope rows fully id-keyed"))
    return out


CHECKS = [check_dimension_csvs, check_risk_data, check_eligibility_data,
          check_value_data, check_value_criticality,
          check_source_repo_id_integrity,
          check_score_component_coverage,
          check_score_input_completeness, check_long_format_keys]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any check fails")
    args = parser.parse_args()

    console.print("\n[bold]Pipeline health check[/bold]\n")
    results: list[Result] = []
    for check in CHECKS:
        results.extend(check())

    table = Table(show_header=True, header_style="bold dim", padding=(0, 1))
    table.add_column("Check", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")
    failures = 0
    for label, ok, detail in results:
        if not ok:
            failures += 1
        table.add_row(label, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)
    console.print(table)

    if failures:
        console.print(f"\n[red]{failures} check(s) failed[/red] of {len(results)}.\n")
    else:
        console.print(f"\n[green]All {len(results)} checks passed — pipeline consistent.[/green]\n")

    return 1 if (args.strict and failures) else 0


if __name__ == "__main__":
    sys.exit(main())
