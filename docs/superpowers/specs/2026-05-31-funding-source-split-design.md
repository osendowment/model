# Funding source split & funding_class removal — design

**Date:** 2026-05-31
**Status:** approved

## Problem

The funding risk dimension conflates raw collection and aggregation in one
file, `data/risk/funding-data.csv`, produced by a single per-repo collector
(`src/github/fetch_funding.py`). That file mixes three independent external
sources (GitHub Sponsors, `.github/FUNDING.yml`, per-repo `funding.json`) plus
two derived columns (`funding_sources`, `funding_class`). This violates the
project's data-organization rule that external/fetched data lives under
`data/sources/<source>/` and only stage outputs live in `data/risk/`.

Additionally:
- `funding_class` (A/B/C/D) is computed twice with *different* definitions
  (the collector uses a multi-signal heuristic; `build_funding.py` recomputes
  from sponsor count alone). Only the latter reaches `risk.csv`. The class is
  to be removed entirely — the pipeline keeps raw funding signals, not a class.
- The per-repo `funding.json` fetch hits ~900 repos individually yet finds
  only **1** in-scope hit (`openssl/openssl`), because it only sees files
  physically served at repo `HEAD`. The FLOSS Fund directory export we already
  download (`data/sources/floss-fund/funding-json.csv`, 599 manifests) lists
  **4** in-scope repos as registered projects (`browserify/resolve`,
  `eemeli/yaml`, `openssl/openssl`, `vuejs/core`) — strictly more coverage and
  one tarball instead of 900 requests.

## Goal

Distribute the raw funding signals to their source folders, derive
`has_funding_json` from the existing FLOSS Fund export, delete `funding_class`,
and leave **only the aggregated `funding.csv`** under `data/risk/`.

## Target architecture

### Sources (raw, `data/sources/`)

- **`data/sources/github/sponsors.csv`** — `repo, repo_id, github_sponsors,
  sponsors_status, fetched_at`. Produced by **`src/github/fetch_sponsors.py`**.
  `sponsors_status` ∈ `{ok, error}` so a `0` from a *failed* GraphQL query is
  distinguishable from a genuine `0` (auditability rule). Migrated rows = `ok`.
- **`data/sources/github/funding-yml.csv`** — `repo, repo_id, has_funding_yml,
  funding_yml_platforms, funding_yml_github, fetched_at`. Produced by
  **`src/github/fetch_funding_yml.py`**. `funding_yml_github` is the
  comma-separated list of usernames under the `github:` key — consumed by the
  sponsors fetcher to also count co-maintainer sponsorships.
- **`data/sources/floss-fund/funding-json.csv`** — the FLOSS Fund directory
  export. Produced by **`src/floss_fund/funding_json.py`** (moved from
  `src/funding/funding_json.py`; folder uses an underscore because it is an
  importable package, while the data folder keeps the hyphen). Unchanged
  output schema.

### Build (aggregate, `data/risk/`)

**`src/pipeline/risk/build_funding.py`** joins the sources →
`data/risk/funding.csv`:

```
repo, repo_id, github_sponsors, has_funding_yml,
funding_yml_platforms, has_funding_json, foundation_host, fetched_at
```

- `github_sponsors`, `has_funding_yml`, `funding_yml_platforms` ← the two
  github source files.
- `has_funding_json` ← `True` iff the repo (normalized `owner/repo`, case-fold,
  strip `.git`/trailing slash) matches a `project_repository` URL in the
  floss-fund export. **No per-repo fetch.**
- `foundation_host` ← `data/sources/foundations/host-by-repo.csv` (unchanged).
- `fetched_at` ← most recent of the contributing source rows' timestamps.
- **No `funding_class`.**

### Fetch ordering

`fetch_funding_yml.py` → `fetch_sponsors.py` (sponsors reads
`funding_yml_github`) → `funding_json.py` (independent) → `build_funding.py`.

## Migration (one-time)

`scripts/migrate-funding-data.py` splits the existing
`data/risk/funding-data.csv` into `sponsors.csv` + `funding-yml.csv`,
preserving fetched values and timestamps (no GitHub re-query;
`funding_yml_github` left empty, repopulated on next real fetch). Then
`data/risk/funding-data.csv` is removed. `has_funding_json` is **not** migrated
— it is derived fresh from the export.

## Cleanup / ripple

- Delete `src/github/fetch_funding.py`.
- Remove `funding_class()` from the codebase and the now-dead
  `risk_classification.funding` block in `src/pipeline/settings.json` (and its
  `FUNDING_THRESHOLDS` binding in `common/params.py`).
- Drop `funding_5y` from the pipeline (empty for every in-scope repo; only came
  from the deleted per-repo fetch).
- Update `src/pipeline/run_risk_pipeline.py`: replace the single `funding`
  fetch step with `funding-yml` + `sponsors` fetch steps (and a floss-fund
  export fetch step before `funding-build`).
- Update `CLAUDE.md` data-organization section (line referencing "the raw
  `funding-data.csv` fetch").
- Regenerate `data/risk/funding.csv`, then `data/risk/risk.csv` (which loses
  its `funding_class` column).

## Expected output diffs

- `has_funding_json` flips `False`→`True` for 3 repos (`browserify/resolve`,
  `eemeli/yaml`, `vuejs/core`) — a correctness improvement.
- `funding.csv` and `risk.csv` lose `funding_class`.

## Out of scope

- Extracting 5-year income (`funding_5y`) from the export manifests' history
  block — deferred (currently empty for all in-scope repos). If needed later,
  extend `funding_json.py` to parse `funding.history[]`.

## Tests

- URL normalization + export matching for `has_funding_json` (incl. `.git`,
  trailing slash, case, non-github URLs).
- `build_funding.py` join: a repo present in each / none of the sources.
- Migration split: round-trips values from a sample `funding-data.csv`.
- `funding.csv` / `risk.csv` no longer contain `funding_class`.
