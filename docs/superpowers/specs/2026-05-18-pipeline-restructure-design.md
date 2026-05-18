# src/pipeline restructure — design

**Date:** 2026-05-18
**Status:** approved (design)

## Goal

Reorganise `src/pipeline/` so each pipeline stage lives in its own folder
with clearly-named per-step scripts, and the top level holds only the
orchestrators + config. **Pure restructure — no behaviour or logic changes.**

## Target layout

```
src/pipeline/
  __init__.py
  settings.json
  run_value_pipeline.py          orchestrator
  run_risk_pipeline.py           orchestrator
  run_eligibility_pipeline.py    orchestrator
  common/
    __init__.py
    params.py        settings.json loader
    repos.py         load_risk_repos / load_risk_slugs / load_repo_ids /
                     canonical_repo_map / load_eligible_repos
  value/
    __init__.py
    build_ecosystem_downloads.py   <- calculate_ecosystem_downloads.py
    build_git_urls.py              <- build_git.py
    unify_value_data.py            <- value.py core (collect + aggregate + write)
    verify_git_urls.py             <- value.py ls-remote git-URL verifier block
  risk/
    __init__.py
    build_concentration.py  build_complexity.py  build_security.py
    build_funding.py  build_visibility.py  build_workload.py   (moved as-is)
    aggregate_risk.py              <- risk.py
  eligibility/
    __init__.py
    eol_common.py                  (moved as-is)
    classify_eligibility.py        <- eligibility.py
```

Top level after the move: 3 runners + `settings.json` + `__init__.py` + 4
folders. Nothing else.

## value.py split (2-way)

`value.py` (970 lines) splits into two step scripts:

- **`value/unify_value_data.py`** — the collect -> aggregate -> write core:
  `collect_ecosystem`, the `_read_*` readers, `aggregate_by_repo` and its
  grouping helpers, `write_value_data`, the funnel/eol/class display tables,
  and the module constants (`ECOSYSTEMS`, `FIELDS`, `CLASS_RANK`, `OUTPUT_FILE`,
  etc.). One in-memory data flow producing `value-data.csv`. Name matches the
  existing `tests/test_unify_value_data.py`.
- **`value/verify_git_urls.py`** — the ls-remote git-URL verifier (value.py
  lines ~493-930): `_lsremote_pass`, `_canonicalize_git_url`, `_verify_non_github`,
  `verify_urls_in_aggregates`, `_host`, the validity-cache helpers, and the
  `_print_git_validity_table` display. Has its own cache `data/git/urls.csv`.

A finer split (separate `collect`/`aggregate` scripts) was rejected — those
sub-steps share in-memory structures, so splitting them would require new
intermediate CSV schemas for no real gain.

The two already-standalone value steps move in unchanged (renamed for
clarity): `calculate_ecosystem_downloads.py` -> `build_ecosystem_downloads.py`,
`build_git.py` -> `build_git_urls.py`.

## Runners

Each runner chains its stage's steps in dependency order and exposes
`--from <step>` / `--only <step>` flags. Steps that live outside
`src/pipeline/` (per-ecosystem `process_data.py`, the ~14 risk fetchers in
`src/<source>/`) are **invoked by** the runner but stay in their source
folders — they are not moved.

- **`run_value_pipeline.py`** — `build_git_urls` -> `build_ecosystem_downloads`
  -> per-ecosystem `process_data.py` (npm/crates/pypi/debian/homebrew) ->
  `unify_value_data` -> `verify_git_urls`. Runs the full chain by default.
- **`run_risk_pipeline.py`** — defaults to the cheap projection part: the 6
  `build_*` scripts -> `aggregate_risk`. A `--with-fetchers` flag prepends the
  multi-hour fetch stage (commits_years, contributors, scc, churn, semgrep,
  cognitive, issues, cves, scorecard, depsdev, funding, ...).
- **`run_eligibility_pipeline.py`** — runs `classify_eligibility` (and its
  upstream license/foundation/repo-owner fetchers as a fetch stage flag,
  consistent with the risk runner).

## Import migration

42 files import `src.pipeline.*`. Every import path is mechanically updated:

| Old | New |
|---|---|
| `src.pipeline.params` | `src.pipeline.common.params` |
| `src.pipeline.repos` | `src.pipeline.common.repos` |
| `src.pipeline.build_<dim>` | `src.pipeline.risk.build_<dim>` |
| `src.pipeline.risk` | `src.pipeline.risk.aggregate_risk` |
| `src.pipeline.build_git` | `src.pipeline.value.build_git_urls` |
| `src.pipeline.calculate_ecosystem_downloads` | `src.pipeline.value.build_ecosystem_downloads` |
| `src.pipeline.value` | `src.pipeline.value.unify_value_data` (+ `verify_git_urls`) |
| `src.pipeline.eligibility` | `src.pipeline.eligibility.classify_eligibility` |
| `src.pipeline.eol_common` | `src.pipeline.eligibility.eol_common` |

Files are relocated with `git mv` to preserve history. `value.py` splitting:
`git mv` value.py -> `unify_value_data.py`, then move the verifier block out
into `verify_git_urls.py` and fix the cross-imports between the two.

## Testing

- Update every test's import paths to the new module locations. The
  `test_unify_value_data.py` imports of value.py internals are repointed at
  `unify_value_data` / `verify_git_urls` as appropriate.
- Each runner is smoke-tested (`--help`, and `--only` on one cheap step).
- `uv run pytest` must stay at **225 passed / 7 pre-existing failures** — zero
  new failures. The 7 pre-existing failures are unrelated WIP schema drift.

## Out of scope

- Moving per-ecosystem `process_data.py` or the risk fetchers — they stay in
  `src/<source>/`.
- Any change to pipeline logic, metrics, schemas, or behaviour.
- Splitting `eligibility.py` or `build_git.py` internally — they move whole.
