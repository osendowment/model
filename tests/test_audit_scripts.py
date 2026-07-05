"""Guard the anomaly/health audit scripts against reorg-rot.

These scripts (scripts/data_anomalies.py, scripts/pipeline_health.py,
scripts/coverage_report.py, scripts/investigate-risk-metrics.py,
scripts/fill_gaps.py) live outside the importable package and reference
module paths + data schema by string. After the src/pipeline -> src and
data/ -> data/sources reorgs they silently rotted (dead imports, stale
columns) and no test caught it. This file:

  1. imports each script as a module (catches dead-import rot);
  2. runs the read-only ones end-to-end against the live repo data;
  3. asserts the year-agnostic schema invariants the scripts rely on
     (percentile cols in [0, 100], dimension score in [1, 100]).
"""
from __future__ import annotations

import csv
import importlib.util
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
RISK_DIR = ROOT / "data" / "risk"
ELIGIBILITY_DIR = ROOT / "data" / "eligibility"

AUDIT_SCRIPTS = [
    "data_anomalies.py",
    "pipeline_health.py",
    "coverage_report.py",
    "investigate-risk-metrics.py",
    "fill_gaps.py",
    "stats.py",
]


def _load_module(path: Path):
    """Import a script file as a module without executing __main__."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(f"_audit_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # runs top-level (incl. imports) but not main()
    return mod


@pytest.mark.parametrize("script", AUDIT_SCRIPTS)
def test_audit_script_imports(script):
    """Each audit script imports cleanly — no dead src.pipeline.* references."""
    _load_module(SCRIPTS / script)


@pytest.mark.parametrize("script", ["investigate-risk-metrics.py"])
def test_readonly_script_runs(script):
    """Read-only scripts run end-to-end against live data without raising."""
    sys.argv = [script]
    runpy.run_path(str(SCRIPTS / script), run_name="__main__")


def test_data_anomalies_clean_or_reports():
    """data_anomalies.main() returns an int and does not raise on live data."""
    mod = _load_module(SCRIPTS / "data_anomalies.py")
    sys.argv = ["data_anomalies.py"]
    rc = mod.main()
    assert rc in (0, 1)


def test_stats_md_is_not_stale():
    """scripts/stats.py recomputes every docs/stats.md figure from the live CSVs;
    its --check headline gate must pass, i.e. stats.md reflects the current run.
    Regression for stats.md drifting after a value/scope/risk regeneration."""
    mod = _load_module(SCRIPTS / "stats.py")
    v, r, e = mod.value_stats(), mod.risk_stats(), mod.eligibility_stats()
    assert mod.check(v, r, e) == 0, "docs/stats.md headline numbers are stale — " \
        "re-run `uv run python scripts/stats.py --markdown`"


def test_eligibility_csv_keeps_company_backed_flagged_nonprofit():
    """Company-backed repos (funding host_type/owner_type == company) are KEPT
    in eligibility.csv and flagged nonprofit=False — they are not dropped.
    Regression for the intent/nonprofit reversal (originally in aggregate_risk,
    now in the eligibility rollup)."""
    eligibility = ELIGIBILITY_DIR / "eligibility.csv"
    funding = ELIGIBILITY_DIR / "funding.csv"
    if not (eligibility.exists() and funding.exists()):
        pytest.skip("eligibility/funding csv not present")
    with funding.open() as f:
        company = {r["repo"].lower() for r in csv.DictReader(f)
                   if "company" in ((r.get("host_type") or "").lower(),
                                    (r.get("owner_type") or "").lower())}
    with eligibility.open() as f:
        elig_rows = {r["repo"].lower(): r for r in csv.DictReader(f)}
    present = company & set(elig_rows)
    assert present, "expected company-backed repos to appear in eligibility.csv"
    bad = {repo for repo in present if (elig_rows[repo].get("nonprofit") or "") != "False"}
    assert not bad, f"company-backed repos not flagged nonprofit=False: {sorted(bad)}"


def test_score_component_coverage_is_full():
    """Score-forming component columns are 100%-populated on live data.

    Regression guard for the no-human-in-window imputation: if a change drops
    concentration bf/hhi/score coverage below the full risk set (e.g. the
    imputation is removed, or a fetch gap reappears), this fails — mirroring the
    pipeline_health check in-process.
    """
    mod = _load_module(SCRIPTS / "pipeline_health.py")
    results = mod.check_score_component_coverage()
    assert results, "no coverage results returned"
    failed = [(label, detail) for label, ok, detail in results if not ok]
    assert not failed, f"score components below 100%: {failed}"


# --- schema invariant sweep over the per-dimension CSVs ----------------------
# The four risk dimensions plus the eligibility-stage funding build (moved to
# data/eligibility/ but still a scored, percentile-carrying dimension CSV).

DIMENSION_CSVS = ["complexity", "concentration", "security", "funding", "workload"]
DIMENSION_PATHS = {
    "funding": ELIGIBILITY_DIR / "funding.csv",
}


def _rows(name: str) -> list[dict]:
    path = DIMENSION_PATHS.get(name, RISK_DIR / f"{name}.csv")
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("dim", DIMENSION_CSVS)
def test_percentile_columns_in_range(dim):
    """Every *_p percentile cell is blank or within [0, 100]."""
    rows = _rows(dim)
    if not rows:
        pytest.skip(f"{dim}.csv not present")
    pcols = [c for c in rows[0] if c.endswith("_p")]
    for r in rows:
        for c in pcols:
            v = (r.get(c) or "").strip()
            if not v:
                continue
            assert 0 <= float(v) <= 100, f"{r.get('repo')}.{c}={v} outside [0,100]"


@pytest.mark.parametrize("dim", DIMENSION_CSVS)
def test_dimension_score_floored_at_one(dim):
    """Dimension `score` is blank or an int in [1, 100] (0 is impossible)."""
    rows = _rows(dim)
    if not rows:
        pytest.skip(f"{dim}.csv not present")
    for r in rows:
        v = (r.get("score") or "").strip()
        if not v:
            continue
        assert 1 <= float(v) <= 100, f"{r.get('repo')}.{dim}.score={v} outside [1,100]"


# --- eligibility.csv rollup columns: strict boolean flags --------------------


def test_eligibility_flags_are_boolean():
    """eligibility.csv flag columns are present on every row and 'True'/'False'
    (the rollup is strict — the ternary license detail stays in licenses.csv)."""
    path = ELIGIBILITY_DIR / "eligibility.csv"
    if not path.exists():
        pytest.skip("eligibility.csv not present")
    with path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        pytest.skip("eligibility.csv empty")
    for col in ("oss", "intent", "nonprofit", "active", "eligible"):
        assert col in rows[0], f"eligibility.csv missing {col} column"
        for r in rows:
            assert (r.get(col) or "").strip() in ("True", "False"), \
                f"{r.get('repo')}.{col}={r.get(col)!r}"


def test_valid_repos_have_a_reachable_upstream():
    """A valid repo's upstream must resolve, on any host. Validity is
    host-agnostic; the numeric `repo_id` is platform-namespaced. Invariants for
    every git_valid=True row:
    (a) it has a git_url (orphans — no upstream — are never valid);
    (b) a github row carries the canonical github clone URL AND a `gh/` repo_id;
    (c) a gitlab row's repo_id, IF present, is a `gl/` id (never `gh/`); it may be
        empty for a repo not yet fetched from the GitLab API — that is coverage,
        tracked in stats.md, not an invariant;
    (d) any other host (codeberg/bitbucket/custom) carries NO repo_id;
    (e) a `gl/` id appears ONLY on a gitlab row.
    Plus the global rule: a non-empty repo_id (gh/ or gl/) ⇒ git_valid.
    Regression for git_url stripped from valid rows, orphans marked valid,
    non-canonical github URLs, gl/ ids on non-gitlab rows, and repo_id leaking
    onto unsupported hosts.
    """
    value_csv = ROOT / "data" / "value" / "value.csv"
    if not value_csv.exists():
        pytest.skip("value.csv not present")
    with value_csv.open() as f:
        all_rows = list(csv.DictReader(f))
    rows = [r for r in all_rows if r.get("git_valid") == "True"]
    if not rows:
        pytest.skip("no valid repos in value.csv")

    # (a) every valid repo has a git_url — an orphan (no upstream) is not valid.
    no_git = [r for r in rows if not (r.get("git_url") or "").strip()]
    assert not no_git, (
        f"{len(no_git)} valid repos have no git_url (orphans must be invalid), "
        f"e.g. {[r.get('repo') for r in no_git[:5]]}"
    )

    def plat(r):
        return (r.get("platform") or "").strip()
    github = [r for r in rows if plat(r) == "github"]
    gitlab = [r for r in rows if plat(r) == "gitlab"]
    other = [r for r in rows if plat(r) not in ("github", "gitlab")]

    # (b) github valid rows: a repo slug, the canonical clone URL, a gh/ repo_id.
    def canonical(slug: str) -> str:
        return f"https://github.com/{slug.strip().lower()}.git"

    bad_github = [
        r for r in github
        if not (r.get("repo") or "").strip()
        or (r.get("git_url") or "").strip().lower() != canonical(r.get("repo", ""))
        or not (r.get("repo_id") or "").strip().startswith("gh/")
    ]
    assert not bad_github, (
        f"{len(bad_github)} valid github repos lack a slug / canonical url / gh/ "
        f"repo_id, e.g. {[(r['repo'], r['git_url'], r['repo_id']) for r in bad_github[:5]]}"
    )

    # (c) a gitlab row's repo_id, if present, is a gl/ id (never gh/). An empty
    # repo_id is allowed — that repo just hasn't been fetched from the GitLab API
    # yet (coverage, not an invariant).
    bad_gitlab = [
        r for r in gitlab
        if (r.get("repo_id") or "").strip()
        and not (r.get("repo_id") or "").strip().startswith("gl/")
    ]
    assert not bad_gitlab, (
        f"{len(bad_gitlab)} valid gitlab repos carry a non-gl/ repo_id, e.g. "
        f"{[(r.get('repo'), r.get('repo_id')) for r in bad_gitlab[:5]]}"
    )

    # (d) any other host carries NO numeric repo_id (github + gitlab only today).
    id_on_other = [r for r in other if (r.get("repo_id") or "").strip()]
    assert not id_on_other, (
        f"{len(id_on_other)} non-github/gitlab valid repos carry a repo_id, e.g. "
        f"{[(r.get('platform'), r.get('repo_id')) for r in id_on_other[:5]]}"
    )

    # (e) a gl/ id appears only on a gitlab row.
    gl_on_nongitlab = [
        r for r in all_rows
        if (r.get("repo_id") or "").strip().startswith("gl/") and plat(r) != "gitlab"
    ]
    assert not gl_on_nongitlab, (
        f"{len(gl_on_nongitlab)} non-gitlab rows carry a gl/ repo_id, e.g. "
        f"{[(r.get('platform'), r.get('repo_id')) for r in gl_on_nongitlab[:5]]}"
    )

    # global: a non-empty repo_id (gh/ or gl/) implies git_valid — an id is only
    # assigned when the platform API confirmed the repo exists.
    id_but_invalid = [
        r for r in all_rows
        if (r.get("repo_id") or "").strip() and r.get("git_valid") != "True"
    ]
    assert not id_but_invalid, (
        f"{len(id_but_invalid)} repos carry a repo_id but are git_valid!=True, e.g. "
        f"{[(r.get('repo'), r.get('repo_id')) for r in id_but_invalid[:5]]}"
    )
