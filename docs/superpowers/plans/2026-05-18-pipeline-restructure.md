# src/pipeline Restructure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Reorganise `src/pipeline/` into `common/ value/ risk/ eligibility/` folders with per-step scripts, per-ecosystem orchestrators, and three stage runners — a pure restructure, no behaviour change.

**Architecture:** `git mv` every module into its stage folder; mechanically repoint all `src.pipeline.*` imports; split `value.py` into `unify_value_data.py` + `verify_git_urls.py`; add a shared subprocess-based `pipeline_runner` helper that the 6 ecosystem orchestrators and 3 stage runners use.

**Tech Stack:** Python 3, `uv`, `pytest`. Spec: `docs/superpowers/specs/2026-05-18-pipeline-restructure-design.md`.

**Baseline invariant:** `uv run pytest -q --continue-on-collection-errors` = **225 passed, 7 failed** (7 pre-existing, unrelated). Every task must keep this exact count — zero new failures. macOS `sed` needs `sed -i ''`.

---

## Phase 1 — Scaffolding

### Task 1: Create folder skeleton + pipeline_runner helper

**Files:**
- Create: `src/pipeline/common/__init__.py`, `src/pipeline/value/__init__.py`, `src/pipeline/risk/__init__.py`, `src/pipeline/eligibility/__init__.py` (all empty)
- Create: `src/pipeline/common/pipeline_runner.py`
- Test: `tests/test_pipeline_runner.py`

- [ ] **Step 1:** Create the four empty `__init__.py` files:
```bash
cd /Users/kv/Dev/osendowment/model
touch src/pipeline/common/__init__.py src/pipeline/value/__init__.py \
      src/pipeline/risk/__init__.py src/pipeline/eligibility/__init__.py
```

- [ ] **Step 2: Write the failing test** `tests/test_pipeline_runner.py`:
```python
"""Tests for the shared pipeline orchestration helper."""
from src.pipeline.common.pipeline_runner import Step, select_steps


STEPS = [
    Step("a", "m.a", fetch=True),
    Step("b", "m.b"),
    Step("c", "m.c", fetch=True),
    Step("d", "m.d"),
]


def test_select_all():
    assert [s.label for s in select_steps(STEPS, None, None, False)] == ["a", "b", "c", "d"]


def test_select_only():
    assert [s.label for s in select_steps(STEPS, None, "c", False)] == ["c"]


def test_select_from():
    assert [s.label for s in select_steps(STEPS, "b", None, False)] == ["b", "c", "d"]


def test_select_skip_fetch():
    assert [s.label for s in select_steps(STEPS, None, None, True)] == ["b", "d"]


def test_select_from_plus_skip_fetch():
    assert [s.label for s in select_steps(STEPS, "b", None, True)] == ["b", "d"]


def test_unknown_step_raises():
    import pytest
    with pytest.raises(KeyError):
        select_steps(STEPS, None, "zzz", False)
```

- [ ] **Step 3: Run test, verify it fails** — `uv run pytest tests/test_pipeline_runner.py -q` → FAIL (module not found).

- [ ] **Step 4: Implement** `src/pipeline/common/pipeline_runner.py`:
```python
"""Shared orchestration helper for pipeline runners.

A pipeline is an ordered list of `Step`s. `run_pipeline` runs each as
`python -m <module>` in a subprocess, honouring --from / --only /
--skip-fetch / --list. Steps flagged `pipeline=True` are themselves
orchestrators — `--skip-fetch` is forwarded into them.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass


@dataclass
class Step:
    label: str            # short name, used by --from / --only
    module: str           # dotted module path, run via `python -m`
    fetch: bool = False   # slow raw-data fetch step — skipped by --skip-fetch
    pipeline: bool = False  # step is itself an orchestrator — forward --skip-fetch


def select_steps(steps: list[Step], from_step: str | None,
                 only: str | None, skip_fetch: bool) -> list[Step]:
    """Resolve --from / --only / --skip-fetch into the list of steps to run."""
    labels = [s.label for s in steps]
    if only is not None:
        if only not in labels:
            raise KeyError(only)
        chosen = [s for s in steps if s.label == only]
    elif from_step is not None:
        if from_step not in labels:
            raise KeyError(from_step)
        chosen = steps[labels.index(from_step):]
    else:
        chosen = list(steps)
    if skip_fetch:
        chosen = [s for s in chosen if not s.fetch]
    return chosen


def build_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--from", dest="from_step", metavar="STEP",
                   help="Start from this step, skipping earlier ones")
    p.add_argument("--only", metavar="STEP", help="Run only this step")
    p.add_argument("--skip-fetch", action="store_true",
                   help="Skip slow raw-data fetch steps")
    p.add_argument("--list", action="store_true", help="List steps and exit")
    return p


def run_pipeline(steps: list[Step], args: argparse.Namespace) -> int:
    """Execute the selected steps as subprocesses. Returns an exit code."""
    if args.list:
        for s in steps:
            tags = " ".join(t for t, on in
                            (("[fetch]", s.fetch), ("[pipeline]", s.pipeline)) if on)
            print(f"  {s.label:24s} {s.module}  {tags}".rstrip())
        return 0
    try:
        selected = select_steps(steps, args.from_step, args.only, args.skip_fetch)
    except KeyError as e:
        print(f"unknown step: {e.args[0]}", file=sys.stderr)
        return 2
    for s in selected:
        print(f"\n=== {s.label} ({s.module}) ===", flush=True)
        cmd = [sys.executable, "-m", s.module]
        if s.pipeline and args.skip_fetch:
            cmd.append("--skip-fetch")
        t0 = time.monotonic()
        result = subprocess.run(cmd)
        dt = time.monotonic() - t0
        if result.returncode != 0:
            print(f"FAILED: {s.label} (exit {result.returncode}, {dt:.0f}s)",
                  file=sys.stderr)
            return result.returncode
        print(f"--- {s.label} done in {dt:.0f}s", flush=True)
    return 0
```

- [ ] **Step 5: Run test, verify pass** — `uv run pytest tests/test_pipeline_runner.py -q` → PASS (6 tests).
- [ ] **Step 6: Commit** — `chore: scaffold src/pipeline stage folders + pipeline_runner helper`

---

## Phase 2 — Move shared modules to common/

### Task 2: Move params.py + repos.py → common/

**Files:** `git mv` `src/pipeline/params.py` `src/pipeline/repos.py` → `src/pipeline/common/`

- [ ] **Step 1:** Move the files:
```bash
cd /Users/kv/Dev/osendowment/model
git mv src/pipeline/params.py src/pipeline/common/params.py
git mv src/pipeline/repos.py src/pipeline/common/repos.py
```

- [ ] **Step 2:** Repoint every importer. `params` and `repos` are unique tokens — safe global sed:
```bash
grep -rl 'src\.pipeline\.params\|src\.pipeline\.repos' src/ tests/ scripts/ --include='*.py' \
  | grep -v __pycache__ \
  | xargs sed -i '' -e 's/src\.pipeline\.params/src.pipeline.common.params/g' \
                    -e 's/src\.pipeline\.repos/src.pipeline.common.repos/g'
```

- [ ] **Step 3:** `repos.py` itself imports `from src.pipeline.params import RISK_INPUT_CLASSES` — the sed above already fixed it. Verify:
```bash
grep -n 'import' src/pipeline/common/repos.py | grep params
```
Expected: `from src.pipeline.common.params import RISK_INPUT_CLASSES`

- [ ] **Step 4: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1`
Expected: `225 passed, 7 failed`. Also: `uv run python -c "from src.pipeline.common.repos import load_risk_repos; from src.pipeline.common.params import RISK_INPUT_CLASSES; print('ok')"` → `ok`.

- [ ] **Step 5: Commit** — `refactor: move params.py + repos.py to src/pipeline/common/`

---

## Phase 3 — Move risk modules to risk/

### Task 3: Move risk.py → risk/aggregate_risk.py

**Files:** `git mv` `src/pipeline/risk.py` → `src/pipeline/risk/aggregate_risk.py`

Order matters: rename `risk.py` **before** moving `build_*.py`, so the token
`src.pipeline.risk` is still unambiguous (only the old module, no `risk/` package contents yet).

- [ ] **Step 1:** `cd /Users/kv/Dev/osendowment/model && git mv src/pipeline/risk.py src/pipeline/risk/aggregate_risk.py`
- [ ] **Step 2:** Repoint importers of the old `risk` module:
```bash
grep -rl 'src\.pipeline\.risk' src/ tests/ scripts/ --include='*.py' | grep -v __pycache__ \
  | xargs sed -i '' -e 's/src\.pipeline\.risk\b/src.pipeline.risk.aggregate_risk/g'
```
- [ ] **Step 3:** Verify no over-rewrite — `grep -rn 'aggregate_risk\.aggregate_risk' src/ tests/` → expect no matches. If any, fix by hand.
- [ ] **Step 4: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`.
- [ ] **Step 5: Commit** — `refactor: move risk.py to src/pipeline/risk/aggregate_risk.py`

### Task 4: Move the six build_*.py → risk/

**Files:** `git mv` `build_concentration.py build_complexity.py build_security.py build_funding.py build_visibility.py build_workload.py` → `src/pipeline/risk/`

- [ ] **Step 1:** Move all six:
```bash
cd /Users/kv/Dev/osendowment/model
for f in build_concentration build_complexity build_security build_funding build_visibility build_workload; do
  git mv src/pipeline/$f.py src/pipeline/risk/$f.py
done
```
- [ ] **Step 2:** Repoint importers (each `build_<dim>` is a unique token):
```bash
grep -rl 'src\.pipeline\.build_concentration\|src\.pipeline\.build_complexity\|src\.pipeline\.build_security\|src\.pipeline\.build_funding\|src\.pipeline\.build_visibility\|src\.pipeline\.build_workload' src/ tests/ scripts/ --include='*.py' | grep -v __pycache__ \
  | xargs sed -i '' -E 's/src\.pipeline\.(build_(concentration|complexity|security|funding|visibility|workload))/src.pipeline.risk.\1/g'
```
- [ ] **Step 3:** `aggregate_risk.py` imports the builders' `INTERMEDIATES`? It reads CSVs, not builder modules — but verify: `grep -n 'build_' src/pipeline/risk/aggregate_risk.py`. Fix any stale path.
- [ ] **Step 4: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`. Then `uv run python -m src.pipeline.risk.aggregate_risk --random 3 --output /tmp/r.csv 2>&1 | tail -2` → writes 3 repos.
- [ ] **Step 5: Commit** — `refactor: move risk dimension builders to src/pipeline/risk/`

---

## Phase 4 — Move eligibility modules to eligibility/

### Task 5: Move eligibility.py + eol_common.py → eligibility/

**Files:** `git mv` `eligibility.py` → `src/pipeline/eligibility/classify_eligibility.py`, `eol_common.py` → `src/pipeline/eligibility/eol_common.py`

- [ ] **Step 1:** Move:
```bash
cd /Users/kv/Dev/osendowment/model
git mv src/pipeline/eligibility.py src/pipeline/eligibility/classify_eligibility.py
git mv src/pipeline/eol_common.py src/pipeline/eligibility/eol_common.py
```
- [ ] **Step 2:** Repoint importers. `eol_common` is unique; `eligibility` as a module token must become `eligibility.classify_eligibility` (the bare `src.pipeline.eligibility` only ever referred to the old module — the folder is new and nothing imports `src.pipeline.eligibility.X` yet):
```bash
grep -rl 'src\.pipeline\.eligibility\|src\.pipeline\.eol_common' src/ tests/ scripts/ --include='*.py' | grep -v __pycache__ \
  | xargs sed -i '' -e 's/src\.pipeline\.eol_common/src.pipeline.eligibility.eol_common/g' \
                    -e 's/src\.pipeline\.eligibility\b/src.pipeline.eligibility.classify_eligibility/g'
```
- [ ] **Step 3:** Fix over-rewrite: the eol_common sed runs first and yields `src.pipeline.eligibility.eol_common`; the second sed then turns it into `src.pipeline.eligibility.classify_eligibility.eol_common`. Repair:
```bash
grep -rl 'eligibility\.classify_eligibility\.eol_common' src/ tests/ scripts/ --include='*.py' | grep -v __pycache__ \
  | xargs sed -i '' -e 's/src\.pipeline\.eligibility\.classify_eligibility\.eol_common/src.pipeline.eligibility.eol_common/g'
```
- [ ] **Step 4:** Verify no double-rewrite remains — `grep -rn 'classify_eligibility\.classify_eligibility\|classify_eligibility\.eol_common' src/ tests/ scripts/` → no matches.
- [ ] **Step 5: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`. Then `uv run python -c "import src.pipeline.eligibility.classify_eligibility, src.pipeline.eligibility.eol_common; print('ok')"`.
- [ ] **Step 6: Commit** — `refactor: move eligibility + eol_common to src/pipeline/eligibility/`

---

## Phase 5 — Move + split value modules

### Task 6: Move the two standalone value steps → value/

**Files:** `git mv` `build_git.py` → `src/pipeline/value/build_git_urls.py`, `calculate_ecosystem_downloads.py` → `src/pipeline/value/build_ecosystem_downloads.py`

- [ ] **Step 1:** Move:
```bash
cd /Users/kv/Dev/osendowment/model
git mv src/pipeline/build_git.py src/pipeline/value/build_git_urls.py
git mv src/pipeline/calculate_ecosystem_downloads.py src/pipeline/value/build_ecosystem_downloads.py
```
- [ ] **Step 2:** Repoint importers (unique tokens):
```bash
grep -rl 'src\.pipeline\.build_git\|src\.pipeline\.calculate_ecosystem_downloads' src/ tests/ scripts/ --include='*.py' | grep -v __pycache__ \
  | xargs sed -i '' -e 's/src\.pipeline\.build_git\b/src.pipeline.value.build_git_urls/g' \
                    -e 's/src\.pipeline\.calculate_ecosystem_downloads/src.pipeline.value.build_ecosystem_downloads/g'
```
- [ ] **Step 3: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`.
- [ ] **Step 4: Commit** — `refactor: move build_git + ecosystem_downloads to src/pipeline/value/`

### Task 7: Split value.py → unify_value_data.py + verify_git_urls.py

`value.py` (970 lines): lines ~1-491 + ~931-970 are the collect/aggregate/write/display
core + `main()`; lines ~493-930 are the ls-remote git-URL verifier. Split them.

**Files:** `git mv` `value.py` → `src/pipeline/value/unify_value_data.py`; Create `src/pipeline/value/verify_git_urls.py`

- [ ] **Step 1:** `cd /Users/kv/Dev/osendowment/model && git mv src/pipeline/value.py src/pipeline/value/unify_value_data.py`
- [ ] **Step 2:** Read `unify_value_data.py` fully. Identify the verifier block — the `# ── Git URL verifier ──` section through `_print_git_validity_table`: functions `_host`, `_now_iso`, `_load_validity_cache`, `_save_validity_cache`, `_is_fresh`, `_lsremote_pass`, `_canonicalize_git_url`, `_verify_non_github`, `verify_urls_in_aggregates`, `_print_git_validity_table`, and the verifier constants (`GIT_VALIDITY_CACHE`, `GIT_URL_TTL_DAYS`, `LS_REMOTE_TIMEOUT`, `LSREMOTE_PARALLEL`, `_VALIDITY_FIELDS`, `_GITHUB_URL_RE`).
- [ ] **Step 3:** Create `src/pipeline/value/verify_git_urls.py` containing: the module docstring, the imports it needs (`argparse`, `csv`, `re`, `subprocess`, `concurrent.futures`, `pathlib.Path`, `rich`, plus `DATA_DIR`), every verifier function/constant moved out of `unify_value_data.py`, and a `main()` that: reads `data/value-data.csv` into a list of dicts, calls `verify_urls_in_aggregates(rows)`, prints `_print_git_validity_table`, and writes the rows back to `data/value-data.csv` (reuse `unify_value_data.write_value_data` via import, or an inline `csv.DictWriter` with the same `FIELDS`). Guard with `if __name__ == "__main__": main()`.
- [ ] **Step 4:** In `unify_value_data.py`: delete the moved verifier block; if its `main()` called `verify_urls_in_aggregates`, remove that call (verification is now a separate step) — `main()` ends at `write_value_data`. Remove now-unused imports.
- [ ] **Step 5:** Repoint importers of `src.pipeline.value`. Inspect each of the ~6 importers (`grep -rln 'src\.pipeline\.value\b' src tests scripts`) and repoint each symbol to whichever new module defines it (`unify_value_data` for `aggregate_by_repo`/`collect_ecosystem`/`FIELDS`/`CLASS_RANK`/`write_value_data`/readers; `verify_git_urls` for `verify_urls_in_aggregates`/`_canonicalize_git_url`/etc.). `tests/test_unify_value_data.py` is the main one — split its imports across the two modules.
- [ ] **Step 6: Verify** — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`. Then `uv run python -c "import src.pipeline.value.unify_value_data, src.pipeline.value.verify_git_urls; print('ok')"`.
- [ ] **Step 7: Commit** — `refactor: split value.py into unify_value_data + verify_git_urls`

---

## Phase 6 — Ecosystem orchestrators

### Task 8: Create the six ecosystem pipeline orchestrators

**Files (Create):** `src/pipeline/value/{npm,crates,pypi,debian,homebrew,cpp}_pipeline.py`

Each script's step list runs that ecosystem's existing `src/<eco>/` scripts. Before
writing, confirm each referenced module runs via `python -m` (has an
`if __name__ == "__main__"`); if a script needs a positional/sub-arg the Step
cannot express, note it in the commit message for follow-up.

- [ ] **Step 1:** `src/pipeline/value/npm_pipeline.py`:
```python
"""npm ecosystem pipeline — fetch raw npm data, then build value outputs."""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("fetch-data",  "src.npm.fetch_npm_data",      fetch=True),
    Step("fetch-stats", "src.npm.fetch_npm_stats",     fetch=True),
    Step("fetch-repos", "src.npm.fetch_nice_registry", fetch=True),
    Step("process",     "src.npm.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("npm ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2:** `src/pipeline/value/crates_pipeline.py` — same shape, `STEPS`:
```python
STEPS = [
    Step("fetch-db-dump",   "src.crates.fetch_db_dump",          fetch=True),
    Step("fetch-downloads", "src.crates.fetch_version_downloads", fetch=True),
    Step("process",         "src.crates.process_data"),
]
```
docstring `"""crates.io ecosystem pipeline."""`, `main()` description `"crates ecosystem pipeline"`.

- [ ] **Step 3:** `src/pipeline/value/pypi_pipeline.py` — `STEPS`:
```python
STEPS = [
    Step("fetch-data", "src.pypi.fetch_pypi_data", fetch=True),
    Step("fetch-urls", "src.pypi.fetch_pypi_urls", fetch=True),
    Step("process",    "src.pypi.process_data"),
]
```
docstring `"""PyPI ecosystem pipeline."""`, description `"pypi ecosystem pipeline"`.

- [ ] **Step 4:** `src/pipeline/value/debian_pipeline.py` — `STEPS`:
```python
STEPS = [
    Step("fetch-data", "src.debian.fetch_debian_data", fetch=True),
    Step("process",    "src.debian.process_data"),
]
```
docstring `"""Debian ecosystem pipeline."""`, description `"debian ecosystem pipeline"`.

- [ ] **Step 5:** `src/pipeline/value/homebrew_pipeline.py` — `STEPS`:
```python
STEPS = [
    Step("fetch-data", "src.homebrew.fetch_homebrew_data", fetch=True),
    Step("process",    "src.homebrew.process_data"),
]
```
docstring `"""Homebrew ecosystem pipeline."""`, description `"homebrew ecosystem pipeline"`.

- [ ] **Step 6:** `src/pipeline/value/cpp_pipeline.py` — composes debian + homebrew, then the cpp aggregation:
```python
"""C/C++ ecosystem pipeline — Debian + Homebrew sub-pipelines, then cpp aggregation."""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("debian",        "src.pipeline.value.debian_pipeline",   pipeline=True),
    Step("homebrew",      "src.pipeline.value.homebrew_pipeline",  pipeline=True),
    Step("fetch-repology", "src.cpp.fetch_repology_urls",          fetch=True),
    Step("process",       "src.cpp.process_data"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("cpp ecosystem pipeline").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Verify** — for each: `uv run python -m src.pipeline.value.<eco>_pipeline --list` prints the step table, exit 0. Run `uv run pytest tests/test_pipeline_runner.py -q` → still 6 passed.
- [ ] **Step 8: Commit** — `feat: add per-ecosystem value pipeline orchestrators`

---

## Phase 7 — Stage runners

### Task 9: Create the three stage runners

**Files (Create):** `src/pipeline/run_value_pipeline.py`, `src/pipeline/run_risk_pipeline.py`, `src/pipeline/run_eligibility_pipeline.py`

- [ ] **Step 1:** `src/pipeline/run_value_pipeline.py`:
```python
"""Value pipeline runner — ecosystem pipelines, then unify + verify."""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

STEPS = [
    Step("npm",       "src.pipeline.value.npm_pipeline",            fetch=True, pipeline=True),
    Step("crates",    "src.pipeline.value.crates_pipeline",         fetch=True, pipeline=True),
    Step("pypi",      "src.pipeline.value.pypi_pipeline",           fetch=True, pipeline=True),
    Step("cpp",       "src.pipeline.value.cpp_pipeline",            fetch=True, pipeline=True),
    Step("downloads", "src.pipeline.value.build_ecosystem_downloads"),
    Step("git-urls",  "src.pipeline.value.build_git_urls"),
    Step("unify",     "src.pipeline.value.unify_value_data"),
    Step("verify",    "src.pipeline.value.verify_git_urls"),
]


def main() -> int:
    return run_pipeline(STEPS, build_parser("value pipeline runner").parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
```
Note: ecosystem steps are `fetch=True` *and* `pipeline=True` — under `--skip-fetch` they still run (so `process_data` runs) but receive `--skip-fetch` so their own fetchers are skipped. To make `select_steps` keep a `fetch=True` step when it is also a pipeline, **update `select_steps`** in `pipeline_runner.py`: change the skip-fetch filter to `[s for s in chosen if s.pipeline or not s.fetch]`. Update `tests/test_pipeline_runner.py` `test_select_skip_fetch` to add a `Step("e", "m.e", fetch=True, pipeline=True)` and assert it survives. Re-run that test.

- [ ] **Step 2:** `src/pipeline/run_risk_pipeline.py` — builders + aggregate by default; `--with-fetchers` prepends the fetch stage:
```python
"""Risk pipeline runner — dimension builders + aggregation.

By default runs only the cheap projection (builders -> aggregate). Pass
--with-fetchers to also run the multi-hour data-collection fetchers first.
"""
import sys

from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    Step("commits-years", "src.git.commits_years",              fetch=True),
    Step("resolve-head",  "src.git.resolve_head",               fetch=True),
    Step("contributors",  "src.github.fetch_contributors_metrics", fetch=True),
    Step("issues",        "src.github.fetch_issue_metrics",     fetch=True),
    Step("scc",           "src.git.fetch_scc",                  fetch=True),
    Step("churn",         "src.github.fetch_churn",             fetch=True),
    Step("semgrep",       "src.github.fetch_semgrep",           fetch=True),
    Step("cognitive",     "src.github.fetch_cognitive",         fetch=True),
    Step("cves",          "src.osv.fetch_cves",                 fetch=True),
    Step("scorecard",     "src.openssf.scorecard",              fetch=True),
    Step("depsdev",       "src.depsdev.fetch",                  fetch=True),
    Step("funding",       "src.github.fetch_funding",           fetch=True),
]
BUILDERS = [
    Step("concentration", "src.pipeline.risk.build_concentration"),
    Step("complexity",    "src.pipeline.risk.build_complexity"),
    Step("security",      "src.pipeline.risk.build_security"),
    Step("funding-build", "src.pipeline.risk.build_funding"),
    Step("visibility",    "src.pipeline.risk.build_visibility"),
    Step("workload",      "src.pipeline.risk.build_workload"),
    Step("aggregate",     "src.pipeline.risk.aggregate_risk"),
]


def main() -> int:
    parser = build_parser("risk pipeline runner")
    parser.add_argument("--with-fetchers", action="store_true",
                        help="Also run the multi-hour data-collection fetchers first")
    args = parser.parse_args()
    steps = (FETCHERS + BUILDERS) if args.with_fetchers else BUILDERS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3:** `src/pipeline/run_eligibility_pipeline.py`:
```python
"""Eligibility pipeline runner — license/EOL/foundation checks, then classify.

Default runs the classification step on existing data. --with-fetchers
prepends the per-ecosystem license + EOL fetchers and the GitHub
repo-owner / foundation fetchers.
"""
from src.pipeline.common.pipeline_runner import Step, build_parser, run_pipeline

FETCHERS = [
    Step("npm-eol",       "src.npm.check_eol",          fetch=True),
    Step("crates-eol",    "src.crates.check_eol",       fetch=True),
    Step("pypi-eol",      "src.pypi.check_eol",         fetch=True),
    Step("cpp-eol",       "src.cpp.check_eol",          fetch=True),
    Step("npm-lic",       "src.npm.fetch_licenses",     fetch=True),
    Step("crates-lic",    "src.crates.fetch_licenses",  fetch=True),
    Step("pypi-lic",      "src.pypi.fetch_licenses",    fetch=True),
    Step("cpp-lic",       "src.cpp.fetch_licenses",     fetch=True),
    Step("homebrew-lic",  "src.homebrew.fetch_licenses", fetch=True),
    Step("osi",           "src.osi.fetch_licenses",     fetch=True),
    Step("repo-owner",    "src.github.fetch_repo_owner_data", fetch=True),
    Step("foundations",   "src.foundations.match_repos", fetch=True),
]
STEPS = [Step("classify", "src.pipeline.eligibility.classify_eligibility")]


def main() -> int:
    parser = build_parser("eligibility pipeline runner")
    parser.add_argument("--with-fetchers", action="store_true",
                        help="Also run the license / EOL / foundation fetchers first")
    args = parser.parse_args()
    steps = (FETCHERS + STEPS) if args.with_fetchers else STEPS
    return run_pipeline(steps, args)


if __name__ == "__main__":
    raise SystemExit(main())
```
Before committing, verify each fetcher module path in `FETCHERS` exists (`src/osi/fetch_licenses.py`, `src/foundations/match_repos.py`, `src/github/fetch_repo_owner_data.py` — some are currently untracked WIP). Drop any Step whose module does not exist and note it in the commit message.

- [ ] **Step 4: Verify** — `uv run python -m src.pipeline.run_value_pipeline --list`, `... run_risk_pipeline --list`, `... run_eligibility_pipeline --list` each print their step tables, exit 0. `uv run pytest tests/test_pipeline_runner.py -q` → passes.
- [ ] **Step 5: Commit** — `feat: add run_value/risk/eligibility_pipeline stage runners`

---

## Phase 8 — Verification & docs

### Task 10: Final verification + docs refresh

**Files:** Modify `docs/pipeline.md`, `docs/value.md`, `docs/risk.md`, `docs/eligibility.md`, `CLAUDE.md` (project), `src/pipeline/CLAUDE.md` if present

- [ ] **Step 1:** Confirm `src/pipeline/` top level is exactly: `__init__.py`, `settings.json`, `run_value_pipeline.py`, `run_risk_pipeline.py`, `run_eligibility_pipeline.py`, and the folders `common/ value/ risk/ eligibility/`. Run `ls src/pipeline/` and verify nothing else.
- [ ] **Step 2:** Full suite — `uv run pytest -q --continue-on-collection-errors 2>&1 | tail -1` → `225 passed, 7 failed`.
- [ ] **Step 3:** `uv run ruff check src/pipeline/ 2>&1 | tail -3` — no new F401/F811/F821 errors introduced by the move (pre-existing E702 etc. in moved files are acceptable; unused imports left by the value.py split are NOT — fix them).
- [ ] **Step 4:** Smoke the risk aggregator end to end on real data: `uv run python -m src.pipeline.run_risk_pipeline --list` then `uv run python -m src.pipeline.risk.aggregate_risk --random 5 --output /tmp/r.csv` → 5 repos written.
- [ ] **Step 5:** Update docs: replace every old module path (`src.pipeline.value`, `src.pipeline.risk`, `src/pipeline/params.json` already done, `src.pipeline.build_*`, `src.pipeline.eligibility`) in `docs/*.md` with the new paths and the new `uv run python -m src.pipeline.run_*_pipeline` invocations. Update the "Scripts" / "How to refresh" sections.
- [ ] **Step 6: Commit** — `docs: update pipeline paths for the src/pipeline restructure`

---

## Self-review notes

- Spec layout (common/value/risk/eligibility + 3 runners) → Tasks 1-9. Ecosystem orchestrators → Task 8. value.py 2-way split → Task 7. Import migration table → Tasks 2-7 sed commands. Testing invariant (225/7) → every task's verify step.
- `Step` / `select_steps` / `run_pipeline` / `build_parser` defined once in Task 1; used identically in Tasks 8-9.
- Ordering guards the `src.pipeline.risk` / `src.pipeline.eligibility` token ambiguity: rename the bare module before populating the folder (Task 3 before Task 4; Task 5 sed sequence + repair step).
- `select_steps` skip-fetch rule is revised in Task 9 Step 1 (pipeline steps survive `--skip-fetch`); its test is updated in the same step.
