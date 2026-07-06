# crates.io

Package downloads, dependencies, and repository mappings for the Rust ecosystem.

## Data Sources

**DB dump**: [static.crates.io/db-dump.tar.gz](https://static.crates.io/db-dump.tar.gz) -- crate/version name mappings and dependency graph. No authentication required. The server supports `Accept-Ranges`, so the fetcher downloads the dump as parallel byte-range chunks.

**Download archives**: [static.crates.io/archive/version-downloads/](https://static.crates.io/archive/version-downloads/) -- one CSV per day of per-version download counts. No authentication required. The fetcher downloads a month's daily files concurrently and aggregates them locally into monthly files (a month is only written once every day fetched successfully).

## Raw Data

In `data/sources/crates/db-dump/` -- slim extracts holding only the columns the pipeline reads (~560 MB, gitignored + regenerable: the raw 3.9 GB dump is downloaded and slimmed inside the gitignored `tmp/`, never committed):
- `crates.csv` -- id, name, repository, homepage
- `versions.csv` -- id, crate_id, license, yanked
- `default_versions.csv` -- current (non-yanked) version per crate
- `dependencies.csv` -- version_id, crate_id, kind

In `data/sources/crates/`:
- `version-downloads/YYYY-MM.csv` -- monthly per-version download totals

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/crates/fetch_db_dump.py` | Download + extract DB dump (skips if done) |
| `src/sources/crates/fetch_version_downloads.py` | Download monthly archives (skips complete months) |
| `src/sources/crates/process_data.py` | Build outputs (~20s) |

```bash
uv run src/sources/crates/fetch_db_dump.py [--chunks 8]
uv run src/sources/crates/fetch_version_downloads.py [--years 2021 2022 ...] [--concurrency 64]
uv run python -m src.sources.crates.process_data [--min-avg N] [--alpha F]
```

`--years` defaults to the model's configured `years` in `src/settings.json` (2021-2025).

## Pipeline

1. **Load mappings** from db-dump (crates, versions, default_versions, dependencies)
2. **Aggregate downloads** -- monthly version-downloads into per-crate annual totals
3. **top-packages.csv** -- crates covering 95% of ecosystem downloads
4. **dependency-tree.csv** -- follow transitive deps through default-version deps only (not yanked)
5. **github-repos.csv** -- parse repo URLs from crates.io metadata
6. **results.csv** -- download-weighted PageRank, value classes A/B/C

## Outputs

In `data/sources/crates/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Crates covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive dep edges from top crates |
| `github-repos.csv` | Package-to-GitHub-repo mappings |
| `results.csv` | All dep-tree crates with pagerank + value_class |

Row counts: see the per-ecosystem value funnel in [stats.md](../stats.md#value).
