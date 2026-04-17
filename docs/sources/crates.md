# crates.io

Package downloads, dependencies, and repository mappings for the Rust ecosystem.

## Data Sources

**DB dump**: [static.crates.io/db-dump.tar.gz](https://static.crates.io/db-dump.tar.gz) -- crate/version name mappings and dependency graph. No authentication required.

**Download archives**: [static.crates.io/archive/version-downloads/](https://static.crates.io/archive/version-downloads/) -- daily per-version download counts, aggregated into monthly files. No authentication required. Supports parallel byte-range requests.

## Raw Data

In `data/crates/`:
- `db-dump/crates.csv` -- crate ID, name, repository URL
- `db-dump/versions.csv` -- version ID, crate ID
- `db-dump/default_versions.csv` -- current (non-yanked) version per crate
- `db-dump/dependencies.csv` -- version-level dependency edges
- `version-downloads/YYYY-MM.csv` -- monthly per-version download totals

## Scripts

| Script | Purpose |
|--------|---------|
| `src/crates/fetch_db_dump.py` | Download + extract DB dump (skips if done) |
| `src/crates/fetch_version_downloads.py` | Download monthly archives (skips complete months) |
| `src/crates/process_data.py` | Build outputs (~20s) |

```bash
uv run src/crates/fetch_db_dump.py [--chunks 16]
uv run src/crates/fetch_version_downloads.py --years 2021 2022 2023 2024 2025 [--concurrency 128]
uv run python -m src.crates.process_data [--min-avg N] [--alpha F]
```

## Pipeline

1. **Load mappings** from db-dump (crates, versions, default_versions, dependencies)
2. **Aggregate downloads** -- monthly version-downloads into per-crate annual totals
3. **top-packages.csv** -- crates covering 95% of ecosystem downloads
4. **dependency-tree.csv** -- follow transitive deps through default-version deps only (not yanked)
5. **github-repos.csv** -- parse repo URLs from crates.io metadata
6. **results.csv** -- download-weighted PageRank, value classes A/B/C/D

## Outputs

In `data/crates/`:

| File | Rows | Description |
|------|------|-------------|
| `top-packages.csv` | ~3.7K | Crates covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | ~48K edges | Transitive deps from top crates |
| `github-repos.csv` | ~6.0K | Package-to-GitHub-repo mappings |
| `results.csv` | ~6.2K | All dep-tree crates with pagerank + value_class |
