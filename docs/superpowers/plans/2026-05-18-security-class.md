# Security Class Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-repo `security_class` (A–D) to `data/security.csv`, derived from the OpenSSF Scorecard score and the 5-year CVE count via the `geom_mean_quartile` method.

**Architecture:** Two risk percentiles (Hazen plotting position) — one over `cve_count_5y` (more = worse), one over `-openssf_score` (lower score = worse) — are combined with a geometric mean into `security_risk_percentile`, then bucketed into equal-count quartiles (A = worst 25%). The three generic helpers this needs (`hazen_percentiles`, `geometric_mean`, `quartile_classes`) already live inside `build_workload.py`; they are first extracted into a shared `src/pipeline/common/stats.py` so both builders import them.

**Tech Stack:** Python 3.11+, `uv` (run via `uv run`), `pytest`, `rich` for terminal output. Pure-stdlib statistics — no numpy/scipy.

**Spec:** `docs/superpowers/specs/2026-05-18-security-class-design.md`

**Conventions:**
- Run everything with `uv run` (never bare `python`).
- Every `git commit` must be authored as Konstantin Vinogradov — prefix each commit with `-c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov"`.
- Do **not** push. Commit locally only.
- Work happens on the current branch `feat/security-class`.

---

## Task 1: Extract shared stats helpers into `src/pipeline/common/stats.py`

Create the new shared module and its tests. This task is purely additive — `build_workload.py` keeps its own copies for now (removed in Task 2), so all existing tests stay green.

**Files:**
- Create: `src/pipeline/common/stats.py`
- Test: `tests/test_stats.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stats.py` with this exact content:

```python
"""Tests for src/pipeline/common/stats.py — shared geom_mean_quartile helpers."""

import pytest

from src.pipeline.common.stats import (
    geometric_mean,
    hazen_percentiles,
    quartile_classes,
)


class TestHazenPercentiles:
    def test_strictly_between_0_and_100(self):
        # Even the minimum value must be > 0 (so a geometric mean can't collapse).
        pctls = hazen_percentiles([5, 1, 9, 3])
        assert all(0 < p < 100 for p in pctls)

    def test_monotonic_with_value(self):
        # Higher input value -> higher percentile.
        pctls = hazen_percentiles([10, 20, 30, 40])
        assert pctls == sorted(pctls)
        assert pctls[0] < pctls[-1]

    def test_negative_values_rank_low(self):
        # A negative input must rank at the low end.
        pctls = hazen_percentiles([-5.0, 0.0, 10.0, 50.0])
        assert pctls[0] == min(pctls)

    def test_ties_share_average_rank(self):
        pctls = hazen_percentiles([7, 7, 7, 7])
        assert pctls[0] == pctls[1] == pctls[2] == pctls[3]
        assert pctls[0] == pytest.approx(50.0)  # 100*(2.5-0.5)/4

    def test_empty(self):
        assert hazen_percentiles([]) == []


class TestGeometricMean:
    def test_known_value(self):
        assert geometric_mean([4.0, 9.0]) == pytest.approx(6.0)

    def test_three_values(self):
        assert geometric_mean([8.0, 8.0, 8.0]) == pytest.approx(8.0)

    def test_empty(self):
        assert geometric_mean([]) == 0.0


class TestQuartileClasses:
    def test_equal_count_split(self):
        # 8 distinct scores -> 2 per class, A = highest.
        classes = quartile_classes([1, 2, 3, 4, 5, 6, 7, 8])
        assert classes == ["D", "D", "C", "C", "B", "B", "A", "A"]

    def test_remainder_within_one(self):
        # 10 scores -> each class holds 2 or 3.
        from collections import Counter
        classes = quartile_classes(list(range(10)))
        counts = Counter(classes)
        assert all(c in (2, 3) for c in counts.values())
        assert set(counts) == {"A", "B", "C", "D"}

    def test_highest_score_is_class_a(self):
        classes = quartile_classes([10, 99, 50, 1])
        assert classes[1] == "A"  # 99 is the highest

    def test_empty(self):
        assert quartile_classes([]) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_stats.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.common.stats'`

- [ ] **Step 3: Create the shared module**

Create `src/pipeline/common/stats.py` with this exact content:

```python
"""Shared statistics helpers for the geom_mean_quartile classifiers.

`build_workload.py` and `build_security.py` both classify repos A-D by
percentile-ranking metrics, taking a geometric mean of the percentiles,
and bucketing the result into equal-count quartiles. These three pure
functions are the shared primitives of that method.
"""


def hazen_percentiles(values: list[float]) -> list[float]:
    """Percentile-rank each value via the Hazen plotting position.

    pct = 100 * (rank - 0.5) / n, with tied values sharing the average of
    their ranks. The result is strictly within (0, 100) - never exactly 0
    or 100 - so a geometric mean taken over these percentiles cannot
    collapse to 0. Higher value -> higher percentile. Empty input -> [].
    """
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    pctls = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, tie-averaged
        pct = 100.0 * (avg_rank - 0.5) / n
        for k in range(i, j + 1):
            pctls[indexed[k][0]] = pct
        i = j + 1
    return pctls


def geometric_mean(values: list[float]) -> float:
    """Geometric mean (prod v)^(1/n). Assumes every value > 0; [] -> 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def quartile_classes(scores: list[float]) -> list[str]:
    """Assign A/B/C/D by equal-count quartiles of `scores` (higher = worse).

    Sorted descending, the highest-scoring 25% get 'A', then 'B', 'C', 'D'.
    When n is not divisible by 4 each class holds floor(n/4) or ceil(n/4)
    members. Empty input -> [].
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    labels = ["A", "B", "C", "D"]
    out = [""] * n
    for p, idx in enumerate(order):  # p: 0-based rank, 0 = highest score
        out[idx] = labels[min(3, p * 4 // n)]
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_stats.py -v`
Expected: PASS — all 12 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/common/stats.py tests/test_stats.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor: add shared geom_mean_quartile stats helpers

Extracts hazen_percentiles / geometric_mean / quartile_classes into
src/pipeline/common/stats.py so build_workload and the upcoming
build_security security_class can share them. build_workload still
carries its own copies until the next commit."
```

---

## Task 2: Rewire `build_workload.py` to import from `common.stats`

Remove the three now-duplicated helpers from `build_workload.py`, import them from `common.stats`, and migrate their tests (those tests now live in `tests/test_stats.py`).

**Files:**
- Modify: `src/pipeline/risk/build_workload.py`
- Modify: `tests/test_build_workload.py`

- [ ] **Step 1: Add the import to `build_workload.py`**

In `src/pipeline/risk/build_workload.py`, replace this line:

```python
from src.pipeline.common.repos import load_risk_repos
```

with:

```python
from src.pipeline.common.repos import load_risk_repos
from src.pipeline.common.stats import (
    geometric_mean,
    hazen_percentiles,
    quartile_classes,
)
```

- [ ] **Step 2: Delete the three local helper definitions**

In `src/pipeline/risk/build_workload.py`, delete the three function definitions `_hazen_percentiles`, `_geometric_mean`, and `_quartile_classes` — this exact block (it sits between the `FIELDS = [...]` list and `def compute_workload_classes`):

```python
def _hazen_percentiles(values: list[float]) -> list[float]:
    """Percentile-rank each value via the Hazen plotting position.

    pct = 100 * (rank - 0.5) / n, with tied values sharing the average of
    their ranks. The result is strictly within (0, 100) — never exactly 0
    or 100 — so a geometric mean taken over these percentiles cannot
    collapse to 0. Higher value → higher percentile. Empty input → [].
    """
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    pctls = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based, tie-averaged
        pct = 100.0 * (avg_rank - 0.5) / n
        for k in range(i, j + 1):
            pctls[indexed[k][0]] = pct
        i = j + 1
    return pctls


def _geometric_mean(values: list[float]) -> float:
    """Geometric mean (∏ v)^(1/n). Assumes every value > 0; [] → 0.0."""
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= v
    return product ** (1.0 / len(values))


def _quartile_classes(scores: list[float]) -> list[str]:
    """Assign A/B/C/D by equal-count quartiles of `scores` (higher = worse).

    Sorted descending, the highest-scoring 25% get 'A', then 'B', 'C', 'D'.
    When n is not divisible by 4 each class holds ⌊n/4⌋ or ⌈n/4⌉ members.
    Empty input → [].
    """
    n = len(scores)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: scores[i], reverse=True)
    labels = ["A", "B", "C", "D"]
    out = [""] * n
    for p, idx in enumerate(order):  # p: 0-based rank, 0 = highest score
        out[idx] = labels[min(3, p * 4 // n)]
    return out
```

After deletion, exactly **two blank lines** must separate the closing `]` of `FIELDS = [...]` from `def compute_workload_classes(`.

- [ ] **Step 3: Update the call sites inside `compute_workload_classes`**

In `src/pipeline/risk/build_workload.py`, replace this block:

```python
    loc_p = _hazen_percentiles([c["loc_per_ac"] for c in classifiable])
    cve_p = _hazen_percentiles([c["cve_per_ac"] for c in classifiable])
    nni_p = _hazen_percentiles([c["nni_per_ac"] for c in classifiable])

    # 3. Geometric mean of the three percentiles → burden score.
    burden = [_geometric_mean([loc_p[i], cve_p[i], nni_p[i]])
              for i in range(len(classifiable))]

    # 4. Equal-count quartile class (A = highest burden).
    classes = _quartile_classes(burden)
```

with:

```python
    loc_p = hazen_percentiles([c["loc_per_ac"] for c in classifiable])
    cve_p = hazen_percentiles([c["cve_per_ac"] for c in classifiable])
    nni_p = hazen_percentiles([c["nni_per_ac"] for c in classifiable])

    # 3. Geometric mean of the three percentiles → burden score.
    burden = [geometric_mean([loc_p[i], cve_p[i], nni_p[i]])
              for i in range(len(classifiable))]

    # 4. Equal-count quartile class (A = highest burden).
    classes = quartile_classes(burden)
```

- [ ] **Step 4: Trim `tests/test_build_workload.py`**

The helper tests now live in `tests/test_stats.py`. Replace the entire contents of `tests/test_build_workload.py` with this exact content (the `TestComputeWorkloadClasses` class is kept verbatim; the three helper test classes and the helper imports are dropped):

```python
"""Tests for src/pipeline/risk/build_workload.py — workload class logic."""

import pytest

from src.pipeline.risk.build_workload import compute_workload_classes


class TestComputeWorkloadClasses:
    @staticmethod
    def _metric(repo, loc, cve, nni, ac):
        return {"repo": repo, "loc": loc, "cve": cve, "nni": nni, "ac": ac}

    def test_classifies_repos_with_all_inputs(self):
        metrics = [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i), ac=2.0)
            for i in range(1, 9)
        ]
        out = compute_workload_classes(metrics)
        assert set(out) == {f"r/{i}" for i in range(1, 9)}
        # r/8 carries the most burden per contributor → class A.
        assert out["r/8"]["workload_class"] == "A"
        assert out["r/1"]["workload_class"] == "D"

    def test_missing_cve_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=None, nni=5.0, ac=2.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
            self._metric("r/c", loc=3000.0, cve=4.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""
        assert out["r/a"]["loc_per_ac"] == ""

    def test_zero_ac_yields_empty_class(self):
        metrics = [
            self._metric("r/a", loc=1000.0, cve=3.0, nni=5.0, ac=0.0),
            self._metric("r/b", loc=2000.0, cve=3.0, nni=5.0, ac=2.0),
        ]
        out = compute_workload_classes(metrics)
        assert out["r/a"]["workload_class"] == ""

    def test_negative_nni_still_classified(self):
        # A repo closing issues faster than it opens them (negative NNI)
        # must still receive a class — it just lands in the low-burden tail.
        metrics = [
            self._metric("r/neg", loc=500.0, cve=0.0, nni=-40.0, ac=4.0),
        ] + [
            self._metric(f"r/{i}", loc=i * 1000.0, cve=float(i), nni=float(i * 10), ac=4.0)
            for i in range(1, 8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/neg"]["workload_class"] != ""
        assert out["r/neg"]["nni_per_ac"] == pytest.approx(-10.0)

    def test_ratios_rounded(self):
        metrics = [
            self._metric(f"r/{i}", loc=1000.0, cve=2.0, nni=4.0, ac=3.0)
            for i in range(8)
        ]
        out = compute_workload_classes(metrics)
        assert out["r/0"]["loc_per_ac"] == pytest.approx(333.3333, abs=1e-3)
```

- [ ] **Step 5: Run the affected tests to verify they pass**

Run: `uv run pytest tests/test_stats.py tests/test_build_workload.py -v`
Expected: PASS — `test_stats.py` (12 tests) and `test_build_workload.py` (5 tests) all green.

- [ ] **Step 6: Verify the module still imports cleanly**

Run: `uv run python -c "import src.pipeline.risk.build_workload; print('ok')"`
Expected: prints `ok` (no `NameError` for the renamed call sites).

- [ ] **Step 7: Commit**

```bash
git add src/pipeline/risk/build_workload.py tests/test_build_workload.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor: build_workload imports stats helpers from common.stats

Drops the three local copies of hazen_percentiles / geometric_mean /
quartile_classes; their unit tests move to tests/test_stats.py."
```

---

## Task 3: Replace the dormant `security` config block

`settings.json` carries a never-wired score-bucket `risk_classification.security` block, and `params.py` loads it as `SECURITY_THRESHOLDS` (read by nothing). Replace the block with a parameter-free `geom_mean_quartile` descriptor and drop the unused constant.

**Files:**
- Modify: `src/pipeline/settings.json`
- Modify: `src/pipeline/common/params.py`

- [ ] **Step 1: Replace the `security` block in `settings.json`**

In `src/pipeline/settings.json`, replace this block:

```json
    "security": {
      "A": 3.0,
      "B": 5.0,
      "C": 7.0,
      "comment": "Buckets the OpenSSF Scorecard score (0-10). A: score ≤ 3 (critical hygiene); B: ≤ 5; C: ≤ 7; D: > 7. Empty when no scorecard available."
    },
```

with:

```json
    "security": {
      "method": "geom_mean_quartile",
      "metrics": ["openssf_score", "cve_count_5y"],
      "comment": "security_class = equal-count quartiles (A = worst 25%) of the geometric mean of Hazen risk-percentiles of openssf_score (inverted — lower score ranks higher-risk) and cve_count_5y. Parameter-free; no numeric thresholds. Empty when openssf_score or cve_count_5y is missing."
    },
```

- [ ] **Step 2: Remove `SECURITY_THRESHOLDS` from `params.py`**

In `src/pipeline/common/params.py`, replace this block:

```python
ISSUE_TREND_THRESHOLDS: dict = _P["risk_classification"]["issue_trend"]
SECURITY_THRESHOLDS: dict = _P["risk_classification"]["security"]
FUNDING_THRESHOLDS: dict = _P["risk_classification"]["funding"]
```

with:

```python
ISSUE_TREND_THRESHOLDS: dict = _P["risk_classification"]["issue_trend"]
FUNDING_THRESHOLDS: dict = _P["risk_classification"]["funding"]
```

- [ ] **Step 3: Verify the JSON parses, `params` imports, and nothing referenced the dropped constant**

Run: `uv run python -c "import json; json.load(open('src/pipeline/settings.json')); print('json ok')"`
Expected: prints `json ok`

Run: `uv run python -c "from src.pipeline.common import params; print('params ok')"`
Expected: prints `params ok`

Run: `grep -rn "SECURITY_THRESHOLDS" src/ tests/`
Expected: no output (the constant is referenced nowhere).

- [ ] **Step 4: Commit**

```bash
git add src/pipeline/settings.json src/pipeline/common/params.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "refactor: replace dormant security score-bucket config

The risk_classification.security block was a never-wired A<=3/B<=5/C<=7
score bucket; params.SECURITY_THRESHOLDS was loaded but unused. Replace
with the parameter-free geom_mean_quartile descriptor security_class
will use."
```

---

## Task 4: Add `compute_security_classes` to `build_security.py` (TDD)

Add the security-class computation as a standalone, tested function. It is not yet wired into `build()` — that happens in Task 5.

**Files:**
- Modify: `src/pipeline/risk/build_security.py`
- Test: `tests/test_build_security.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_security.py` with this exact content:

```python
"""Tests for src/pipeline/risk/build_security.py — security class logic."""

import pytest

from src.pipeline.risk.build_security import compute_security_classes


def _metric(repo, openssf_score, cve):
    return {"repo": repo, "openssf_score": openssf_score, "cve": cve}


class TestComputeSecurityClasses:
    def test_classifies_repos_with_both_inputs(self):
        # 8 repos worsening on both axes -> r/8 worst, r/1 best.
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i, cve=float(i))
            for i in range(1, 9)
        ]
        out = compute_security_classes(metrics)
        assert set(out) == {f"r/{i}" for i in range(1, 9)}
        assert out["r/8"]["security_class"] == "A"   # lowest score + most CVEs
        assert out["r/1"]["security_class"] == "D"   # highest score + fewest CVEs

    def test_openssf_score_is_inverted(self):
        # The repo with the LOWEST openssf_score gets the HIGHEST risk pctl.
        metrics = [
            _metric("r/low", openssf_score=1.0, cve=0.0),
            _metric("r/mid", openssf_score=5.0, cve=0.0),
            _metric("r/high", openssf_score=9.0, cve=0.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/low"]["openssf_risk_pctl"] > out["r/high"]["openssf_risk_pctl"]

    def test_cve_zero_mass_shares_one_percentile(self):
        # Every repo with cve == 0 must receive an identical cve_risk_pctl;
        # a repo carrying CVEs ranks strictly higher-risk than that block.
        metrics = [_metric(f"r/{i}", openssf_score=5.0, cve=0.0) for i in range(6)]
        metrics.append(_metric("r/withcve", openssf_score=5.0, cve=10.0))
        out = compute_security_classes(metrics)
        zero_pctls = {out[f"r/{i}"]["cve_risk_pctl"] for i in range(6)}
        assert len(zero_pctls) == 1
        assert out["r/withcve"]["cve_risk_pctl"] > zero_pctls.pop()

    def test_needs_both_axes_weak_for_class_a(self):
        # A repo terrible on only ONE axis (great score, many CVEs) must
        # NOT land in A — the geometric mean keeps it out of the worst
        # quartile.
        metrics = [_metric("r/mixed", openssf_score=10.0, cve=150.0)] + [
            _metric(f"r/{i}", openssf_score=10.0 - i * 0.5, cve=float(i))
            for i in range(1, 12)
        ]
        out = compute_security_classes(metrics)
        assert out["r/mixed"]["security_class"] != "A"

    def test_missing_openssf_score_yields_empty(self):
        metrics = [
            _metric("r/a", openssf_score=None, cve=3.0),
            _metric("r/b", openssf_score=5.0, cve=3.0),
            _metric("r/c", openssf_score=6.0, cve=4.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/a"]["security_class"] == ""
        assert out["r/a"]["openssf_risk_pctl"] == ""
        assert out["r/a"]["cve_risk_pctl"] == ""
        assert out["r/a"]["security_risk_percentile"] == ""

    def test_missing_cve_yields_empty(self):
        metrics = [
            _metric("r/a", openssf_score=5.0, cve=None),
            _metric("r/b", openssf_score=5.0, cve=3.0),
            _metric("r/c", openssf_score=6.0, cve=4.0),
        ]
        out = compute_security_classes(metrics)
        assert out["r/a"]["security_class"] == ""

    def test_equal_count_quartiles(self):
        from collections import Counter
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i * 0.2, cve=float(i))
            for i in range(20)
        ]
        out = compute_security_classes(metrics)
        counts = Counter(out[f"r/{i}"]["security_class"] for i in range(20))
        assert counts == {"A": 5, "B": 5, "C": 5, "D": 5}

    def test_composite_is_geometric_mean_of_the_two_pctls(self):
        metrics = [
            _metric(f"r/{i}", openssf_score=10.0 - i, cve=float(i))
            for i in range(1, 9)
        ]
        out = compute_security_classes(metrics)
        r = out["r/4"]
        expected = (r["openssf_risk_pctl"] * r["cve_risk_pctl"]) ** 0.5
        assert r["security_risk_percentile"] == pytest.approx(expected, abs=0.01)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_build_security.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_security_classes'`

- [ ] **Step 3: Add the stats import to `build_security.py`**

In `src/pipeline/risk/build_security.py`, replace this line:

```python
from src.pipeline.common.repos import canonical_repo_map, load_risk_repos
```

with:

```python
from src.pipeline.common.repos import canonical_repo_map, load_risk_repos
from src.pipeline.common.stats import (
    geometric_mean,
    hazen_percentiles,
    quartile_classes,
)
```

- [ ] **Step 4: Add `_to_float` and `compute_security_classes`**

In `src/pipeline/risk/build_security.py`, insert these two functions immediately **before** `def build() -> list[dict]:` (i.e. after `_load_bestpractices_badge` and its blank lines):

```python
def _to_float(value: str) -> float | None:
    """Parse a CSV cell to float; blank or unparseable → None."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compute_security_classes(metrics: list[dict]) -> dict[str, dict]:
    """Compute per-repo security risk percentiles and the A–D class.

    `metrics` — one dict per repo with keys `repo`, `openssf_score`, and
    `cve` (the 5-year CVE count). `openssf_score` and `cve` are floats or
    None (None = the underlying metric is missing).

    Returns {repo: {...}} with these keys per repo:
        openssf_risk_pctl, cve_risk_pctl,
        security_risk_percentile, security_class
    A repo is classified only when BOTH `openssf_score` and `cve` are
    present; otherwise every value is the empty string "".

    Both axes are risk percentiles (higher = worse security): `cve` is
    ranked directly (more CVEs → higher), `openssf_score` is ranked
    negated (lower score → higher risk — the OpenSSF "higher = better"
    convention flipped to the pipeline's "A = worst"). The composite
    `security_risk_percentile` is their geometric mean; `security_class`
    is its equal-count quartile (A = worst 25%).
    """
    keys = ("openssf_risk_pctl", "cve_risk_pctl",
            "security_risk_percentile", "security_class")
    out: dict[str, dict] = {m["repo"]: {k: "" for k in keys} for m in metrics}

    # 1. Keep only repos with both inputs present.
    classifiable: list[dict] = []
    for m in metrics:
        score, cve = m["openssf_score"], m["cve"]
        if score is None or cve is None:
            continue
        classifiable.append({"repo": m["repo"], "score": score, "cve": cve})
    if not classifiable:
        return out

    # 2. Hazen risk-percentile each axis across the classifiable set.
    #    openssf_score is negated so a LOWER score → HIGHER risk percentile.
    openssf_p = hazen_percentiles([-c["score"] for c in classifiable])
    cve_p = hazen_percentiles([c["cve"] for c in classifiable])

    # 3. Geometric mean of the two risk percentiles → security risk score.
    risk = [geometric_mean([openssf_p[i], cve_p[i]])
            for i in range(len(classifiable))]

    # 4. Equal-count quartile class (A = highest risk).
    classes = quartile_classes(risk)

    # 5. Emit.
    for i, c in enumerate(classifiable):
        out[c["repo"]] = {
            "openssf_risk_pctl": round(openssf_p[i], 2),
            "cve_risk_pctl": round(cve_p[i], 2),
            "security_risk_percentile": round(risk[i], 2),
            "security_class": classes[i],
        }
    return out
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_build_security.py -v`
Expected: PASS — all 8 tests green.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline/risk/build_security.py tests/test_build_security.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: add compute_security_classes (geom-mean quartile of OpenSSF + CVE)

Pure classifier: two Hazen risk-percentiles (cve_count_5y direct,
openssf_score inverted), geometric mean, equal-count quartiles
(A = worst 25%). Not yet wired into build() — next commit."
```

---

## Task 5: Wire `security_class` into `build_security.py`

Emit the four new columns from `build()`, add them to the CSV schema, print a class-distribution table, and document the change.

**Files:**
- Modify: `src/pipeline/risk/build_security.py`

- [ ] **Step 1: Add the four columns to `FIELDS`**

In `src/pipeline/risk/build_security.py`, replace:

```python
FIELDS = [
    "repo", "repo_id",
    "openssf_score", "openssf_score_source",
    "cve_count_5y", "ossfuzz_enrolled",
    "sast_findings_total", "sast_findings_error", "sast_findings_security",
    "bestpractices_badge_id",
    "fetched_at",
]
```

with:

```python
FIELDS = [
    "repo", "repo_id",
    "openssf_score", "openssf_score_source",
    "cve_count_5y", "ossfuzz_enrolled",
    "sast_findings_total", "sast_findings_error", "sast_findings_security",
    "bestpractices_badge_id",
    "openssf_risk_pctl", "cve_risk_pctl",
    "security_risk_percentile", "security_class",
    "fetched_at",
]
```

- [ ] **Step 2: Add the second pass to `build()`**

In `src/pipeline/risk/build_security.py`, in `build()`, replace the end of the function:

```python
            "bestpractices_badge_id": badges.get(repo, ""),
            "fetched_at": ossf_checked_at,
        })
    return rows
```

with:

```python
            "bestpractices_badge_id": badges.get(repo, ""),
            "fetched_at": ossf_checked_at,
        })

    # Second pass — security_class needs population-wide percentile
    # ranking, so it is computed after every row's raw metrics are set.
    metrics = [
        {
            "repo": r["repo"],
            "openssf_score": _to_float(r["openssf_score"]),
            "cve": _to_float(r["cve_count_5y"]),
        }
        for r in rows
    ]
    classes = compute_security_classes(metrics)
    for r in rows:
        r.update(classes[r["repo"]])

    return rows
```

- [ ] **Step 3: Add the Security-class table to `main()`**

In `src/pipeline/risk/build_security.py`, in `main()`, replace:

```python
    enrolled = sum(1 for r in rows if r["ossfuzz_enrolled"] == "True")
    console.print(f"\n[dim]OSS-Fuzz enrolled: {enrolled:,} / {total:,}[/dim]")
    console.print(f"[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")
```

with:

```python
    enrolled = sum(1 for r in rows if r["ossfuzz_enrolled"] == "True")
    console.print(f"\n[dim]OSS-Fuzz enrolled: {enrolled:,} / {total:,}[/dim]")

    # Security-class distribution (A/B/C/D, or — when unclassifiable).
    cls = Counter(r["security_class"] or "—" for r in rows)
    ctable = Table(title="\n[bold]Security class[/bold]",
                   show_header=True, header_style="bold dim", padding=(0, 1))
    ctable.add_column("Class", style="bold")
    ctable.add_column("Repos", justify="right")
    for c in ("A", "B", "C", "D", "—"):
        if cls.get(c):
            ctable.add_row(c, f"{cls[c]:,}")
    console.print(ctable)

    console.print(f"[dim]Wrote {total:,} rows → {OUTPUT_FILE}[/dim]")
```

- [ ] **Step 4: Document the new columns in the module docstring**

In `src/pipeline/risk/build_security.py`, in the module docstring, replace:

```
        bestpractices_badge_id,             ([2026], "passing"/"silver"/"gold"/
                                            "in_progress"/"" if not enrolled)
        fetched_at                          (checked_at of openssf row used)
```

with:

```
        bestpractices_badge_id,             ([2026], "passing"/"silver"/"gold"/
                                            "in_progress"/"" if not enrolled)
        openssf_risk_pctl,                  (0–100; Hazen percentile of
                                            openssf_score, inverted — lower
                                            score ranks higher-risk)
        cve_risk_pctl,                      (0–100; Hazen percentile of
                                            cve_count_5y — more CVEs higher)
        security_risk_percentile,           (geometric mean of the two pctls)
        security_class,                     (A–D equal-count quartile of
                                            security_risk_percentile, A =
                                            worst; "" if openssf_score or
                                            cve_count_5y missing)
        fetched_at                          (checked_at of openssf row used)
```

- [ ] **Step 5: Document the method in the module docstring**

In `src/pipeline/risk/build_security.py`, in the module docstring, replace:

```
fall back to any sha present in the long file for that repo (deterministic
lexicographic pick).

Usage:
```

with:

```
fall back to any sha present in the long file for that repo (deterministic
lexicographic pick).

Security class
--------------
`security_class` (A–D) is the equal-count quartile of a composite
`security_risk_percentile` — the geometric mean of two Hazen risk
percentiles: one over `cve_count_5y` (more CVEs → higher) and one over
`-openssf_score` (lower Scorecard score → higher). A = worst 25%. A repo
is classified only when both `openssf_score` and `cve_count_5y` are
present.

~78% of risk-scope repos have zero CVEs and thus share one identical
`cve_risk_pctl`; for them the class effectively tracks the OpenSSF axis,
with the CVE axis only re-ranking the minority that carry CVEs.

Usage:
```

- [ ] **Step 6: Run the build on real data to verify**

Run: `uv run python -m src.pipeline.risk.build_security`
Expected: completes without error; prints the existing coverage table **and** a new `Security class` table with four rows `A`/`B`/`C`/`D` of roughly equal size (~225 repos each) plus a small `—` row for repos missing a metric.

Run: `head -1 data/security.csv`
Expected: header ends with `...,bestpractices_badge_id,openssf_risk_pctl,cve_risk_pctl,security_risk_percentile,security_class,fetched_at`

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest tests/test_stats.py tests/test_build_workload.py tests/test_build_security.py tests/test_build_funding.py -v`
Expected: PASS — all tests green.

- [ ] **Step 8: Commit (code only — not the regenerated CSV)**

```bash
git add src/pipeline/risk/build_security.py
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "feat: emit security_class in security.csv

build_security runs a second pass after collecting raw metrics:
compute_security_classes adds openssf_risk_pctl, cve_risk_pctl,
security_risk_percentile, security_class. main() prints the A/B/C/D
distribution. aggregate_risk auto-joins the new columns — no change
needed there."
```

---

## Task 6: Document the security class in `docs/risk.md`

**Files:**
- Modify: `docs/risk.md`

- [ ] **Step 1: Add the Security Class subsection**

In `docs/risk.md`, find the line `## Data Sources`. Immediately **before** it (after the existing `### Workload Class` subsection and its trailing blank line), insert:

```markdown
### Security Class

Combines the two OpenSSF-rooted security signals into a single A–D tier
using the same method as the workload class. Two risk percentiles are
formed (▴ higher = worse security):

- `openssf_risk_pctl` — percentile of `openssf_score`, **inverted**: a
  lower Scorecard score ranks as higher risk.
- `cve_risk_pctl` — percentile of `cve_count_5y`: more known CVEs ranks
  as higher risk.

Each axis is percentile-ranked across the classified set (Hazen position
`100·(rank−0.5)/n`, strictly in 0–100); `security_risk_percentile` is the
geometric mean of the two. A repo scores high only when bad on **both**
axes. The class is its equal-count quartile:

| Class | Label | Criteria |
|-------|-------|----------|
| **A** | critical | top 25% of `security_risk_percentile` |
| **B** | high | next 25% |
| **C** | moderate | next 25% |
| **D** | healthy | bottom 25% |
| _empty_ | no signal | `openssf_score` or `cve_count_5y` missing |

~78% of risk-scope repos have zero known CVEs and so share one identical
`cve_risk_pctl`; for those repos the class is effectively driven by the
OpenSSF Scorecard axis, with the CVE axis only re-ranking the minority
that carry CVEs.

```

- [ ] **Step 2: Add the four columns to the `risk-data.csv` output table**

In `docs/risk.md`, find the `risk-data.csv` output-column table and its last row:

```
| `workload_class` | A–D equal-count quartile of `workload_burden_percentile` (A = worst); empty when an input is missing |
```

Append immediately after it:

```
| `openssf_risk_pctl` | Hazen percentile (0–100) of `openssf_score`, inverted (lower score → higher risk) |
| `cve_risk_pctl` | Hazen percentile (0–100) of `cve_count_5y` (more CVEs → higher risk) |
| `security_risk_percentile` | Geometric mean of `openssf_risk_pctl` and `cve_risk_pctl` |
| `security_class` | A–D equal-count quartile of `security_risk_percentile` (A = worst); empty when `openssf_score` or `cve_count_5y` is missing |
```

- [ ] **Step 3: Commit**

```bash
git add docs/risk.md
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "docs: document the security class in risk.md"
```

---

## Task 7: Regenerate `data/security.csv` with the new columns

Commit the regenerated data file so the checked-in `security.csv` matches the new code.

**Files:**
- Modify: `data/security.csv`

- [ ] **Step 1: Regenerate the file**

Run: `uv run python -m src.pipeline.risk.build_security`
Expected: completes; prints the `Security class` table.

- [ ] **Step 2: Sanity-check the result**

Run: `uv run python -c "import csv; rows=list(csv.DictReader(open('data/security.csv'))); from collections import Counter; print(Counter(r['security_class'] or '—' for r in rows))"`
Expected: a `Counter` whose `A`/`B`/`C`/`D` counts differ by at most 1 from each other (equal-count quartiles), plus a small `—` bucket; all counts sum to the risk-scope total (~899).

- [ ] **Step 3: Commit only `data/security.csv`**

Other data files may carry unrelated uncommitted edits — stage **only** `data/security.csv`:

```bash
git add data/security.csv
git -c user.email=kv@kvinogradov.com -c user.name="Konstantin Vinogradov" commit -m "data: add security_class columns to security.csv

risk-data.csv will pick up the four new columns on the next full
run_risk_pipeline (aggregate_risk auto-joins them)."
```

---

## Verification Checklist

After all tasks:

- [ ] `uv run pytest tests/test_stats.py tests/test_build_workload.py tests/test_build_security.py -v` — all green.
- [ ] `grep -rn "SECURITY_THRESHOLDS" src/ tests/` — no output.
- [ ] `grep -rn "_hazen_percentiles\|_geometric_mean\|_quartile_classes" src/` — no output (all renamed to the public `common.stats` names).
- [ ] `head -1 data/security.csv` ends with `openssf_risk_pctl,cve_risk_pctl,security_risk_percentile,security_class,fetched_at`.
- [ ] `uv run python -m src.pipeline.risk.build_security` prints a `Security class` table summing to 899.
