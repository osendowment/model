# Design: workload class + contributor-fetch cleanup

**Date:** 2026-05-18
**Status:** approved (design), pending implementation plan

## Motivation

The risk pipeline measures sustainability risk across six dimensions. The
"Maintainer workload" dimension currently produces raw metrics in
`data/workload.csv` but **no class** — unlike concentration, complexity,
security, and funding, which each carry an A–D class.

This work adds a `workload_class`: a per-repo A–D tier capturing how much
codebase, security, and issue burden each contributor carries. It also
cleans up an upstream data-quality problem the class depends on — the
unreliable `/stats/contributors` API path — and introduces a clean
`active_contributors` count as the class's denominator.

Four parts, in dependency order:

1. Remove the dead `/stats/contributors` code paths (scripts).
2. Delete the stale per-year contributor data (consolidate CSVs).
3. Add `active_contributors` + ensure the fetch date is stored.
4. Add `workload_class` to `build_workload.py`.

## Background: why `/stats/contributors` must go

GitHub exposes two contributor endpoints:

- `/repos/{repo}/contributors` — paginated list, each contributor's
  **lifetime** `contributions` (commit count). Stable.
- `/repos/{repo}/stats/contributors` — per-week breakdown, would allow
  windowing to 2021–2025. **Returns HTTP `202 "computing"` indefinitely
  for ~90% of repos** and never resolves.

Measured coverage over the 929 risk-scope repos (value class A/B):

| Source | Metric | Coverage |
|---|---|---:|
| `/contributors` (lifetime) | `bf_commits_lifetime` | ~94% |
| `/contributors` (lifetime) | non-bot contributor count | ~99.6% |
| `/stats/contributors` (per-year) | any non-zero per-year cell | **~8%** |

The batch fetcher (`batch_runner.py`) already migrated to `/contributors`.
`/stats/contributors` survives only in single-repo CLI code and as stale,
mostly-empty per-year columns in the wide CSVs. We remove both.

**Consequence for terminology:** there is no reliable windowed
"active in last 5 years" contributor count. `active_contributors` (AC)
in this design means **lifetime distinct non-bot contributors** — the
exact set the bus factor is computed over. The name reflects intent;
the caveat (lifetime, not windowed) is documented at every output.

---

## Part 1 — Remove `/stats/contributors` (scripts)

### `src/github/github_client.py`
- Remove `fetch_contributor_stats` (sync, ~line 124) and the async
  `/stats/contributors` fetch (~line 274).
- **Keep** `_fetch_contributors_paginated`, `_fetch_total_commits`,
  `_fetch_total_contributors` — these hit `/contributors` / `/commits`
  and are stable.

### `src/github/fetch_contributors_metrics.py`
- Remove the `/stats/contributors`-only functions: `_week_in_range`,
  `_parse_api_stats`, `calculate_bus_factor` (the `date_range` stats
  variant), `_cumulative_loc`, `compute_yearly_breakdown`.
- **Keep** `compute_lifetime_metrics`, `_compute_bus_factor`,
  `parse_repo`, `is_bot` usage.
- Rewrite the single-repo CLI branch of `main()` to use the same
  `/contributors` lifetime path as the batch: fetch via
  `_fetch_contributors_paginated` + totals, run `compute_lifetime_metrics`,
  display the single aggregate result. Remove the `--years` argument
  (every metric is lifetime — a year range is meaningless).
- Drop the now-unused import of `fetch_contributor_stats` and
  `display_yearly_breakdown`.

### `src/github/display.py`
- Remove `display_yearly_breakdown` (no remaining caller). Keep
  `_spinner`, `display_results`.

### `src/github/batch_runner.py`
- Remove the wide-file machinery: `WIDE_FILES`, `YEARS_FILE`,
  `YEARS_FIELDS`, `_upsert_yearly_csv`, `_upsert_yearly_csv_batch`,
  `_upsert_wide_file`, `_upsert_years_file`, `_read_existing_periods`,
  and `_build_metrics_extractors` (or trim it to whatever
  `_upsert_concentration_data` still needs).
- `batch_update` calls **only** `_upsert_concentration_data` to persist
  results (no more wide-file writes).
- **Keep** `_recently_fetched_repos` (freshness gate — already reads
  `concentration-data.csv`, not the wide files).

### Verification
After removal, `grep -rn "stats/contributors" src/` returns nothing
(docstrings included). A test asserts this.

---

## Part 2 — Delete stale per-year data (consolidate)

The per-year columns (`2021`…`2025`) in the wide CSVs are 202-pathology
junk (~8% populated). With the per-year writers gone, each wide file
would collapse to a single `2021-2025` column already duplicated in
`concentration-data.csv`.

**Delete** (via `git rm`):
- `data/github/contributors/bus-factor.csv`
- `data/github/contributors/hhi.csv`
- `data/github/contributors/contributors.csv`
- `data/github/contributors/bots.csv`
- `data/github/contributors/commits.csv`
- `data/github/contributors/years.csv`
- the now-empty `data/github/contributors/` directory

**Before deleting `contributors.csv`**, run a one-time migration
(Part 3) to carry its `2021-2025` column into `concentration-data.csv` —
that column already holds the non-bot contributor count we need, so no
multi-hour re-fetch is required.

`data/concentration-data.csv` becomes the **single** contributor-metrics
file.

---

## Part 3 — `active_contributors` + fetch date

### Fetch date
`concentration-data.csv` already carries `fetched_at` (UTC ISO 8601,
written by `_upsert_concentration_data`). Verified present. No change
needed beyond keeping it through the refactor — it is the canonical
"when did we fetch the contributor data used for BF / HHI / AC" stamp.

### New column: `active_contributors`
`active_contributors` = count of **non-bot** contributors in the
`/contributors` payload — the exact set `_compute_bus_factor` runs over.
The extractor already exists in `batch_runner.py`
(`len([c for c in r.contributors if not c.is_bot])`); route it into
`concentration-data.csv` instead of the deleted `contributors.csv`.

New `concentration-data.csv` header:
```
repo, repo_id, total_commits, total_contributors,
active_contributors, bus_factor, hhi, fetched_at
```
- `total_contributors` — `/contributors?anon=true` Link-header count
  (anon-inclusive, full). Kept.
- `active_contributors` — non-bot count from the payload. New.

Update `CONCENTRATION_FIELDS` and the `_upsert_concentration_data` row
builder accordingly.

### One-time migration
A small script reads `contributors.csv`'s `2021-2025` column and writes
it as `active_contributors` into `concentration-data.csv` (matching on
`repo`). Repos absent from `contributors.csv` get an empty value.
Coverage after migration: ~99.6%. Run once, then delete `contributors.csv`.

### `src/pipeline/risk/build_concentration.py`
- Remove `HHI_FILE`, `BF_FILE`, `CONTRIB_FILE`, `COMMITS_FILE`,
  `_load_agg_column`. Read everything from `concentration-data.csv` only.
- Add `active_contributors` to `FIELDS` and the output row (passed
  through from `concentration-data.csv`).
- Update the module docstring (no more wide-file inputs; everything is
  lifetime from `/contributors`).

`concentration.csv` gains an `active_contributors` column.

**Caveat (documented in docstring + docs/risk.md):** `active_contributors`
is lifetime distinct non-bot contributors. For repos with >5000
contributors (mega-repos the fetcher skips; `/contributors` truncates
named results) it is a **floor**, not exact.

---

## Part 4 — `workload_class` in `build_workload.py`

### Inputs
`build_workload.py` additionally reads three sibling intermediates
(it already runs after all of them in `run_risk_pipeline.py`):

| Input | Source file | Column |
|---|---|---|
| LOC | `data/complexity.csv` | `loc_2025_eoy` |
| CVE | `data/security.csv` | `cve_count_5y` |
| AC | `data/concentration.csv` | `active_contributors` |

NNI is computed in-script from values `build_workload` already loads:
`NNI = issues_opened_5y − issues_closed_5y` (net new issues, 2021–2025).

### Per-maintainer burden ratios
Three ratios, all "▴ higher = more workload":
- `loc_per_ac  = LOC / AC`
- `cve_per_ac  = CVE / AC`
- `nni_per_ac  = NNI / AC`

`nni_per_ac` may be **negative** when a repo closes issues faster than it
opens them — kept as-is (it correctly ranks at the low-burden end).

### Percentile ranking
Each ratio is percentile-ranked across the set of repos that have **all
three** ratios computable. Ranking uses the **Hazen plotting position**:
```
pctl = 100 * (rank - 0.5) / n
```
with ties averaged. This yields percentiles strictly within `(0, 100)` —
critically, never exactly `0` — so the geometric mean below cannot
collapse. (`build_complexity.py`'s `_percentile_ranks` uses
`(rank-1)/(n-1)`, which can hit 0; do **not** reuse it here — add a
local `_hazen_percentiles` helper.)

Output columns: `loc_per_ac_pctl`, `cve_per_ac_pctl`, `nni_per_ac_pctl`.

### Composite burden score
```
workload_burden_percentile = (loc_pctl * cve_pctl * nni_pctl) ** (1/3)
```
Geometric mean of the three percentiles. A repo scores high only when it
is broadly burdened — one extreme axis cannot dominate the way it would
in an arithmetic mean.

### Class — equal-count quartiles
`workload_class` buckets `workload_burden_percentile` into four
**equal-count** groups ("25% groups"):
- Sort classified repos by `workload_burden_percentile` **descending**
  (highest burden first); position `p` is 1-based.
- `class_index = min(3, (p - 1) * 4 // n)` → `0→A, 1→B, 2→C, 3→D`.
- `A` = top-25% burden (worst), `D` = bottom-25% (healthiest).

`A = worst` matches the existing concentration / complexity / security
convention. When `n` is not divisible by 4, each class holds `⌊n/4⌋` or
`⌈n/4⌉` repos (the `(p-1)*4//n` boundary distributes the remainder
deterministically).

### Edge cases
- **AC missing or 0** → all workload-class columns empty for that repo
  (division undefined).
- **Any of LOC / CVE / NNI missing** → empty (the class needs all
  three). Given coverage (LOC ~100%, CVE ~98%, AC ~99.6%), ~97% of
  risk-scope repos receive a class; the rest are correctly blank.
- **NNI negative** → ratio kept negative, ranks low. Not floored.
- Percentile ranking + quartile assignment are done in a **second pass**
  after all ratios are collected (same two-pass shape as
  `build_complexity.py`'s hotspot percentile).

### New `workload.csv` columns
Appended to the existing schema:
- `active_contributors` — **renamed** from `active_maintainers_lifetime`;
  now sourced from `concentration.csv` (the deleted `contributors.csv`
  is gone).
- `net_new_issues_5y`
- `loc_per_ac`, `cve_per_ac`, `nni_per_ac`
- `loc_per_ac_pctl`, `cve_per_ac_pctl`, `nni_per_ac_pctl`
- `workload_burden_percentile`
- `workload_class`

`aggregate_risk.py` needs **no change** — it joins every column from each
intermediate, so the new columns flow into `risk-data.csv` automatically.

---

## Documentation & settings

### `docs/risk.md`
- Add a **Workload Class** subsection with the A–D table (alongside the
  existing Concentration / Complexity / Issue Debt class tables).
- Update the "Maintainer workload" metrics-roadmap leaf:
  `active_contributors ← GitHub /contributors [lifetime]` (drop the
  `[2021–2025]` aspiration and the per-year note).
- Update the `risk-data.csv` output-column table with the new columns.
- Update the "Source-file coverage" table — the
  `data/github/contributors/*.csv` wide files no longer exist;
  `concentration-data.csv` is the single contributor source.

### `src/pipeline/settings.json`
Add a `workload` block under `risk_classification`, matching the
prevailing terse style:
```json
"workload": {
  "method": "geom_mean_quartile",
  "ratios": ["loc_per_ac", "cve_per_ac", "nni_per_ac"],
  "comment": "workload_class = equal-count quartiles (A = worst 25%) of the geometric mean of Hazen percentiles of LOC/AC, CVE/AC, NNI/AC. AC = active_contributors (lifetime non-bot contributors). NNI = issues_opened_5y - issues_closed_5y. Parameter-free; no numeric thresholds."
}
```

---

## Testing

New regression tests (follow existing `tests/` conventions):

**Part 1 — removal**
- `grep`-style / import test: no `stats/contributors` reference remains
  in `src/`; importing the three touched modules succeeds.

**Part 3 — concentration**
- `build_concentration` reads `concentration-data.csv` only and emits a
  populated `active_contributors` column.

**Part 4 — workload_class** (the core logic)
- Hazen percentiles are strictly within `(0, 100)` — never 0 — so the
  geometric mean of a repo that is the unique minimum on one axis is
  still positive.
- Equal-count quartiles: with `n` classified repos, each class holds
  `n/4 ± 1`; `A` is the highest-burden quarter.
- A repo with **negative NNI** ranks in the low-burden tail and still
  receives a class.
- A repo **missing CVE** (or LOC, or AC) gets empty workload-class
  columns; a repo with `AC = 0` gets empty columns.
- Geometric mean is computed correctly on a known small fixture.

## Out of scope

- Re-fetching true windowed (2021–2025) contributor activity — would
  require fixing the GitHub `202` pathology; deferred.
- Classes for the other dimensions that still lack one (visibility);
  this spec covers workload only.
- Any change to `aggregate_risk.py` logic (it auto-joins new columns).

## File-change summary

| File | Change |
|---|---|
| `src/github/github_client.py` | remove `/stats/contributors` fetchers |
| `src/github/fetch_contributors_metrics.py` | remove yearly/stats path; single-repo CLI uses `/contributors` |
| `src/github/display.py` | remove `display_yearly_breakdown` |
| `src/github/batch_runner.py` | remove wide-file writers; add `active_contributors` to concentration-data |
| `src/pipeline/risk/build_concentration.py` | read `concentration-data.csv` only; emit `active_contributors` |
| `src/pipeline/risk/build_workload.py` | add workload_class + ratios + percentiles |
| `src/pipeline/settings.json` | add `risk_classification.workload` |
| `docs/risk.md` | workload class table; roadmap + coverage + output updates |
| `data/concentration-data.csv` | add `active_contributors` column (one-time migration) |
| `data/github/contributors/*.csv` | **deleted** (6 files + dir) |
| `tests/` | new regression tests (Parts 1, 3, 4) |
