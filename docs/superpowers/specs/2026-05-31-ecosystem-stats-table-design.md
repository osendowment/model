# Ecosystem stats table — design

**Date:** 2026-05-31
**Status:** approved (pending written-spec review)

## Goal

Replace `data/value/ecosystem-downloads.csv` (a year × ecosystem download
matrix) with `data/value/stats.csv` — a **metric × ecosystem** matrix that
captures the key value-pipeline metrics per ecosystem, kept up to date
incrementally as each ecosystem pipeline computes.

## File format

`data/value/stats.csv`, one row per metric, one column per ecosystem, plus a
`total` column:

```
metric,npm,pypi,crates,debian,homebrew,cpp,total
downloads_2021,1596404178196,124454273394,6884300434,154760080,0,,1727843398... 
...
packages_top,5765,2460,3719,1633,1062,1329,14639
...
```

- **Ecosystem columns (6):** `npm, pypi, crates, debian, homebrew, cpp`.
- **`total` column:** for every metric, `total` = sum of the **five**
  non-cpp ecosystems (`npm+pypi+crates+debian+homebrew`). cpp is **excluded**
  from `total` because its C/C++ packages already appear in the debian and
  homebrew columns — summing it would double-count.
- **cpp download cells are blank.** cpp has no comparable per-year download
  count (its importance signal is `downloads_score`, a weighted debian+homebrew
  blend), so `downloads_*` rows leave the cpp cell empty. cpp's
  `packages_*` and `pagerank_*` cells **are** populated.
- Empty cell = "not applicable / not yet computed" (distinct from `0`).

## Metrics

### Base metrics (written directly)

| metric | meaning | source |
|---|---|---|
| `downloads_2021`…`downloads_2025` | total downloads that year | `build_stats` download loaders (existing) |
| `packages_all` | candidate universe: packages with nonzero avg downloads, **before** the top-cut | `process_data` step_top pre-cut count |
| `packages_top` | packages selected into `top-packages.csv` | rows of `top-packages.csv` |
| `packages_top_with_deps` | top ∪ dependency-tree nodes | rows of `results.csv` |
| `git_urls` | packages with a resolved git URL on **any** host | `git.csv` (non-empty picked `git`) |
| `github_repos` | packages with a resolved GitHub repo | `git.csv` (non-empty `github`) |
| `pagerank_p50` / `p75` / `p90` | **distribution** (rank-based): count of packages in the top 50% / 25% / 10% by pagerank score | `results.csv` `pagerank` column |
| `pagerank_cum_p50` / `cum_p75` / `cum_p90` | **Pareto** (mass-based): count of top-ranked packages whose cumulative pagerank share first reaches 50% / 75% / 90% of total mass | `results.csv` `pagerank` column |

`pagerank_pXX` (distribution) = count of packages at or above the XXth
percentile pagerank score, i.e. the top `(100−XX)%` of packages by rank.

`pagerank_cum_pXX` (Pareto) = the "vital few." By construction these equal
the value-class boundary counts: `cum_p50` = count(class A),
`cum_p75` = count(A+B), `cum_p90` = count(A+B+C) — a built-in cross-check
against `value_class` in `results.csv`. N (the population pagerank ranks
over) = `packages_top_with_deps`.

### Derived metrics (computed from base cells in every column)

| metric | formula (per column, incl. `total`) |
|---|---|
| `downloads_total` | sum of `downloads_2021…2025` |
| `top_share_pct` | `100 × packages_top / packages_all` |

### Row order

```
downloads_2021, downloads_2022, downloads_2023, downloads_2024, downloads_2025,
downloads_total, packages_all, packages_top, top_share_pct,
packages_top_with_deps, git_urls, github_repos,
pagerank_p50, pagerank_p75, pagerank_p90,
pagerank_cum_p50, pagerank_cum_p75, pagerank_cum_p90
```

## Components

### `src/pipeline/common/stats.py` (new — shared helper)

The single source of truth for the matrix. No script writes `stats.csv`
directly; all go through this module.

- `STATS_FILE = data/value/stats.csv`
- `ECOSYSTEMS`, `TOTAL_ECOSYSTEMS` (the 5 non-cpp), `BASE_METRICS`,
  `DERIVED_METRICS`, `METRIC_ORDER`.
- `read_stats() -> dict[metric][eco] -> str` — `{}` if file absent.
- `update_stats(ecosystem: str, values: dict[str, float]) -> None` —
  read-modify-write: set the given base cells for `ecosystem`, then
  `_recompute(matrix)`, then atomic write. Creates the file (all metric
  rows, blank cells) if absent. Touches only the passed ecosystem's cells —
  a single-ecosystem rerun updates only its column.
- `pagerank_metrics(scores: list[float]) -> dict[str, int]` — given the
  per-package pagerank scores, returns the six `pagerank_*` values.
- `_recompute(matrix)` — (a) for each base metric, set `total` = sum of the
  present numeric `TOTAL_ECOSYSTEMS` cells; (b) for every column (6 ecos +
  `total`), fill `downloads_total` and `top_share_pct` from that column's
  base cells. Idempotent and order-independent, so write order never
  produces an inconsistent file.
- Atomic write (`.tmp` + `os.replace`), `QUOTE_ALL`, matching existing style.

### `src/pipeline/value/build_stats.py` (renamed from `build_ecosystem_downloads.py`)

- **Default mode:** compute the `downloads_*` rows via the existing loaders
  (npm may hit the network to fill missing years) and write them through
  `update_stats(eco, {downloads_YYYY: ...})` for each of the 5 download
  ecosystems. Keeps its current pipeline position (after the ecosystem
  pipelines) — it refreshes downloads for the *next* run's top-selection,
  exactly as today.
- **`--rebuild` mode:** recompute **every** row for every ecosystem from
  on-disk artifacts (no network) — downloads from raw files, `packages_*`
  from `top-packages.csv`/`results.csv`, `pagerank_*` from `results.csv`,
  `git_urls`/`github_repos` from `git.csv` — and write the whole matrix.
  This is the consistency/repair pass and the migration path.

### `src/<eco>/process_data.py` (6 files: npm, pypi, crates, debian, homebrew, cpp)

At the end of `main()`, after `results.csv` is written, call
`update_stats(<eco>, {...})` with `packages_all`, `packages_top`,
`packages_top_with_deps`, and the six `pagerank_*` (via `pagerank_metrics`).
Each `step_top` already knows its pre-cut universe size (`packages_all`) and
selected count (`packages_top`); `step_results` knows the pagerank scores and
row count. Thread those values out to `main()` (small signature changes) —
no recomputation from disk.

### `src/pipeline/value/build_git_urls.py`

`git_urls` and `github_repos` are only known after URL resolution, so
`process_data` cannot set them. After writing each ecosystem's `git.csv`,
call `update_stats(<eco>, {git_urls, github_repos})`.

### `src/pipeline/common/params.py`

`ecosystem_avg_downloads(ecosystem)` currently parses the wide
`ecosystem-downloads.csv` (year rows × eco columns). Rewrite to read
`stats.csv` via `read_stats()`, pull the `downloads_2021…2025` cells for the
ecosystem, and average the populated (>0) years — identical semantics, new
layout. Only npm/pypi/crates/homebrew call this; debian uses source-install
mass and cpp uses per-ecosystem cumulative shares, so neither needs it.

### `src/pipeline/run_value_pipeline.py`

- Rename step `downloads` → `stats` (module `build_stats`), same position.
- Add a final step `stats-rebuild` → `build_stats --rebuild` after `verify`,
  so a full run always ends with a fully-consistent matrix recomputed from
  disk.

## Data flow

```
ecosystem pipelines (npm/pypi/crates/cpp→debian+homebrew)
  process_data → results.csv (+ update_stats: packages_*, pagerank_*)
        │
        ▼
build_stats (downloads_* ; may fetch)        ← reads prior stats.csv for top-selection
        │
        ▼
build_git_urls → git.csv (+ update_stats: git_urls, github_repos)
        │
        ▼
unify_value_data → value/value.csv
        │
        ▼
verify_git_urls
        │
        ▼
build_stats --rebuild  → full consistent stats.csv from disk
```

## Migration

`data/value/ecosystem-downloads.csv` is removed; `git mv` is not meaningful
(format changes), so it is deleted and `data/value/stats.csv` is generated by
`build_stats --rebuild` once. Update the one ref in `params.py` and the
mention in project `CLAUDE.md` (Data Organization → value stage).

## Auditability

Per project rules, every value is traceable: each `stats.csv` cell is
recomputable from a named on-disk artifact (`build_stats --rebuild` does
exactly that), and the `pagerank_cum_*` rows cross-check against
`value_class` counts in `results.csv`. The file is a derived rollup of
already-audited per-package data, so no extra `fetched_at`/status column is
needed (the underlying artifacts carry those).

## Testing

- `tests/test_stats.py` (new): `_recompute` total/derived math (incl. cpp
  excluded from `total`, blank cpp download cells); `update_stats` merge
  preserves other ecosystems' cells; `pagerank_metrics` distribution vs
  cumulative on a small known score list (and the `cum_*` == class-boundary
  identity).
- Update any test referencing `ecosystem-downloads.csv` or
  `ecosystem_avg_downloads`'s file format.

## Out of scope

- No new fetched data — every metric derives from existing artifacts.
- Risk/eligibility stages unchanged.
- No `value.csv` schema change.
