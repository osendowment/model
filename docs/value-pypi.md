# Value Pipeline: PyPI

## Data Sources

**Downloads**: [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi)
(`bigquery-public-data.pypi.file_downloads`). The dataset is free to query but the table is large —
extracting 2021–2025 requires processing ~47 TB of data (~$235 at on-demand BigQuery pricing of $5/TB).

Downloads from known mirror tools (`bandersnatch`, `z3c.pypimirror`, `warehouse`) are excluded.

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

**Dependencies**: [PyPI JSON API](https://pypi.org/pypi/{package}/json) — `info.requires_dist` returns PEP 508 dependency specifiers. Only runtime deps are kept (extras skipped). Fetched iteratively by `fetch_pypi_data.py`.

**Repo mappings**: External dataset (manually sourced).

### Raw Data Files

- `data/pypi/bigquery/bq-package-downloads.csv` — full BigQuery export (~849K packages, manual)
- `data/pypi/raw/package-dependencies.csv` — `package, dependency, type, fetched_at` (from PyPI API)
- `data/pypi/raw/package-github-mapping.csv` — package → GitHub URL mapping (manual)

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/pypi/fetch_pypi_data.py` | Iterative dep crawler — fetches from PyPI JSON API until tree is complete | `uv run src/pypi/fetch_pypi_data.py` |
| `src/pypi/process_data.py` | Build all output CSVs from raw data (~3.5s) | `uv run src/pypi/process_data.py` |

Options:
```bash
uv run src/pypi/fetch_pypi_data.py --concurrency 30   # tune request rate
uv run src/pypi/fetch_pypi_data.py --limit 50          # test mode
uv run src/pypi/process_data.py --min-avg 500000       # lower threshold
```

### Pipeline Steps (inside process_data.py)

1. Load package downloads from BigQuery export
2. Load dependency graph from `package-dependencies.csv`
3. **top-packages.csv** — packages with avg ≥ 1M downloads
4. **dependency-tree.csv** — BFS transitive closure from top packages; skips deps not in PyPI download data
5. **github-repos.csv** — parse GitHub URLs → `owner/repo`
6. **results.csv** — download-weighted PageRank

## Outputs

### top-packages.csv

~7.5K packages with avg ≥ 1M downloads.

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `avg_downloads` | Average annual downloads across 2021–2025 (mirrors excluded) |
| `2021`–`2025` | Total downloads for that calendar year (mirrors excluded) |

### dependency-tree.csv

~25K edges in the transitive dependency graph.

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | Dependency kind: `declared` |

### github-repos.csv

~3.7K package→repo mappings.

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `github_repo` | GitHub repository (`owner/repo`) |

### results.csv

~8.2K packages in the dep tree with downloads + PageRank.

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
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
