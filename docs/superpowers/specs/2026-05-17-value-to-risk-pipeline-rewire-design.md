# Value → Risk pipeline rewire — design

**Date:** 2026-05-17
**Status:** approved (design)

## Goal

Make the risk pipeline consume **A/B value-class repos** directly from
`value-data.csv` instead of the eligibility-filtered set
(`eligibility-data.csv`). The target value classes are configurable in the
renamed config file `settings.json`. Update pipeline docs to the new order.

## Pipeline change

Before: **Value → Eligibility → Risk** (Risk reads `eligibility-data.csv`).

After: **Value → Risk → Eligibility** — Risk reads A/B-class repos straight
from `value-data.csv`; Eligibility moves *after* Risk and (target state)
narrows to `value_class=A` ∩ highest risk class.

Scope of *this* task: rewire the risk pipeline + docs. Rewriting
`eligibility.py`'s own input logic is a **follow-up** — docs describe the
target state, code is left runnable as-is.

## 1. Config: `params.json` → `settings.json`

- `git mv src/pipeline/params.json src/pipeline/settings.json`.
- `src/pipeline/params.py`: update `_PARAMS_PATH` to `settings.json`. The
  **module stays named `params.py`** (it is the loader; only the data file
  is renamed) — so the ~8 `from src.pipeline.params import …` call sites are
  untouched.
- Update doc mentions of `params.json` in `docs/risk.md`, `docs/value.md`.
- New block in `settings.json`:

```json
"risk_input": {
  "value_classes": ["A", "B"],
  "comment": "Risk pipeline runs on repos whose value-data.csv class is in this list."
}
```

- `params.py` exposes `RISK_INPUT_CLASSES: list[str] = _P["risk_input"]["value_classes"]`.

## 2. Shared loader: `load_risk_repos()`

Lives in `src/pipeline/repos.py` (the risk-pipeline shared module). One
canonical loader every risk script calls.

- Reads `data/value-data.csv`; keeps rows whose `class ∈ RISK_INPUT_CLASSES`.
- Dedups by lowercased `github_repo`, highest class wins (A > B).
- Drops orphan rows (empty `github_repo`) and `gh_valid=False` (404) repos —
  metrics can't be fetched for them.
- Resolves `repo_id` + `archived` + `size`/`stars` from
  `data/github/repos.csv` (the new authoritative repo-metadata source,
  replacing both `top-repos.csv` enrichment and `eligibility-data.csv`
  repo_id lookups). `skip_archived=True` default.
- Returns `list[RepoEntry]` sorted by slug.
- Add `load_risk_slugs()` convenience wrapper (slugs only).
- Add `load_repo_ids()` helper → `{repo: repo_id}` from `github/repos.csv`,
  for scripts that currently build that map off `eligibility-data.csv`.

`load_eligible_repos()` is **kept** (eligibility still uses it) but no longer
called by any risk script. `load_risk_repos` is the renamed/generalised
`load_ab_repos` (now settings-driven instead of hardcoded `{A, B}`);
`load_ab_repos`/`load_ab_slugs` remain as thin aliases so the untouched
`fetch_repo_owner_data.py` import keeps working.

## 3. Scripts to rewire (~21)

All swap their input loader to `load_risk_repos()` and move any
`repo→repo_id` lookup off `eligibility-data.csv` onto `github/repos.csv`.
"eligible" wording in banners/docstrings/argparse help updated to
"risk-scope" / "A/B value-class".

**Builders + aggregator (`src/pipeline/`):** `build_concentration.py`,
`build_complexity.py`, `build_funding.py`, `build_security.py`,
`build_visibility.py`, `build_workload.py`, `risk.py`.

**Fetchers:** `src/osv/fetch_cves.py`, `src/depsdev/fetch.py`,
`src/ossinsight/fetch.py`, `src/openssf/scorecard.py`,
`src/github/fetch_churn.py`, `fetch_funding.py`, `fetch_semgrep.py`,
`fetch_cognitive.py`, `fetch_advanced_complexity.py`,
`fetch_issue_metrics.py`, `fetch_contributors_metrics.py`,
`src/git/fetch_scc.py`, `src/git/commits_years.py`, `src/git/resolve_head.py`.

**Not touched:** `git/migrations/*` (one-off migration scripts),
`eligibility.py`, `github/resolve_licenses.py`,
`github/fetch_repo_owner_data.py` (value/eligibility-stage fetcher; already
class-driven). `value.py` is touched only for §4 below, not the loader swap.

## 4. Value pipeline: include all classes

`value.py` currently drops D-class repos before writing — `aggregate_by_repo`
has `drop_d_class=True` (default) and `value-data.csv` stores only A/B/C.

Change: include **all classes (A/B/C/D)** in the final `value-data.csv`.
Flip `drop_d_class` default to `False` (single call site at `value.py:942`;
no CLI flag). Update the in-code comment block (`value.py:352-357`) and the
module docstring that say "value-data.csv only stores ABC".

The risk loader still filters `class ∈ {A, B}`, so the extra D rows are inert
for Risk — this purely makes `value-data.csv` the complete long-tail table.

## 5. Docs to update

- `docs/pipeline.md` — pipeline order, dataflow diagram, funnel table,
  "How to refresh" run order → Value → Risk → Eligibility.
- `docs/risk.md` — input is `value-data.csv` (`class ∈ settings.risk_input`),
  not `eligibility-data.csv`; `params.json` → `settings.json`; drop "eligible"
  framing; refresh coverage/count language.
- `docs/value.md` — `params.json` → `settings.json`; pipeline-order mention;
  value-data.csv now includes D-class (all classes).
- `docs/eligibility.md` — note Eligibility now runs after Risk, target scope
  `value_class=A` ∩ highest risk class.

## 6. Run plan (collect missing data + perf stats)

1. Smoke-test the rewiring with `--limit`/`--random` on each builder + fetcher.
2. Run the 6 builders + `risk.py` → coverage report shows exact gaps on the
   new ~881-repo A/B scope.
3. Run TTL-aware fetchers to fill missing data (only the newly-in-scope
   repos + pre-existing holes are fetched).
4. Re-run builders + aggregator.
5. Report per-stage perf: items/sec, elapsed, repo counts, coverage %.

## Testing

- `tests/` — update any test that asserts the eligible-set loader; add a
  `load_risk_repos()` test (class filter from settings, invalid/archived
  skipped, repo_id resolved).
- `uv run pytest` green before the data run.

## Out of scope

- Rewriting `eligibility.py` to consume `value_class=A` ∩ risk class A.
- Renaming the `params.py` module.
- Touching `git/migrations/*`.
