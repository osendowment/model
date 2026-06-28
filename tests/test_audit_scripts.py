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


def test_valid_repos_have_git_url():
    """Every valid repo must carry a git_url, and a valid github repo must have
    the canonical github clone URL (both github_repo AND git_url — not one or
    the other). Regression: git_url was being stripped from github repos,
    leaving every valid github repo with an empty git_url.
    """
    value_csv = ROOT / "data" / "value" / "value.csv"
    if not value_csv.exists():
        pytest.skip("value.csv not present")
    with value_csv.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("valid") == "True"]
    if not rows:
        pytest.skip("no valid repos in value.csv")

    no_git = [r for r in rows if not (r.get("git_url") or "").strip()]
    assert not no_git, (
        f"{len(no_git)} valid repos have no git_url, e.g. "
        f"{[r.get('github_repo') or r.get('id') for r in no_git[:5]]}"
    )

    def canonical(slug: str) -> str:
        return f"https://github.com/{slug.strip().lower()}.git"

    mismatched = [
        r for r in rows
        if (r.get("github_repo") or "").strip()
        and (r.get("git_url") or "").strip().lower() != canonical(r["github_repo"])
    ]
    assert not mismatched, (
        f"{len(mismatched)} valid github repos have a non-canonical git_url, e.g. "
        f"{[(r['github_repo'], r['git_url']) for r in mismatched[:5]]}"
    )
