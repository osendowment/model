# Design: security class

**Date:** 2026-05-18
**Status:** approved (design), pending implementation plan

## Motivation

The risk pipeline classifies repos A–D on several dimensions —
concentration, complexity, issue debt, funding, and (planned) workload.
The **Security** dimension produces raw metrics in `data/security.csv`
but **no class**.

This work adds a `security_class`: a per-repo A–D tier built from the two
OpenSSF-rooted security signals already in `security.csv` — the OpenSSF
Scorecard score and the 5-year CVE count — using the same
`geom_mean_quartile` method as the planned `workload_class`.

`settings.json` already carries a dormant `risk_classification.security`
block (a score-bucket `A≤3 / B≤5 / C≤7` scheme that was never wired into
any builder, and `params.py:SECURITY_THRESHOLDS` that nothing reads).
That dormant config is **superseded** by this design.

## Method

`security_class` mirrors `workload_class` exactly — the same
`geom_mean_quartile` shape, with two axes instead of three.

Both axes are **risk percentiles** (higher = worse security), so the
geometric mean is a risk score and a repo reaches the worst class only
when it is bad on *both* axes — one extreme axis alone cannot dominate.

### Inputs

Both columns already exist in `data/security.csv` — `build_security.py`
needs no new file reads:

| Axis | Column | Direction |
|---|---|---|
| OpenSSF Scorecard | `openssf_score` (0–10) | higher = **better** |
| Known CVEs | `cve_count_5y` (count) | higher = **worse** |

`openssf_score` is the local Scorecard score, falling back to the
deps.dev mirror — `build_security` already resolves this into a single
`openssf_score` column, so the class consumes it as-is regardless of
source.

### Risk percentiles

Each axis is percentile-ranked across the classified population (see
*Population* below) with the **Hazen plotting position**:

```
pctl = 100 * (rank - 0.5) / n
```

ties averaged. Hazen yields percentiles strictly within `(0, 100)` —
never exactly `0` — so the geometric mean below cannot collapse to `0`.

- `cve_risk_pctl` — Hazen percentile of `cve_count_5y`. More CVEs →
  higher percentile (direct; `_hazen_percentiles` already maps higher
  value → higher percentile).
- `openssf_risk_pctl` — Hazen percentile of **`-openssf_score`**. The
  score is negated so a *lower* score → *higher* risk percentile. This
  inversion is the one place the OpenSSF "higher = better" convention is
  flipped to the pipeline's "A = worst" convention; it is commented at
  the call site.

### Composite

```
security_risk_percentile = sqrt(openssf_risk_pctl * cve_risk_pctl)
```

Geometric mean of the two risk percentiles. A repo scores high only when
broadly insecure; a strong OpenSSF score pulls the composite down even
when the CVE count is high, and vice versa (the "needs both weak → A"
behaviour chosen during brainstorming).

### Class — equal-count quartiles

`security_class` buckets `security_risk_percentile` into four
**equal-count** quartiles:

- Sort classified repos by `security_risk_percentile` **descending**
  (highest risk first).
- `class_index = min(3, p * 4 // n)` for 0-based rank `p` →
  `0→A, 1→B, 2→C, 3→D`.
- **A = worst 25%** (highest security risk), **D = best 25%**.

`A = worst` matches the concentration / complexity / issue-debt /
funding / workload convention. When `n` is not divisible by 4 each class
holds `⌊n/4⌋` or `⌈n/4⌉` repos.

This is exactly `build_workload.py`'s `_quartile_classes` (higher score =
worse, A = top), reused unchanged.

## Population & edge cases

A repo is **classified only when both `openssf_score` and `cve_count_5y`
are present**; otherwise all four new columns are the empty string `""`.

- Percentile ranking runs over the set of repos with both metrics — the
  same "needs all inputs" rule `compute_workload_classes` uses.
- `cve_count_5y` is `""` only when OSV was never queried for that repo;
  a queried-and-zero repo carries `"0"` and **is** classified (CVE = 0
  is a real, low-risk signal, not missing data).
- Current coverage: `openssf_score` 898/899, `cve_count_5y` 899/899 →
  **898/899 classified (99.9%)**; the one repo missing `openssf_score`
  gets a blank class.
- Hazen guarantees both percentiles are `> 0`, so `security_risk_percentile`
  is always `> 0` for classified repos — no geometric-mean collapse.

### Documented characteristic — the CVE zero-mass

78% of risk-scope repos have `cve_count_5y = 0`. Under tie-averaged
Hazen ranking, every zero-CVE repo receives the **same** `cve_risk_pctl`
(≈ 39 — the average rank of the zero block). For that majority the
composite reduces to `sqrt(openssf_risk_pctl * 39)`, so `security_class`
effectively tracks the OpenSSF axis; the CVE axis only re-ranks the ~22%
of repos that have ≥ 1 CVE, pulling them toward A.

This is correct and intended — CVEs are a sparse signal that should only
move repos which actually have them — but it is non-obvious, so it is
stated in the `build_security.py` docstring and in `docs/risk.md`.

## DRY — shared stats helpers

`build_workload.py` already defines three generic, pure helpers used by
its `geom_mean_quartile` pipeline:

- `_hazen_percentiles(values) -> list[float]`
- `_geometric_mean(values) -> float`
- `_quartile_classes(scores) -> list[str]` (higher score = worse, A = top)

`security_class` needs the identical three. Per the repo's DRY rule,
they are **extracted** into a new module `src/pipeline/common/stats.py`
(public names — drop the leading underscore: `hazen_percentiles`,
`geometric_mean`, `quartile_classes`). Both `build_workload.py` and
`build_security.py` import from there.

- `build_workload.py` — remove the three local definitions, import the
  three from `src.pipeline.common.stats`; internal call sites unchanged.
- `tests/test_build_workload.py` — currently tests the three helpers via
  `build_workload`. Those helper tests move to a new `tests/test_stats.py`
  (re-pointed at `src.pipeline.common.stats`); `test_build_workload.py`
  keeps only workload-specific tests.

This refactor is in-scope because `build_security` directly needs the
helpers; no unrelated code is touched.

## `build_security.py` changes

`build_security.py` currently builds rows in a single pass. The class
needs a **second pass** (percentile ranking is population-wide), exactly
like `compute_workload_classes`.

- **New `FIELDS`** — append before `fetched_at`:
  `openssf_risk_pctl, cve_risk_pctl, security_risk_percentile,
  security_class`.
- **New function `compute_security_classes(metrics)`** — mirrors
  `compute_workload_classes`. Input: one dict per repo with `repo`,
  `openssf_score` (`float | None`), `cve` (`float | None`). Output:
  `{repo: {openssf_risk_pctl, cve_risk_pctl, security_risk_percentile,
  security_class}}`, each value `""` for unclassified repos.
  - Keep only repos with both inputs present.
  - `cve_risk_pctl = hazen_percentiles([cve ...])`.
  - `openssf_risk_pctl = hazen_percentiles([-score ...])`.
  - `security_risk_percentile = geometric_mean([openssf_risk_pctl[i],
    cve_risk_pctl[i]])`.
  - `security_class = quartile_classes([security_risk_percentile ...])`.
  - Rounding: percentiles and the composite to 2 dp (matches workload).
- **`build()`** — after assembling the metric rows, call
  `compute_security_classes` and merge the four columns into each row
  (blank for unclassified).
- **`main()`** — add a `Security class` distribution table (A/B/C/D
  counts + a `—` row for blanks), styled like `build_funding.py`'s
  `Funding class` table.
- **Module docstring** — document the four new columns, the
  `geom_mean_quartile` method, and the CVE zero-mass characteristic.

`aggregate_risk.py` needs **no change** — it joins every column of each
intermediate, so the four new columns flow into `risk-data.csv`
automatically.

## `settings.json` & `params.py`

### `src/pipeline/settings.json`

Replace the dormant `risk_classification.security` block with a
parameter-free descriptor matching the `workload` block's style:

```json
"security": {
  "method": "geom_mean_quartile",
  "metrics": ["openssf_score", "cve_count_5y"],
  "comment": "security_class = equal-count quartiles (A = worst 25%) of the geometric mean of Hazen risk-percentiles of openssf_score (inverted — lower score ranks higher-risk) and cve_count_5y. Parameter-free; no numeric thresholds. Empty when openssf_score or cve_count_5y is missing."
}
```

### `src/pipeline/common/params.py`

Remove the `SECURITY_THRESHOLDS = _P["risk_classification"]["security"]`
line — the new block has no numeric thresholds and nothing in `src/`
reads `SECURITY_THRESHOLDS` (verified by grep). The `workload` block is
likewise not loaded into `params.py`; parameter-free blocks need no
constant.

## Documentation

### `docs/risk.md`

- Add a **Security Class** subsection alongside the existing
  Concentration / Complexity / Issue Debt class tables, describing the
  `geom_mean_quartile` method, the two axes, the OpenSSF-score inversion,
  and the CVE zero-mass characteristic.
- Update the `risk-data.csv` output-column table with the four new
  columns.

## Testing

New regression tests, following existing `tests/` conventions.

**`tests/test_stats.py`** — the three extracted helpers (migrated from
`test_build_workload.py`, re-pointed at `src.pipeline.common.stats`):
- Hazen percentiles strictly within `(0, 100)`, never `0`.
- Ties share the averaged percentile.
- `geometric_mean` correct on a known fixture; `[] → 0.0`.
- `quartile_classes`: equal-count split, `A` = highest-score quarter,
  `n` not divisible by 4 → each class `⌊n/4⌋..⌈n/4⌉`.

**`tests/test_build_security.py`** — `compute_security_classes`:
- **Inversion** — the repo with the lowest `openssf_score` gets the
  highest `openssf_risk_pctl`.
- **CVE zero-mass** — all repos with `cve = 0` get an identical
  `cve_risk_pctl`.
- **Both axes weak → A** — a repo bad on both lands in `A`; a repo bad
  on only one axis lands mid-pack (the geometric-mean behaviour).
- **Missing input → blank** — a repo missing `openssf_score` (or `cve`)
  gets `""` for all four columns and is excluded from the ranking
  population.
- **Equal-count quartiles** — with `n` classified repos each class holds
  `n/4 ± 1`; `A` is the highest-risk quarter.
- **Geometric mean** — composite computed correctly on a small fixture.

## Out of scope

- Folding `sast_findings_*`, `ossfuzz_enrolled`, or
  `bestpractices_badge_id` into the class — the class is deliberately the
  two OpenSSF-rooted signals only (`openssf_score`, `cve_count_5y`).
- A combined cross-dimension risk grade — each dimension keeps its own
  independent class.
- Any change to `aggregate_risk.py` logic.

## File-change summary

| File | Change |
|---|---|
| `src/pipeline/common/stats.py` | **new** — `hazen_percentiles`, `geometric_mean`, `quartile_classes` |
| `src/pipeline/risk/build_workload.py` | drop the 3 local helpers; import from `common.stats` |
| `src/pipeline/risk/build_security.py` | add `compute_security_classes`; 4 new columns; 2nd pass; class table; docstring |
| `src/pipeline/settings.json` | replace `risk_classification.security` with the `geom_mean_quartile` descriptor |
| `src/pipeline/common/params.py` | remove unused `SECURITY_THRESHOLDS` |
| `docs/risk.md` | Security Class subsection; output-column table |
| `tests/test_stats.py` | **new** — the 3 shared helpers |
| `tests/test_build_workload.py` | drop helper tests (moved to `test_stats.py`) |
| `tests/test_build_security.py` | **new** — `compute_security_classes` |
