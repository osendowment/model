# Datasets

All data is derived from public sources (GitHub API, PyPI, npm).

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
| [top-package-downloads.csv](pypi/top-package-downloads.csv) | PyPI packages with 1M+ avg annual downloads (2021–2025), ~7.5K packages |
| [package-dependencies.csv](pypi/package-dependencies.csv) | Package dependency graph |
| [package-github-mapping.csv](pypi/package-github-mapping.csv) | PyPI package → GitHub repo mapping |

### top-packages.csv columns

| Column | Description |
|--------|-------------|
| `package` | PyPI package name |
| `avg_downloads` | Average annual downloads across all tracked years (mirrors excluded) |
| `2021`–`2025` | Total downloads for that calendar year (mirrors excluded) |

### Data source

Downloads come from the public [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi)
(`bigquery-public-data.pypi.file_downloads`). The dataset is free to query but the table is large —
extracting 2021–2025 requires processing ~47 TB of data (~$235 at on-demand BigQuery pricing of $5/TB).

Downloads from known mirror tools (`bandersnatch`, `z3c.pypimirror`, `warehouse`) are excluded.
Only packages with `avg_downloads >= 1,000,000` are committed to this repo.

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

After exporting the query results, filter to `avg_downloads >= 1000000` before committing.

## npm

| File | Description |
|------|-------------|
| [top-package-downloads.csv](npm/top-package-downloads.csv) | npm packages with annual download counts (2021–2025), ~17K packages |

### top-packages.csv columns

| Column | Description |
|--------|-------------|
| `package` | npm package name |
| `avg_downloads` | Average annual downloads across all tracked years |
| `2021`–`2025` | Total downloads for that calendar year |

### Data source

Downloads are fetched from the [npm downloads API](https://api.npmjs.org/downloads/point/{start}:{end}/{package}).
Each year column corresponds to a `point` range query from `{year}-01-01` to `{year}-12-31`.

**Example:**
```
GET https://api.npmjs.org/downloads/point/2024-01-01:2024-12-31/semver
→ {"downloads": 16558352576, "start": "2024-01-01", "end": "2024-12-31", "package": "semver"}
```

### Verification

The integration test `tests/test_npm_downloads.py` picks a random package and year from this file,
fetches the live npm API, and asserts the download count matches exactly:

```
uv run pytest tests/test_npm_downloads.py -v
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
