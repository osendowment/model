# Project Guidelines

## Code Organization

`src/` is organized by **role**, mirroring the Value → Risk pipeline (plus a
manual eligibility review) and the `data/` layout. A script's location tells
you what it is:

- `src/sources/<source>/` — source-related scripts: everything that fetches or
  processes data from one external source. One folder per source — ecosystem
  registries (`npm/`, `pypi/`, `crates/`, `cpp/`, `debian/`, `homebrew/`), code/Git
  analysis (`git/`, `github/`), and the standalone sources (`osv/`, `openssf/`,
  `depsdev/`, `osi/`, `ossfuzz/`, `repology/`, `floss_fund/`,
  `opencollective/`, `foundations/`, `ecosystems/`). Module folders use
  underscores (importable); the matching `data/sources/` folder may use a hyphen
  (e.g. code `floss_fund/` ↔ data `floss-fund/`).
- `src/common/` — shared infrastructure used across stages: `params.py`, `repos.py`,
  `tables.py`, `stats.py`, `pipeline_runner.py`, `funding_platforms.py`.
- `src/value/`, `src/risk/` — pipeline-stage scripts: the per-dimension
  builders for that stage **plus** its orchestrator (`run_<stage>_pipeline.py`,
  run via `uv run python -m src.<stage>.run_<stage>_pipeline`). Eligibility
  has no stage folder of its own — it is a **manual review** of the top
  candidates, not an automated stage. The source inputs that inform it
  (`src/sources/osi/`, `src/sources/foundations/`, the per-ecosystem
  `check_eol.py` / `fetch_licenses.py`, `fetch_repo_owner_data`) are kept
  under `src/sources/`.
- `src/settings.json` — model parameters/config at the `src/` root (loaded by `src/common/params.py`).
- Only truly general-purpose scripts (tied to no source or stage) go in a top-level
  `scripts/` folder. Never leave a script in a bare `scripts/` folder if it belongs
  to a specific source or stage.

## Data Organization

`data/` mirrors the two-stage pipeline (Value → Risk), with all external-source data isolated under `data/sources/`:

- `data/sources/<source>/` — raw + intermediate data fetched from external sources. One folder per source: ecosystem registries (`npm/`, `pypi/`, `crates/`, `cpp/`, `debian/`, `homebrew/`), code/Git analysis (`git/`, `github/`), and the standalone sources (`osv/`, `openssf/`, `depsdev/`, `osi/`, `ossfuzz/`, `ossinsight/`, `repology/`, `endoflife/`, `floss-fund/`, `opencollective/`, `foundations/`).
- `data/value/` — Value-stage outputs: `value.csv` (the unified per-repo value table; carries a tri-state `valid` column), `validation.csv` (git/GitHub validation audit table — rollup of the source caches), `overrides.csv` (curated manual repo/validity corrections), `stats.csv` (per-ecosystem stats matrix: metric rows × ecosystem columns — downloads per year + package/repo counts).
- `data/risk/` — Risk-stage outputs: `risk.csv` (final aggregated risk table) plus the per-dimension builds (`concentration.csv`, `complexity.csv`, `security.csv`, `funding.csv`, `visibility.csv`, `workload.csv`). Raw funding signals live under `data/sources/github/` (`sponsors.csv` inbound, `sponsorships.csv` outbound, `funding-yml.csv`), `data/sources/floss-fund/` (`funding-json.csv`), and `data/sources/opencollective/` (`budgets.csv`).

(Eligibility is a manual review, not a pipeline stage — it has no stage output folder. Its source signals live under `data/sources/` (`osi/`, `foundations/`, per-ecosystem `eol.csv`, license data).)

Rule: a script reading external/fetched data points at `data/sources/<source>/…`; a script reading or writing a stage result points at `data/<stage>/…`. Never write a stage output into `data/sources/`, and never write fetched source data into a stage folder.

## Documentation

`docs/` mirrors the pipeline. Keep the `docs/` root to **exactly one page per stage** — `value.md`, `risk.md` — with everything else in a subfolder:

- `docs/sources/<source>.md` — one page per external data source.
- `docs/components/<component>.md` — cross-cutting component docs (e.g. `validation.md`, how `data/value/validation.csv` is formed).

When a doc's content spans multiple stages, fold it into the relevant stage page(s) rather than adding a new top-level overview doc.

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

## Git

- **Never push to this repo without explicit user approval in the current message.** Always commit locally and ask before pushing.

## Stack

- Python with `uv` for package management (`uv run` to execute scripts)
- Async I/O with `aiohttp` for data fetching
- `rich` for terminal output (progress bars, tables)
