# crates.io (Rust)

The crates.io source page for the Rust ecosystem: package downloads,
dependencies, and repository mappings. It covers the fetch mechanics and the
[Value pipeline](../value.md) assembly that turns them into a download-weighted
PageRank and an A/B/C value class for every crate.

## Data Sources

| Signal | Source | Notes |
|---|---|---|
| Crate metadata + dep graph | [static.crates.io/db-dump.tar.gz](https://static.crates.io/db-dump.tar.gz) | Crate/version names, dependency edges, and each crate's `repository` URL. The server supports `Accept-Ranges`, so the fetcher downloads the dump as parallel byte-range chunks |
| Downloads | [static.crates.io/archive/version-downloads/](https://static.crates.io/archive/version-downloads/) | One CSV per day of per-version counts. The fetcher pulls a month's daily files concurrently and aggregates them locally. It writes a month only when every day of it succeeded |
| Licenses | the DB dump | `license` on the crate's default version |
| EOL | the DB dump | A crate is EOL when its default version is `yanked` |

No authentication required.

## Raw Data

`data/sources/crates/db-dump/` holds slim extracts with only the columns the
pipeline reads (~560 MB, gitignored and regenerable). The raw 3.9 GB dump is
downloaded and slimmed inside the gitignored `tmp/`, and never committed.

| File | Schema |
|---|---|
| `crates.csv` | `id, name, repository, homepage` |
| `versions.csv` | `id, crate_id, license, yanked` |
| `default_versions.csv` | `crate_id, num_versions, version_id` |
| `dependencies.csv` | `version_id, crate_id, kind` |

In `data/sources/crates/`:
- `version-downloads/YYYY-MM.csv` — monthly per-version download totals

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/crates/fetch_db_dump.py` | Download the dump, write the slim extracts, delete the scratch copies. Skips when all four extracts exist |
| `src/sources/crates/fetch_version_downloads.py` | Download the monthly archives. Skips complete months |
| `src/sources/crates/process_data.py` | Build the outputs (~20 s) |
| `src/sources/crates/fetch_licenses.py` | Join the dump's default-version `license` into `results.csv` |
| `src/sources/crates/check_eol.py` | Flag crates whose default version is yanked → `eol.csv` |

```bash
uv run python -m src.sources.crates.fetch_db_dump [--chunks 8]
uv run python -m src.sources.crates.fetch_version_downloads [--years 2021 2022 ...] [--concurrency 64]
uv run python -m src.sources.crates.process_data [--min-avg N] [--alpha F]
uv run python -m src.sources.crates.fetch_licenses
uv run python -m src.sources.crates.check_eol [--limit 100]
```

`--years` defaults to the model's configured `years` in `src/settings.json`
(2021–2025). `fetch_licenses` and `check_eol` run as the `crates-lic` and
`crates-eol` steps of `src.eligibility.run_eligibility_pipeline`.

## Pipeline

`src.value.crates_pipeline` orchestrates the stage: fetch-db-dump →
fetch-downloads → process. The shared Value mechanics are in
[`value.md`](../value.md).

1. **Load mappings** from the dump (crates, versions, default_versions,
   dependencies).
2. **Aggregate downloads** — monthly version totals → per-crate annual totals.
3. **top-packages.csv** — crates covering 95% of ecosystem downloads.
4. **dependency-tree.csv** — walk transitive deps of each crate's
   **default version**, so the graph reflects current published state and no
   yanked or historical version inflates it. Every dependency *kind* rides
   along — see below.
5. **github-repos.csv** — parse the `repository` field from crates.io metadata.
6. **PageRank** — download-weighted personalized PageRank (α = 0.85) over the
   dep graph.
7. **results.csv** — sort by PageRank descending. Cumulative-share cutoffs
   assign value class A (≤75%) / B (≤95%) / C (rest).

### Dependency kinds: all three ride along

npm, PyPI, and cpp all keep runtime dependencies only. crates does not.
`src/sources/crates/process_data.py` copies the dump's `kind` field through
`KIND_MAP = {0: normal, 1: build, 2: dev}` into the `type` column of
`dependency-tree.csv` and filters nothing, so `dev` and `build` edges
propagate PageRank alongside `normal` ones. A crate pulled in only by a test
harness or a build script therefore scores as though downstream crates ship
it.

### Metric lineage

`←` = data source, `[…]` = period.

```
Rust (crates.io)
├── downloads_2021..2025   ← crates.io daily archives        [2021–2025]
├── avg_downloads          ← derived                          [2021–2025]
├── avg_downloads_share    ← derived                          [2021–2025]
├── top                    ← derived (95% cum-dl)             [2021–2025]
├── dep edges (package→dep)← DB-dump deps, all kinds          [most recent]
├── pagerank               ← derived                          [2021–2025]
├── value_class            ← derived                          [2021–2025]
└── package→repo           ← DB-dump `repository` field       [most recent]
```

## Outputs

In `data/sources/crates/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Crates covering 95% of downloads — `package, avg_downloads, avg_downloads_share, 2021`–`2025` |
| `dependency-tree.csv` | Transitive dep edges from the top crates — `package, dependency, type`; `type` is `normal` / `build` / `dev` |
| `github-repos.csv` | Package → GitHub repo mappings — `package, github_repo` |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | One row per dep-tree crate — see the schema below |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |

`results.csv` columns: `package`, `github_repo`, `git`, `eco_guess`,
`avg_downloads`, `2021`–`2025`, `top`, `pagerank`, `value_class`, `repo_id`,
`canonical_url`, `license`.

`repo_id` is host-namespaced — `gh/<numeric id>` or `gl/<host>-<numeric id>`
(`to_repo_id` in `src/common/repos.py`). `canonical_url` holds the upstream
clone URL when the hosted repo is a mirror. The value rollup's ecosyste.ms
authority pass (`src.value.apply_ecosystems_authority`) rewrites the git URL
and slug; `fetch_licenses.py` fills `license`.

Row counts: see the preview pipeline sheet → Value for the crates.io funnel
(top crates → dep tree → results → repo coverage) and the class distribution.

## Where it's used downstream

- **Value** — each crate's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_crates` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — class-A crates repos enter `src.risk.run_risk_pipeline` (scope set
  by `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — the same class-A repos (archived included) enter the
  automated [Eligibility stage](../eligibility.md)
  (`src.eligibility.run_eligibility_pipeline`), joined by `repo_id`.
  The per-ecosystem signals feed it: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/crates/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Repo coverage quirk

The crates.io `repository` field is a free-form URL, so it resolves non-GitHub
hosts too. Git coverage therefore runs ahead of GitHub coverage: crates hosted
on GitLab, Codeberg, or sourcehut still get a `git` URL.
