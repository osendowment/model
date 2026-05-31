# Risk percentiles — design

**Date:** 2026-05-31
**Branch / worktree:** `feat/risk-percentiles` (`.worktrees/risk`)
**Status:** design, pending implementation plan

## Goal

Replace the A/B/C/D **class** outputs of the risk dimensions with a uniform,
direction-aware **percentile (`_p`)** system:

1. Every risk-bearing metric gets a `<col>_p` percentile (0–100, higher = riskier).
2. Each dimension gets a composite total: `<dim>_score` (geometric mean of its
   component `_p`s) and `<dim>_p` (that score re-percentiled into a true
   percentile).
3. One overall `risk_score` / `risk_p` per repo in `risk.csv`.
4. All `*_class` columns and their threshold machinery are **removed**.

The percentile must be "smart" about direction: the **worst** value on an axis
maps to **100**, the **best** maps to **~0**, and a value shared by a large
majority is *not* dragged toward the middle. Concretely: a repo with bus
factor 1 (the worst possible) must score **100** on that axis, not the ~68 a
tie-averaged percentile gives today.

## 1. The risk percentile statistic

A single shared function replaces both current percentile helpers
(`hazen_percentiles` and complexity's local `_percentile_ranks`).

```
risk_percentiles(values: list[float], higher_is_worse: bool) -> list[float]
```

For each repo *i* with value *vᵢ*, orient values so "more risk" is a single
direction (`risk = v` if `higher_is_worse` else `-v`), then:

```
less_i  = # repos strictly LESS risky than i
worse_i = # repos strictly MORE risky than i
P_i = 100 * less_i / (less_i + worse_i)
```

- **Ties are ignored** — `P_i` depends only on how many repos are strictly
  better or worse, never on how many share `vᵢ`. This is the key property: a
  common value is *not* pulled toward the center.
- **Worst value → exactly 100** (`worse_i = 0`), regardless of how common it is
  → bus factor 1 scores 100. ✓
- **Best value → 0** (`less_i = 0`), regardless of how common it is → 0 CVEs
  scores ~0 ("push best-ties down"). ✓
- **Floor for the geometric mean:** a literal 0 would collapse any geometric
  mean it enters. Only the least-risky group hits 0, so we floor those (and only
  those) to `δ = 100 / (2n)` (≈ 0.056 at n≈900 — the same order as the old Hazen
  floor). The worst group keeps an exact 100 (a high value never breaks the
  geometric mean).
- **Constant axis:** if every repo shares one value (`less + worse = 0` for all),
  the axis carries no signal → every `_p` is `""` and the axis is dropped from
  any composite that would include it.
- **Missing value:** a repo without a parseable value for the metric gets `_p =
  ""` and is excluded from that metric's population.

### Worked examples

Bus factor (`higher_is_worse=False`), values 1 (50% of repos), 2 (30%), 3 (20%):

| value | less (strictly higher BF) | worse | P |
|---|---|---|---|
| 1 | 50% | 0 | **100.0** |
| 2 | 20% | 50% | 28.6 |
| 3 | 0 | 80% | 0 → floored to δ |

CVE count (`higher_is_worse=True`), values 0 (78%), 1 (15%), 5 (7%):

| value | less (strictly fewer) | worse | P |
|---|---|---|---|
| 0 | 0 | 22% | 0 → floored to δ |
| 1 | 78% | 7% | 91.8 |
| 5 | 93% | 0 | **100.0** |

### Population for each `_p`

Each component `_p` ranks a repo against **all repos that have that metric**
(present + parseable), *not* gated on sibling metrics. (This is a deliberate
change from today's security/workload builders, which only rank repos that have
*all* the dimension's inputs. Ranking each axis over its own population is more
correct.) The dimension **composite** is still only computed for repos that have
every component the composite needs.

## 2. Direction map

`higher_is_worse = True` (more = riskier): all HHI variants, `cve_count_5y`,
`sast_findings_total/error/security`, `loc_2025_eoy`, `scc_complexity_2025_eoy`,
`cognitive_max`, `cyclomatic_max`, `churn_5y_total`, `hotspot_log`,
`loc_per_ac`, `cve_per_ac`, `nni_per_ac`.

`higher_is_worse = False` (less = riskier): all bus-factor variants,
`openssf_score`, `github_sponsors`, `channels_count`, `oc_avg_funding`,
`stars`, `forks`, `watchers`.

## 3. Per-dimension columns

For every dimension: each listed metric gains a `<metric>_p`; the dimension
gains `<dim>_score` (geometric mean of the **composite** component `_p`s) and
`<dim>_p` (`risk_percentiles([score], higher_is_worse=True)` — the score
re-ranked into a true percentile). `_score` is kept alongside `_p` for
auditability (the `_p` is reproducible from the component `_p`s via the
`_score`).

### concentration.csv
- New `_p`: `bf_commits_github_p`, `hhi_commits_github_p`, `bf_commits_git_p`,
  `hhi_commits_git_p`, `bf_commits_git_2021_2025_p`, `hhi_commits_git_2021_2025_p`.
- Composite: `concentration_score`, `concentration_p` from
  **`bf_commits_git_2021_2025_p` + `hhi_commits_git_2021_2025_p`** (the windowed
  git pair).
- Remove: `concentration_class`.

### complexity.csv
- New `_p`: `loc_2025_eoy_p`, `scc_complexity_2025_eoy_p`, `cognitive_max_p`,
  `cyclomatic_max_p`, `churn_5y_total_p`, `hotspot_log_p`
  (`hotspot_log_p` replaces today's `hotspot_percentile`).
- Composite: `complexity_score`, `complexity_p` from
  **`loc_2025_eoy_p` + `cyclomatic_max_p`** (mirrors the old loc + cyclomatic
  class intent).
- Remove: `loc_class`, `cc_class`, `complexity_class`, `hotspot_percentile`.

### security.csv
- Rename `openssf_risk_pctl → openssf_score_p`, `cve_risk_pctl → cve_count_5y_p`,
  `security_risk_percentile → security_p`; add `security_score`.
- Optional new `_p`: `sast_findings_total_p`, `sast_findings_error_p`,
  `sast_findings_security_p` (not in the composite).
- Composite: `security_score`, `security_p` from
  **`openssf_score_p` + `cve_count_5y_p`**.
- Remove: `security_class`.

### workload.csv
- Rename `loc_per_ac_pctl → loc_per_ac_p`, `cve_per_ac_pctl → cve_per_ac_p`,
  `nni_per_ac_pctl → nni_per_ac_p`, `workload_burden_percentile → workload_p`;
  add `workload_score`.
- Optional new `_p` (informational, **not** in the composite):
  `issue_close_ratio_p` (lower = worse), `issue_trend_score_p` (lower = worse).
- Composite: `workload_score`, `workload_p` from
  **`loc_per_ac_p` + `cve_per_ac_p` + `nni_per_ac_p`**.
- Remove: `workload_class`.

### funding.csv
- New `_p`: `github_sponsors_p`, `channels_count_p`, `oc_avg_funding_p` (all
  `higher_is_worse=False` → no funding ⇒ high risk).
- Composite: `funding_score`, `funding_p` from those three.
- Data semantics: blank `github_sponsors`/`oc_avg_funding` = "not measured"
  (excluded); an explicit `0` = "measured, none" (worst). `channels_count` is
  always numeric (0+). **Open point** — confirm `sponsors.csv` writes `0` vs
  blank correctly so we don't rank "never fetched" as max risk.

### visibility.csv
- New `_p`: `stars_p`, `forks_p`, `watchers_p` (all `higher_is_worse=False` →
  obscure infra ⇒ high risk).
- Composite: `visibility_score`, `visibility_p` from those three.

### risk.csv (aggregate)
- All of the above flow through the join unchanged.
- New overall: `risk_score` = geometric mean of the **available** dimension
  `_p`s (concentration, complexity, security, workload, funding, visibility;
  skip missing); `risk_p` = `risk_score` re-percentiled. A repo needs ≥1
  dimension total to get a `risk_p`.

## 4. Code changes

### src/pipeline/common/stats.py
- Add `risk_percentiles(values, higher_is_worse)` (§1).
- Add a small DRY helper for the composite, e.g.
  `geom_mean_then_rank(component_p_rows) -> (scores, percentiles)`:
  geometric mean per repo, then `risk_percentiles(scores, higher_is_worse=True)`.
- Keep `geometric_mean`.
- **Remove** `hazen_percentiles` and `quartile_classes`.

### builders
- `build_concentration.py`, `build_complexity.py`, `build_security.py`,
  `build_workload.py`, `build_funding.py`, `build_visibility.py`: drop class
  logic, add the `_p` / `_score` columns and the per-dimension composite via the
  shared helpers; update `FIELDS`, docstrings, and the coverage/`main()` summary
  tables (class-distribution tables → score/percentile coverage).
- `build_complexity.py`: delete `loc_class`, `_worst_class`, `_CLASS_RANK`,
  `_percentile_ranks`; `hotspot_percentile → hotspot_log_p`.
- `aggregate_risk.py`: after the join, compute `risk_score` / `risk_p` and append
  the two columns.

### settings.json / params.py
- Remove `risk_classification.concentration`, `.complexity_loc`, `.security`,
  `.workload` blocks.
- Remove **dead** `risk_classification.issue_debt` and `.issue_trend` blocks
  (no readers outside `params.py` — verified) and their `params.py` bindings
  `ISSUE_DEBT_THRESHOLDS`, `ISSUE_TREND_THRESHOLDS`.
- Remove `CONCENTRATION_THRESHOLDS`, `COMPLEXITY_LOC_THRESHOLDS` bindings.
- Keep value-stage params (`value_classes`, `risk_input.value_classes`, etc.)
  untouched — eligibility consumes **value** class only, so it is unaffected.

### tests
- `test_stats.py`: drop `hazen_percentiles` / `quartile_classes` tests; add
  `risk_percentiles` tests (worst→100, best→δ, ties-ignored, direction,
  constant-axis, missing) and a `geom_mean_then_rank` test. Include the bus
  factor 1 → 100 regression case explicitly.
- `test_build_{concentration,complexity,security,workload,funding}.py`: replace
  class assertions with `_p` / composite assertions.

### docs / scripts
- `docs/risk.md`, `docs/pipeline.md`: rewrite the class sections as the
  percentile system.
- `scripts/coverage_report.py` (and any other in-repo reader of the risk
  `*_class` columns): point at the new `_p` columns.

## 5. Out of scope
- The separate `osendowment/data` dashboard repo reads `risk.csv` columns
  (including the `*_class` columns being removed). Updating it is a **follow-up
  in that repo**, not this worktree. Flagged here so it isn't forgotten.
- No changes to fetchers or to the Value/Eligibility stages.

## 6. Open points for review
1. `sponsors.csv` blank-vs-`0` semantics (§3 funding) — confirm before trusting
   `github_sponsors_p`.
2. Optional `_p`s flagged "not in composite" (sast_*, issue_close_ratio,
   issue_trend_score) — include them as informational columns, or skip?
3. Keep both `<dim>_score` and `<dim>_p`, or `_p` only? (Spec keeps both for
   audit; easy to trim.)
