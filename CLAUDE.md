# Project Guidelines

## Running the Pipeline

**Use `scripts/run-pipeline.sh` — it is the entry point for running the model.**
It chains all four stages in order (Value → Risk → Eligibility → Preview) and
finishes with `scripts/pipeline_health.py`. Warm run ≈2 min.

```bash
scripts/run-pipeline.sh              # normal run (TTL-cached)
scripts/run-pipeline.sh --refresh    # force refetch past every TTL
scripts/run-pipeline.sh --offline    # pure-cache run, no network
```

Args pass through to every stage runner. **Never hand-chain the stage runners
instead** — the Preview stage (`src.run_preview_pipeline`: results → people →
preview-xlsx) is the one people forget, which silently leaves
`data/preview/preview.xlsx` stale.

The per-stage runners are for *targeted* work only:

| Command | Use for |
|---|---|
| `uv run python -m src.<stage>.run_<stage>_pipeline` | one stage (`--list`, `--from STEP`, `--only STEP`) |
| `uv run python -m src.value.run_value_pipeline --rollup` | rebuild `value.csv` from existing per-eco `results.csv` (skips ecosystem sub-pipelines) |
| `uv run python -m scripts.pipeline_health` | consistency check — **all checks must pass** before a change is done |

A red `pipeline_health` check is a bug, not noise: it catches stale builds and
override rows orphaned by a repo-identity change.

## Code Organization

`src/` is organized by **role**, mirroring the three-stage Value → Risk →
Eligibility pipeline and the `data/` layout. A script's location tells
you what it is:

- `src/sources/<source>/` — source-related scripts: everything that fetches or
  processes data from one external source. One folder per source — ecosystem
  registries (`npm/`, `pypi/`, `crates/`, `cpp/`, `debian/`, `homebrew/`), code/Git
  analysis (`git/`, `github/`, `gitlab/`), and the standalone sources (`osv/`,
  `openssf/`, `depsdev/`, `osi/`, `ossfuzz/`, `repology/`, `floss_fund/`,
  `funding/`, `opencollective/`, `ecosystems/`). Module folders use
  underscores (importable); the matching `data/sources/` folder may use a hyphen
  (e.g. code `floss_fund/` ↔ data `floss-fund/`).
- `src/common/` — shared infrastructure used across stages: `params.py`, `repos.py`,
  `tables.py`, `stats.py`, `pipeline_runner.py`, `funding_platforms.py`.
- `src/value/`, `src/risk/`, `src/eligibility/` — pipeline-stage scripts:
  the per-dimension builders for that stage **plus** its orchestrator
  (`run_<stage>_pipeline.py`, run via
  `uv run python -m src.<stage>.run_<stage>_pipeline`). The eligibility
  stage's source inputs (`src/sources/osi/`, `src/sources/funding/`, the
  per-ecosystem `check_eol.py` / `fetch_licenses.py`,
  `fetch_repo_owner_data`) stay under `src/sources/`.
- `src/settings.json` — model parameters/config at the `src/` root (loaded by `src/common/params.py`).
- Only truly general-purpose scripts (tied to no source or stage) go in a top-level
  `scripts/` folder. Never leave a script in a bare `scripts/` folder if it belongs
  to a specific source or stage.

## Data Organization

`data/` mirrors the three-stage pipeline (Value → Risk → Eligibility), with all external-source data isolated under `data/sources/`:

- `data/sources/<source>/` — raw + intermediate data fetched from external sources. One folder per source: ecosystem registries (`npm/`, `pypi/`, `crates/`, `cpp/`, `debian/`, `homebrew/`), code/Git analysis (`git/`, `github/`, `gitlab/`), and the standalone sources (`osv/`, `openssf/`, `depsdev/`, `osi/`, `ossfuzz/`, `ossinsight/`, `repology/`, `endoflife/`, `floss-fund/`, `funding/`, `opencollective/`).
- `data/value/` — Value-stage outputs: `value.csv` (the unified per-repo value table; carries a strict True/False `git_valid` column), `validation.csv` (git/GitHub validation audit table — rollup of the source caches), `overrides.csv` (curated manual repo/validity corrections), `stats.csv` (per-ecosystem stats matrix: metric rows × ecosystem columns — downloads per year + package/repo counts).
- `data/risk/` — Risk-stage outputs: `risk.csv` (final aggregated risk table) plus the per-dimension builds (`concentration.csv`, `complexity.csv`, `security.csv`, `workload.csv`).
- `data/eligibility/` — Eligibility-stage outputs: `eligibility.csv` (the rollup: `eligible = oss AND intent AND nonprofit AND active`) plus the per-dimension builds (`licenses.csv`, `active.csv`, `funding.csv`) and `overrides.csv` (curated per-repo host/owner backing, OC slug, and the manual `eol` verdict). Raw funding signals live under `data/sources/github/` (`sponsors.csv` inbound, `sponsorships.csv` outbound, `funding-yml.csv`), `data/sources/floss-fund/` (`funding-json.csv`), `data/sources/opencollective/` (`budgets.csv`), and `data/sources/funding/` (foundation rosters + `host-by-repo.csv`); license/EOL source signals under `data/sources/osi/` and the per-ecosystem folders.

Rule: a script reading external/fetched data points at `data/sources/<source>/…`; a script reading or writing a stage result points at `data/<stage>/…`. Never write a stage output into `data/sources/`, and never write fetched source data into a stage folder.

Exception: regenerable vendor-dump data never enters git/LFS. The raw 3.9 GB crates db-dump is transient scratch in the gitignored `tmp/`; `fetch_db_dump` slims it to the pipeline-read columns in `data/sources/crates/db-dump/` (~560 MB, gitignored) — re-downloadable on demand.

## Documentation

`docs/` mirrors the pipeline. Keep the `docs/` root to **exactly one page per stage** — `value.md`, `risk.md`, `eligibility.md` — plus **`docs/data-sources.md`**, with everything else in a subfolder:

- `docs/data-sources.md` — the source × stage matrix: one row per external source (favicon + link to its `sources/` page), columns Value / Risk / Eligibility.
- `docs/sources/<source>.md` — one page per external data source.
- `docs/components/<component>.md` — cross-cutting component docs (e.g. `validation.md`, how `data/value/validation.csv` is formed).

When a doc's content spans multiple stages, fold it into the relevant stage page(s) rather than adding a new top-level overview doc.

### Stats live only on the preview stats sheet

**Every pipeline/funnel/coverage/distribution figure lives on the `stats` sheet of `data/preview/preview.xlsx` and nowhere else.** The sheet is rendered at build time by `src.build_preview_workbook` from `scripts/stats.py` (the generator — every figure recomputed from the live CSVs; `--markdown` emits the same tables as text). There is no stats document in `docs/` to refresh or drift. That covers: per-stage funnel counts (packages → dep tree → results → with-repo), class/score distributions, repo-identity coverage, and per-component "N of the top repos carry signal X" coverage tables (Risk and Eligibility share one scope — the valid class-A set **including archived repos**, which surface in eligibility as `active=False`).

- Methodology pages (`value.md`, `risk.md`, `eligibility.md`, the component and source docs) describe **how** a metric is built (formulas, schemas, column descriptions, worked illustrative examples) and **point to** the preview stats sheet for **how many** — they must not restate the counts.
- A single concrete number that *defines* a parameter (e.g. "top = 95% of cumulative downloads") stays in the methodology page — it's config, not a result.
- One number, one place: adding a new figure means adding it to `scripts/stats.py`, never hard-coding it in a doc. A coverage/funnel count found in any page is a bug — move it into the generator and leave a pointer.

## Philosophy

- **Performance AND clarity** — scripts must be fast (async I/O, batching, concurrency) but also easy to read and audit. These are not in conflict: optimize with explicit, well-named code rather than clever tricks.
- A researcher unfamiliar with the codebase should be able to follow exactly what a script does and why — even if it uses async or batching.
- Data transformations must be traceable — it should always be clear where each output value came from.
- Prefer flat, explicit steps. Use abstractions only when they make the code *more* readable, not less.
- Name things clearly. A well-named function or variable is worth more than a comment.

## Auditability

- **The model pipeline must be auditable end to end.** Every metric in an
  output CSV must be traceable back to the fetch that produced it.
- **Every fetch must record a date and a success flag** — unless both are
  already obvious from the data itself. The date is when the value was
  fetched (`fetched_at` / `checked_at`). The success flag distinguishes
  "checked, genuinely absent/zero" from "fetch failed / never ran" — a
  missing or `False`/`0` value must never silently stand in for a network
  error or timeout.
  - "Obvious from the data" means the value is self-evidently a real
    measurement (e.g. a non-empty count with a `fetched_at`). When a
    `False`/empty/`0` outcome could equally mean "failed", add an explicit
    status column (e.g. `*_checked`, `*_status`, a sidecar `queried.csv`).
- **Repo-keyed source schema contract**: any fetched source CSV whose rows
  are keyed by a GitHub repo *name* must also carry (1) a **`repo_id`**
  column — the stable numeric GitHub id resolved at fetch time (blank only
  when genuinely unresolvable, never invented) — and (2) a **fetch-date**
  column (`fetched_at`/`checked_at`/`date`), either per row or in a
  documented per-repo `.status.csv` sidecar. Rationale: slugs drift on
  renames; every downstream join is by `repo_id`, so an id-less rewrite
  silently blanks whole dimensions. `scripts/pipeline_health.py`
  (`check_source_repo_id_integrity` + `check_source_schema_contract`)
  enforces this. Exemptions: value-stage identity-resolution files (they
  *produce* the ids), vendor dumps (crates db-dump, nice-registry), and
  files keyed by non-repo entities (OC slugs, user logins).

## Git

- **Never push to this repo without explicit user approval in the current message.** Always commit locally and ask before pushing.

## Stack

- Python with `uv` for package management (`uv run` to execute scripts)
- Async I/O with `aiohttp` for data fetching
- `rich` for terminal output (progress bars, tables)
