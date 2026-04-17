# Value Pipeline: crates.io

## Data Sources

**DB dump** (`https://static.crates.io/db-dump.tar.gz`) — crate/version name mappings and
dependency graph. Extracted to `data/crates/db-dump/`.

**Daily archive** (`https://static.crates.io/archive/version-downloads/YYYY-MM-DD.csv`) —
per-version download counts. Aggregated into monthly files under
`data/crates/version-downloads/YYYY-MM.csv`.

### Raw Data Files

In `data/crates/`:
- `db-dump/crates.csv` — crate ID → name + repository URL
- `db-dump/versions.csv` — version ID → crate ID
- `db-dump/default_versions.csv` — current (non-yanked) version per crate
- `db-dump/dependencies.csv` — version-level dependency edges
- `version-downloads/YYYY-MM.csv` — monthly per-version download totals

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/crates/fetch_db_dump.py` | Download + extract DB dump (skips if already done) | `uv run src/crates/fetch_db_dump.py` |
| `src/crates/fetch_version_downloads.py` | Download monthly download archives (skips complete months) | `uv run src/crates/fetch_version_downloads.py --years 2021 2022 2023 2024 2025` |
| `src/crates/process_data.py` | Build all output CSVs (~15s on re-run) | `uv run src/crates/process_data.py` |

### Pipeline Steps (inside process_data.py)

1. Load mappings from db-dump (crates, versions, default_versions, dependencies)
2. Aggregate monthly version-downloads into per-crate annual totals
3. **top-packages.csv** — crates with avg ≥ 1M downloads
4. **dependency-tree.csv** — BFS from top crates through default-version deps only (not yanked/historical)
5. **github-repos.csv** — parse repo URLs from crates.io metadata
6. **results.csv** — download-weighted PageRank

## Outputs

### top-packages.csv

~3K crates with avg ≥ 1M downloads.

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `avg_downloads` | Average annual downloads across 2021–2025 |
| `2021`–`2025` | Total downloads for that calendar year |

### dependency-tree.csv

~39K edges in the transitive dependency graph.

| Column | Description |
|--------|-------------|
| `package` | Dependent crate |
| `dependency` | Dependency crate |
| `type` | Dependency kind: `normal`, `build`, or `dev` |

### github-repos.csv

~5K package→repo mappings.

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `github_repo` | GitHub repository (`owner/repo`) |

### results.csv

~5.2K packages in the dep tree with downloads + PageRank.

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `github_repo` | GitHub repository (`owner/repo`) |
| `avg_downloads` | Average annual downloads across 2021–2025 |
| `2021`–`2025` | Total downloads for that calendar year |
| `top` | `True` if the package meets the ≥ 1M download threshold |
| `pagerank` | Download-weighted PageRank in the dependency graph |
| `value_class` | Value class (A/B/C/D) based on cumulative pagerank share — see [value classes](#value-classes) |

### Value Classes

Packages are classified into four tiers based on how much ecosystem value they
account for, measured by cumulative share of total pagerank (sorted descending):

| Class | Cumulative Share | Meaning |
|-------|-----------------|---------|
| **A** | 0–50% | Critical infrastructure — ecosystem breaks without these |
| **B** | 50–75% | Important — widely used, significant dependency chains |
| **C** | 75–90% | Useful — meaningful but not load-bearing |
| **D** | 90–100% | Long tail — niche, leaf nodes, minimal ecosystem impact |

Current distribution (~5.2K packages):

| Class | Packages | % of total |
|-------|----------|-----------|
| A | ~49 | 0.9% |
| B | ~195 | 3.7% |
| C | ~433 | 8.3% |
| D | ~4,557 | 87.1% |
