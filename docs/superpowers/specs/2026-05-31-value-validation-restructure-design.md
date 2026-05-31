# Value-stage validation restructure — design

**Date:** 2026-05-31
**Status:** approved (pending written-spec review)

## Goal

Consolidate git/GitHub validation in the value stage into a single audit
table and a single `valid` column, make validation a **hard gate** over the
full value universe, and move all manual overrides to one file.

Three artifacts change:

1. `data/value/value.csv` — drop `gh_valid`, `git_valid`, `llm_guess`; add a
   single `valid` column.
2. `data/value/validation.csv` — **new** aggregated validation audit table.
3. `data/value/overrides.csv` — renamed from `value-repo-overrides.csv`,
   restructured to hold every kind of manual override.

## value.csv schema

**Drop:** `gh_valid`, `git_valid`, `llm_guess`.
**Add:** `valid` (tri-state string).
**Keep:** `gh_repo_id` (identity, not validity).

New header:
```
id, github_repo, gh_repo_id, git_url, valid, ecosystems, packages,
top_eco, top_eco_pkg, top_eco_pct, class, class_npm, class_pypi,
class_crates, class_cpp
```

`valid` semantics, per row:
- `True` — every hosting target the row carries is valid in `validation.csv`.
- `False` — at least one of the row's targets is invalid.
- empty — the row has **no** target (orphan: blank `github_repo` and `git_url`).

A row's targets: its `github_repo` (validated via GitHub API) and/or its
`git_url` when that URL is a non-GitHub host (validated via `git ls-remote`).
In current data each row has exactly one real target — a github row's derived
`https://github.com/owner/name.git` is **not** separately validated. The "all
targets valid" rule generalises if a row ever carries both a github repo and a
distinct non-github URL.

`llm_guess` is removed entirely (noisy provenance tag, unused downstream).

## validation.csv (new)

`data/value/validation.csv`, one row per distinct validated target:
```
target, type, sources, checked_at, valid
```
- `target` — `owner/name` (when `type=github_repo`) or the full URL (when
  `type=git_url`).
- `type` — `github_repo` | `git_url`.
- `sources` — comma-joined, sorted ecosystems whose packages resolve to this
  target (e.g. `npm,pypi` or `debian`). Derived at build time from the value
  grouping.
- `checked_at` — ISO timestamp of the validation (from the source cache row).
- `valid` — `True` | `False`.

This is a **rollup** of the two existing source-of-truth caches, keyed by
target:
- `data/sources/github/repos.csv` — `valid` + `fetched_at` per github repo.
- `data/sources/git/urls.csv` — `valid` + `checked_at` per non-github URL.

No new fetching happens here; `verify_git_urls` populates those caches first.

## overrides.csv (renamed + restructured)

`data/value/value-repo-overrides.csv` → `data/value/overrides.csv`.

The single home for every manual correction of a package's repo data.
Restructured columns:
```
package, ecosystem, github_repo, git_url, valid, reason
```
- `package`, `ecosystem` — the key (matches a constituent package of a value
  group). Unchanged.
- `github_repo` — force the corrected GitHub slug (existing behaviour).
- `git_url` — **new**, optional: force a corrected non-github clone URL for
  groups whose canonical upstream is not on GitHub. When set and
  `github_repo` is blank, the group's identity becomes this URL.
- `valid` — **new**, optional: manually pin the resolved target's validity
  (`True`/`False`). Rescues an automated false-negative (a real repo the API
  404s) or force-excludes a known-bad one. Blank = let automated validation
  decide.
- `reason` — required free-text justification (existing).

Precedence: at most one of `github_repo` / `git_url` sets identity; if both
are blank the row is a pure `valid`-pin (identity unchanged). A blank
`reason` is rejected by the loader (these are curated, must be explained).

Identity overrides (`github_repo`/`git_url`) apply where they do today — in
`unify_value_data.apply_repo_overrides`, before validation runs. The `valid`
pin applies in `build_validation` (see below), since validity is computed
there.

## build_validation.py (new step)

`src/pipeline/value/build_validation.py` — runs after `verify_git_urls`.

Responsibilities:
1. Load every distinct target from `value.csv` (github repos + non-github
   `git_url`s), each with its `sources` (the ecosystems of its member rows —
   available from the grouping carried in `value.csv`'s `ecosystems` column,
   joined per target).
2. Read the two source caches and the `valid` pins from `overrides.csv`.
3. **Hard-gate assertion:** every target must have a verdict (cache row, or
   override pin). A target with no verdict is a pipeline error — fail loudly
   with the offending targets listed, never silently treat missing as
   invalid (per the project auditability rule). The fix is to (re)run
   `verify_git_urls`; `build_validation` does not fetch.
4. Write `validation.csv` (target/type/sources/checked_at/valid), overrides
   winning over cache.
5. Join `valid` back into `value.csv`: for each row, `valid` = AND of its
   targets' verdicts (`True` if all valid, `False` if any invalid, empty if
   no target).

`verify_git_urls` is trimmed: it still canonicalises URLs, validates github +
non-github targets into the source caches, and rewrites `github_repo` to
current post-rename names — but **no longer writes** `gh_valid`/`git_valid`
into `value.csv` (those columns are gone). The validity verdict now lives in
`validation.csv` and the joined `valid` column, both written by
`build_validation`.

## Coverage: full universe, hard gate

Validation runs over **all classes (A–D)**, not just risk-scope A/B. This is
already effectively true — the caches hold ~10.5k targets and only ~2 needed
fetching on the last run — but the gate makes it a guarantee: a value-pipeline
run cannot finish with an unvalidated target in `value.csv`.

`value.csv` is **not** filtered — `valid=False` and orphan rows stay. The
**risk pipeline** applies the validity filter (keep `valid=True`) alongside
its existing A/B class filter. (Spec for that filter lives with the risk
stage; out of scope here beyond noting the contract.)

## Pipeline flow (run_value_pipeline)

```
… ecosystem pipelines …
unify_value_data   → value.csv  (targets + ecosystems; identity overrides applied; no `valid` yet)
verify_git_urls    → validate ALL targets → refresh github/repos.csv + git/urls.csv;
                     rewrite github_repo to current names
build_validation   → validation.csv (aggregate caches + `sources` + valid-pins);
                     assert full coverage; join `valid` into value.csv
```

Add a `validation` step to `STEPS` in `run_value_pipeline.py` after `verify`.

## Migration

- `git mv data/value/value-repo-overrides.csv data/value/overrides.csv`; add
  the `git_url` and `valid` columns (empty for existing 14 rows).
- Update refs: `unify_value_data.py` (`OVERRIDES_FILE`, `load_repo_overrides`,
  `apply_repo_overrides` to read/apply `git_url`), `tests/test_unify_value_data.py`,
  `docs/value.md`, project `CLAUDE.md` (Data Organization → value stage).
- `value.csv` is regenerated by a `unify → verify → build_validation` run,
  dropping the three columns and adding `valid`; `validation.csv` is created
  by that run. (No manual data edits.)

## Auditability

- `validation.csv` makes every verdict traceable to a `checked_at` and a
  source (cache row or override). The hard gate guarantees no silent
  "missing = invalid".
- `valid` in `value.csv` is a pure projection of `validation.csv` — no
  independent state.
- Override-driven verdicts are explained by the required `reason`.

## Testing

- `tests/test_build_validation.py` (new): target extraction from value rows;
  `sources` aggregation across ecosystems; AND-of-targets → row `valid`
  (True/False/empty for orphan); override `valid`-pin wins over cache;
  hard-gate raises on a missing verdict.
- Update `tests/test_unify_value_data.py`: new overrides schema + `git_url`
  override; assert dropped columns (`gh_valid`/`git_valid`/`llm_guess`) absent
  and `valid` present in the written schema.

## Out of scope

- The risk-stage validity filter (noted as a contract only).
- The separate stats.csv spec (2026-05-31) — independent; its
  `git_urls`/`github_repos` counts may later read validated counts, tracked
  there.
- No new network fetching beyond what `verify_git_urls` already does.
