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
    v, r = mod.value_stats(), mod.risk_stats()
    assert mod.check(v, r) == 0, "docs/stats.md headline numbers are stale — " \
        "re-run `uv run python scripts/stats.py --markdown`"


def test_risk_csv_keeps_company_backed_flagged_nonprofit():
    """Company-backed repos (funding host_type/owner_type == company) are KEPT in
    the final risk.csv and flagged nonprofit=False — they are no longer dropped.
    Regression for the intent/nonprofit reversal in aggregate_risk."""
    risk, funding = RISK_DIR / "risk.csv", RISK_DIR / "funding.csv"
    if not (risk.exists() and funding.exists()):
        pytest.skip("risk/funding csv not present")
    with funding.open() as f:
        company = {r["repo"].lower() for r in csv.DictReader(f)
                   if "company" in ((r.get("host_type") or "").lower(),
                                    (r.get("owner_type") or "").lower())}
    with risk.open() as f:
        risk_rows = {r["repo"].lower(): r for r in csv.DictReader(f)}
    present = company & set(risk_rows)
    assert present, "expected company-backed repos to appear in risk.csv"
    bad = {repo for repo in present if (risk_rows[repo].get("nonprofit") or "") != "False"}
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


# --- schema invariant sweep over the per-dimension risk CSVs ----------------

DIMENSION_CSVS = ["complexity", "concentration", "security", "funding", "workload"]


def _rows(name: str) -> list[dict]:
    path = RISK_DIR / f"{name}.csv"
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


# --- risk.csv aggregate columns: intent + nonprofit --------------------------


def test_risk_intent_and_nonprofit_are_boolean_flags():
    """risk.csv intent/nonprofit are present on every row and are 'True'/'False'."""
    path = RISK_DIR / "risk.csv"
    if not path.exists():
        pytest.skip("risk.csv not present")
    with path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        pytest.skip("risk.csv empty")
    for col in ("intent", "nonprofit"):
        assert col in rows[0], f"risk.csv missing {col} column"
        for r in rows:
            assert (r.get(col) or "").strip() in ("True", "False"), \
                f"{r.get('repo')}.{col}={r.get(col)!r}"


def test_valid_repos_are_github_with_canonical_url():
    """A valid repo must be a github repo (platform=github) with a `repo` slug
    AND the canonical github clone URL. Validity is github-only: orphans and
    non-github-only upstreams are invalid. Regression for (a) git_url stripped
    from github repos, (b) non-github / orphan rows being marked valid, and
    (c) a github `repo` ever coexisting with a non-github git_url.
    """
    value_csv = ROOT / "data" / "value" / "value.csv"
    if not value_csv.exists():
        pytest.skip("value.csv not present")
    with value_csv.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("valid") == "True"]
    if not rows:
        pytest.skip("no valid repos in value.csv")

    not_github = [r for r in rows if (r.get("platform") or "").strip() != "github"]
    assert not not_github, (
        f"{len(not_github)} valid repos are not platform=github, e.g. "
        f"{[(r.get('platform'), r.get('git_url')) for r in not_github[:5]]}"
    )

    no_repo = [r for r in rows if not (r.get("repo") or "").strip()]
    assert not no_repo, (
        f"{len(no_repo)} valid repos have no repo slug, e.g. "
        f"{[r.get('git_url') for r in no_repo[:5]]}"
    )

    no_git = [r for r in rows if not (r.get("git_url") or "").strip()]
    assert not no_git, (
        f"{len(no_git)} valid repos have no git_url, e.g. "
        f"{[r.get('repo') for r in no_git[:5]]}"
    )

    def canonical(slug: str) -> str:
        return f"https://github.com/{slug.strip().lower()}.git"

    mismatched = [
        r for r in rows
        if (r.get("repo") or "").strip()
        and (r.get("git_url") or "").strip().lower() != canonical(r["repo"])
    ]
    assert not mismatched, (
        f"{len(mismatched)} valid github repos have a non-canonical git_url, e.g. "
        f"{[(r['repo'], r['git_url']) for r in mismatched[:5]]}"
    )
