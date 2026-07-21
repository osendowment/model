# crates.io

Package downloads, dependencies, and repository mappings for the Rust
ecosystem.

## Data Sources

| Signal | Source | Notes |
|---|---|---|
| Crate metadata + dep graph | [static.crates.io/db-dump.tar.gz](https://static.crates.io/db-dump.tar.gz) | The server supports `Accept-Ranges`, so the fetcher downloads the dump as parallel byte-range chunks |
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

1. **Load mappings** from the dump (crates, versions, default_versions,
   dependencies).
2. **Aggregate downloads** — monthly version totals → per-crate annual totals.
3. **top-packages.csv** — crates covering 95% of ecosystem downloads.
4. **dependency-tree.csv** — walk transitive deps of each crate's
   **default version**, so the graph reflects current published state and no
   yanked or historical version inflates it.
5. **github-repos.csv** — parse repo URLs from crates.io metadata.
6. **results.csv** — download-weighted PageRank, value classes A/B/C.

### Dependency kinds: all three ride along

Unlike npm, PyPI, and cpp — which all keep runtime deps only — the crates dep
tree keeps **every** dependency kind the dump declares. The `type` column
carries the dump's `kind` mapped through `{0: normal, 1: build, 2: dev}`, and
`process_data.py` applies no filter, so `dev` and `build` edges propagate
PageRank alongside `normal` ones. A crate used only as a test or build-script
dependency therefore scores as though downstream crates ship it.

## Outputs

In `data/sources/crates/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Crates covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive dep edges from the top crates (`package, dependency, type`) |
| `github-repos.csv` | Package → GitHub repo mappings |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | Every dep-tree crate with `pagerank`, `value_class`, `repo_id`, `canonical_url`, `license` |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |

Row counts: see the per-ecosystem value funnel in the preview pipeline sheet.
