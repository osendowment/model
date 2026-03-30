# Datasets

All data is derived from public sources (GitHub API, PyPI, npm, crates.io).

---

## Ecosystem Pipeline (npm · PyPI · crates.io)

npm, PyPI, and crates.io all follow the same pipeline structure and produce identical output schemas.

```
Raw data               Processing              Outputs
──────────────         ───────────             ───────────────────────
downloads data    ──►                     ──►  top-packages.csv
dep graph data    ──►  process_data.py    ──►  dependency-tree.csv
repo mappings     ──►                     ──►  github-repos.csv
                                          ──►  results.csv
```

**Output schemas** (same columns in every ecosystem):

**top-packages.csv** — packages with avg ≥ 1M annual downloads

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `avg_downloads` | Average annual downloads across populated years |
| `2021`–`2025` | Downloads for that calendar year |

**dependency-tree.csv** — full transitive dep tree rooted at top packages

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | `declared` (npm/PyPI) or `normal`/`build`/`dev` (crates) |

**github-repos.csv** — GitHub repos for packages in the dep tree

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | GitHub repository (`owner/repo`) |

**results.csv** — all dep-tree packages scored

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | GitHub repository (`owner/repo`) |
| `avg_downloads` | Average annual downloads |
| `2021`–`2025` | Downloads for that calendar year |
| `top` | `True` if the package meets the ≥ 1M threshold |
| `pagerank` | PageRank score in the dependency graph |
| `pagerank_dl` | PageRank weighted by download counts |

---

## GitHub

| File | Description |
|------|-------------|
| [top-repos.csv](github/top-repos.csv) | GitHub repos with 1K+ stars across tracked languages |
| [repo-contrib-metrics.csv](github/repo-contrib-metrics.csv) | Yearly contributor metrics (bus factor, HHI, contributor count) |
| [repo-year-sha.csv](github/repo-year-sha.csv) | Commit SHAs per repo per year (coordination file for git-metrics pipeline) |
| [repo-search-counts.csv](github/repo-search-counts.csv) | Cached search API counts for date range optimization |
| [locs.csv](github/locs.csv) | Estimated LOC per repo from GitHub /languages endpoint |
| [language-stats.csv](github/language-stats.csv) | Language metadata: scc names, file extensions, bytes-per-line, scc support flag |

## PyPI

| File | Description |
|------|-------------|
| [top-packages.csv](pypi/top-packages.csv) | PyPI packages with avg ≥ 1M downloads (2021–2025), ~7.5K packages |
| [dependency-tree.csv](pypi/dependency-tree.csv) | Full transitive dependency graph rooted at top packages, ~21K edges |
| [github-repos.csv](pypi/github-repos.csv) | GitHub owner/repo for packages in the dep tree, ~3.7K entries |
| [results.csv](pypi/results.csv) | All packages in the dep tree (~9.3K) with downloads + PageRank |

### Columns

**top-packages.csv**

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `avg_downloads` | Average annual downloads across 2021–2025 (mirrors excluded) |
| `2021`–`2025` | Total downloads for that calendar year (mirrors excluded) |

**dependency-tree.csv**

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | Dependency kind: `declared` or `discovered` |

**github-repos.csv**

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `github_repo` | GitHub repository (`owner/repo`) |

**results.csv**

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `github_repo` | GitHub repository (`owner/repo`) |
| `avg_downloads` | Average annual downloads across 2021–2025 |
| `2021`–`2025` | Total downloads for that calendar year |
| `top` | `True` if the package meets the ≥ 1M download threshold |
| `pagerank` | PageRank score in the dependency graph |
| `pagerank_dl` | PageRank weighted by download counts |

### Data source

Downloads come from the public [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi)
(`bigquery-public-data.pypi.file_downloads`). The dataset is free to query but the table is large —
extracting 2021–2025 requires processing ~47 TB of data (~$235 at on-demand BigQuery pricing of $5/TB).

Downloads from known mirror tools (`bandersnatch`, `z3c.pypimirror`, `warehouse`) are excluded.
The full BigQuery export is stored in `data/pypi/bigquery/bq-package-downloads.csv`.

**BigQuery extraction query:**

```sql
SELECT
    project as package,
    CAST(ROUND(COUNT(*) / 5) AS INT64) AS avg_downloads,
    COUNTIF(timestamp >= TIMESTAMP('2021-01-01') AND timestamp < TIMESTAMP('2022-01-01')) AS `2021`,
    COUNTIF(timestamp >= TIMESTAMP('2022-01-01') AND timestamp < TIMESTAMP('2023-01-01')) AS `2022`,
    COUNTIF(timestamp >= TIMESTAMP('2023-01-01') AND timestamp < TIMESTAMP('2024-01-01')) AS `2023`,
    COUNTIF(timestamp >= TIMESTAMP('2024-01-01') AND timestamp < TIMESTAMP('2025-01-01')) AS `2024`,
    COUNTIF(timestamp >= TIMESTAMP('2025-01-01') AND timestamp < TIMESTAMP('2026-01-01')) AS `2025`
  FROM `bigquery-public-data.pypi.file_downloads`
  WHERE timestamp >= TIMESTAMP('2021-01-01')
    AND timestamp <  TIMESTAMP('2026-01-01')
    AND details.installer.name NOT IN ('bandersnatch', 'z3c.pypimirror', 'warehouse')
  GROUP BY project
  ORDER BY avg_downloads DESC
```

### Collection pipeline

```bash
# Build all output CSVs from local raw data (~2.5s)
uv run src/pypi/process_data.py
```

Raw input files (not regenerated by the pipeline):
- `data/pypi/bigquery/bq-package-downloads.csv` — full BigQuery export (~849K packages)
- `data/pypi/raw/package-dependencies.csv` — package dependency graph
- `data/pypi/raw/package-github-mapping.csv` — package → GitHub URL mapping

## npm

| File | Description |
|------|-------------|
| [top-packages.csv](npm/top-packages.csv) | npm packages with avg ≥ 1M annual downloads (2021–2025) |
| [dependency-tree.csv](npm/dependency-tree.csv) | Full transitive dep tree rooted at top packages |
| [github-repos.csv](npm/github-repos.csv) | GitHub owner/repo for dep-tree packages |
| [results.csv](npm/results.csv) | All dep-tree packages with downloads + PageRank |

See [Ecosystem Pipeline](#ecosystem-pipeline-npm--pypi--cratesio) above for column schemas.

### Pipeline

```
fetch_nice_registry.py          fetch_npm_data.py
        │                               │
        ▼                               ▼
nice-registry/packages.csv      raw/downloads.csv
                                 raw/dependencies.csv
                                        │
                                        ▼
                                 process_data.py
                          ┌────────────┼────────────┐
                          ▼            ▼             ▼
                  top-packages  dependency-tree   results
                                  github-repos
```

**Step 1 — top-packages.csv**
Filter `raw/downloads.csv` to packages with avg ≥ 1M annual downloads.

**Step 2 — build dep tree** *(iterative)*
BFS from top packages through `raw/dependencies.csv`. For any tree node not yet in deps, fetch from the npm registry and upsert. Repeat until the full transitive dep tree is covered.

**Step 3 — fetch missing downloads**
For every dep-tree package, ensure `raw/downloads.csv` has all 5 years. Fetches newest-to-oldest; short-circuits on the first year where the API returns null (package didn't exist yet — fill zeros for all earlier years).

**Step 4 — dependency-tree.csv**
Write all transitive edges reachable from top packages.

**Step 5 — github-repos.csv**
Match dep-tree packages against `nice-registry/packages.csv`; extract `owner/repo` slugs.

**Step 6 — results.csv**
Run PageRank over the dep graph (plain + download-weighted) and join with download data.

### Data source

Downloads: [npm downloads API](https://api.npmjs.org/downloads/point) — bulk endpoint supports up to 128 packages per request.

```
GET https://api.npmjs.org/downloads/point/2024-01-01:2024-12-31/semver,lodash,react
→ {"semver": {"downloads": 16558352576, ...}, "lodash": {...}, ...}
```

Deps: [npm registry](https://registry.npmjs.org) — `/{package}/latest` returns declared runtime dependencies.

Repo mappings: [nice-registry/all-the-package-repos](https://github.com/nice-registry/all-the-package-repos) — community-maintained `package → repo_url` mapping (~2M packages).

### Collection pipeline

```bash
# 1. Fetch nice-registry repo mappings (one-time, ~212 MB)
uv run src/npm/fetch_nice_registry.py

# 2. Fetch raw downloads + deps (iterates until graph is complete)
uv run src/npm/fetch_npm_data.py
uv run src/npm/fetch_npm_data.py --concurrency 10   # tune request rate
uv run src/npm/fetch_npm_data.py --limit 50          # test mode

# 3. Build output CSVs
uv run src/npm/process_data.py
uv run src/npm/process_data.py --ignore-gaps         # skip fetching, use available data
```

Raw input files (in `data/npm/raw/`):
- `downloads.csv` — `package, year, downloads` for all fetched packages
- `dependencies.csv` — `package, dep_name, dep_version, fetched_at`

## Crates (Rust)

| File | Description |
|------|-------------|
| [top-packages.csv](crates/top-packages.csv) | Crates with avg ≥ 1M downloads (2021–2025), ~3K packages |
| [dependency-tree.csv](crates/dependency-tree.csv) | Full transitive dependency graph rooted at top packages, ~39K edges |
| [github-repos.csv](crates/github-repos.csv) | GitHub owner/repo for packages in the dep tree, ~5K entries |
| [results.csv](crates/results.csv) | All packages in the dep tree (~5.2K) with downloads + PageRank |

### Columns

**top-packages.csv**

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `avg_downloads` | Average annual downloads across 2021–2025 |
| `2021`–`2025` | Total downloads for that calendar year |

**dependency-tree.csv**

| Column | Description |
|--------|-------------|
| `package` | Dependent crate |
| `dependency` | Dependency crate |
| `type` | Dependency kind: `normal`, `build`, or `dev` |

**github-repos.csv**

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `github_repo` | GitHub repository (`owner/repo`) |

**results.csv**

| Column | Description |
|--------|-------------|
| `package` | Crate name |
| `github_repo` | GitHub repository (`owner/repo`) |
| `avg_downloads` | Average annual downloads across 2021–2025 |
| `2021`–`2025` | Total downloads for that calendar year |
| `top` | `True` if the package meets the ≥ 1M download threshold |
| `pagerank` | PageRank score in the dependency graph |
| `pagerank_dl` | PageRank weighted by download counts |

### Data source

Downloads come from two crates.io public sources:

- **DB dump** (`https://static.crates.io/db-dump.tar.gz`) — crate/version name mappings and
  dependency graph. Extracted to `data/crates/db-dump/`.
- **Daily archive** (`https://static.crates.io/archive/version-downloads/YYYY-MM-DD.csv`) —
  per-version download counts. Aggregated into monthly files under
  `data/crates/version-downloads/YYYY-MM.csv`.

### Collection pipeline

```bash
# 1. Download + extract DB dump (skips if already done)
uv run src/crates/fetch_db_dump.py

# 2. Download monthly download archives (skips already-complete months)
uv run src/crates/fetch_version_downloads.py --years 2021 2022 2023 2024 2025

# 3. Build all output CSVs (~15s on re-run)
uv run src/crates/process_data.py
```

## Derived

| File | Description |
|------|-------------|
| [eligibility.csv](eligibility.csv) | OSS license eligibility per repo |
| [risk-metrics.csv](risk-metrics.csv) | Risk classifications per repo (see columns below) |

### risk-metrics.csv columns

| Column | Description |
|--------|-------------|
| `repo` | GitHub repo slug (owner/name) |
| `repo_id` | GitHub numeric repo ID |
| `active_contributors` | Contributors with commits in 2021–2025 |
| `hhi_commits` | Herfindahl-Hirschman Index (0–10000) — higher = more concentrated |
| `bus_factor_commits` | Minimum contributors accounting for 50% of commits |
| `est_locs` | Estimated lines of code (from GitHub /languages endpoint) |
| `concentration_class` | A (critical) / B (high risk) / C (moderate) / D (healthy) — based on BF + HHI |
| `complexity_class` | A (massive, ≥1M) / B (large, 100K–1M) / C (moderate, 10K–100K) / D (small, <10K) — based on LOC |
