# PyPI

Package downloads, dependencies, and repository mappings for the Python ecosystem.

## Data Sources

**Downloads**: [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) (`bigquery-public-data.pypi.file_downloads`). Requires manual export -- querying 2021-2025 processes ~47 TB (~$235 at $5/TB). Mirror tools excluded (bandersnatch, z3c.pypimirror, warehouse).

BigQuery SQL:
```sql
SELECT project as package,
  CAST(ROUND(COUNT(*) / 5) AS INT64) AS avg_downloads,
  COUNTIF(timestamp >= '2021-01-01' AND timestamp < '2022-01-01') AS `2021`,
  -- ...same for 2022-2025
FROM `bigquery-public-data.pypi.file_downloads`
WHERE timestamp >= '2021-01-01' AND timestamp < '2026-01-01'
  AND details.installer.name NOT IN ('bandersnatch','z3c.pypimirror','warehouse')
GROUP BY project ORDER BY avg_downloads DESC
```

**Dependencies**: [PyPI JSON API](https://pypi.org/pypi/{package}/json) -- `info.requires_dist` returns PEP 508 dependency specifiers. Only runtime deps kept. Rate limit ~50 req/s.

**Repo mappings**: two layers. `github-repos.csv` is built from a legacy one-shot BigQuery extract pre-filtered to `github.com` URLs (`raw/package-github-mapping.csv`, manual). Separately, `src/sources/pypi/fetch_pypi_urls.py` queries `pypi.org/pypi/{package}/json` for every results.csv package and writes the full URL set (`info.project_urls` + `info.home_page`) to `raw/package-urls.csv`, which the value-stage git-URL builder classifies by host (GitHub / GitLab / etc.).

No authentication required (except BigQuery for download data).

## Raw Data

- `data/sources/pypi/bigquery/bq-package-downloads.csv` -- ~849K packages x 5 years (manual export)
- `data/sources/pypi/raw/package-dependencies.csv` -- package, dependency, type, fetched_at
- `data/sources/pypi/raw/package-github-mapping.csv` -- package-to-GitHub URL (manual, legacy)
- `data/sources/pypi/raw/package-urls.csv` -- package, url (all project URLs from the PyPI JSON API; per-package responses cached in `raw/api-cache/`)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/pypi/fetch_pypi_data.py` | Iterative dep crawler (~45 pkg/s) |
| `src/sources/pypi/fetch_pypi_urls.py` | Fetch full project URLs per package (cached) |
| `src/sources/pypi/process_data.py` | Build outputs from raw data |

```bash
uv run src/sources/pypi/fetch_pypi_data.py [--max-rounds 20] [--concurrency 20] [--limit 50]
uv run python -m src.sources.pypi.fetch_pypi_urls [--refresh] [--limit 50]
uv run python -m src.sources.pypi.process_data [--min-avg N] [--alpha F]
```

## Pipeline

1. **Load downloads** from BigQuery export
2. **Load dependency graph** from raw deps
3. **top-packages.csv** -- packages covering 95% of ecosystem downloads
4. **dependency-tree.csv** -- follow transitive deps from top packages
5. **github-repos.csv** -- parse GitHub URLs from mapping file
6. **results.csv** -- download-weighted PageRank, value classes A/B/C

## Outputs

In `data/sources/pypi/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive dep edges from top packages |
| `github-repos.csv` | Package-to-GitHub-repo mappings |
| `results.csv` | All dep-tree packages with pagerank + value_class |

Row counts: see the per-ecosystem value funnel in [stats.md](../stats.md#value).
