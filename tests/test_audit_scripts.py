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


def test_risk_scope_matches_eligibility_scope():
    """Eligibility now shares the risk scope — both A/B ∩ valid.

    risk.csv and eligibility.csv must cover the same repo set (the prior
    927-vs-897 mismatch was the eligibility-scope bug).
    """
    risk = {r["repo"] for r in _rows("risk") if r.get("repo")}
    elig_path = ROOT / "data" / "eligibility" / "eligibility.csv"
    if not risk or not elig_path.exists():
        pytest.skip("risk.csv or eligibility.csv not present")
    with elig_path.open() as f:
        elig = {r["repo"] for r in csv.DictReader(f) if r.get("repo")}
    assert risk == elig, (
        f"risk\\eligibility={sorted(risk - elig)[:5]}, "
        f"eligibility\\risk={sorted(elig - risk)[:5]}"
    )
