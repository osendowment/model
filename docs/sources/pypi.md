# PyPI

Package downloads, dependencies, and repository mappings for the Python
ecosystem.

## Data Sources

| Signal | Source | Notes |
|---|---|---|
| Downloads | [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) (`bigquery-public-data.pypi.file_downloads`) | Manual export. The 2021–2025 query processes ~47 TB (~$235 at $5/TB). Mirror installers excluded: bandersnatch, z3c.pypimirror, warehouse |
| Dependencies | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `info.requires_dist` (PEP 508). **Runtime only** — specifiers marked `; extra ==` are skipped. Fetched at 50 req/s, 20-way concurrency |
| Project URLs | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `info.project_urls` + `info.home_page`, cached per package in `raw/api-cache/` |
| Licenses | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | First hit wins: `info.license_expression` (PEP 639) → OSI `classifiers` → free-form `info.license` |
| EOL | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | Classifier `Development Status :: 7 - Inactive` |
| Package funding | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | Funding URLs inside `info.project_urls` |

No authentication required, except BigQuery for the download export.

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

**Repo mappings use two layers.** `github-repos.csv` comes from a one-shot
BigQuery extract already filtered to `github.com` URLs
(`raw/package-github-mapping.csv`, manual). Separately,
`fetch_pypi_urls.py` writes the full URL set to `raw/package-urls.csv`, and
the value-stage git-URL builder classifies those by host.

## Raw Data

| File | Schema | Notes |
|---|---|---|
| `bigquery/bq-package-downloads.csv` | `package, avg_downloads, 2021…2025` | Manual BigQuery export |
| `raw/package-dependencies.csv` | `package, dependency, type, fetched_at` | |
| `raw/package-github-mapping.csv` | `package, github_url` | Manual, static |
| `raw/package-urls.csv` | `package, url` | All project URLs; responses cached in `raw/api-cache/` |
| `raw/licenses.csv` | `package, license, fetched_at` | Lowercase SPDX cache, 90-day TTL |
| `raw/package-dependencies-full-manual.csv` | — | **Stale artefact.** No reader or writer anywhere in the repo |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/pypi/fetch_pypi_data.py` | Iterative dep crawler (~45 pkg/s) |
| `src/sources/pypi/fetch_pypi_urls.py` | Fetch the full project-URL set per package (cached) |
| `src/sources/pypi/process_data.py` | Build the outputs from raw data |
| `src/sources/pypi/fetch_licenses.py` | Fetch SPDX licenses → `raw/licenses.csv`, then join into `results.csv` |
| `src/sources/pypi/check_eol.py` | Flag packages classified `7 - Inactive` → `eol.csv` |
| `src/sources/pypi/fetch_funding.py` | Extract funding URLs from `project_urls` → `funding.csv` |

```bash
uv run src/sources/pypi/fetch_pypi_data.py [--max-rounds 20] [--concurrency 20] [--limit 50]
uv run python -m src.sources.pypi.fetch_pypi_urls [--refresh] [--limit 50]
uv run python -m src.sources.pypi.process_data [--min-avg N] [--alpha F]
uv run python -m src.sources.pypi.fetch_licenses [--force] [--apply-only] [--limit 100]
uv run python -m src.sources.pypi.check_eol [--refresh] [--limit 100]
uv run python -m src.sources.pypi.fetch_funding [--force] [--limit 20]
```

`fetch_licenses`, `check_eol`, and `fetch_funding` run as the `pypi-lic`,
`pypi-eol`, and `pypi-funding` steps of
`src.eligibility.run_eligibility_pipeline`.

## Pipeline

1. **Load downloads** from the BigQuery export.
2. **Load the dependency graph** from the raw deps.
3. **top-packages.csv** — packages covering 95% of ecosystem downloads.
4. **dependency-tree.csv** — follow transitive runtime deps from the top set.
5. **github-repos.csv** — parse GitHub URLs from the mapping file.
6. **results.csv** — download-weighted PageRank, value classes A/B/C.

## Outputs

In `data/sources/pypi/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive runtime dep edges from the top packages |
| `github-repos.csv` | Package → GitHub repo mappings |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | Every dep-tree package with `pagerank`, `value_class`, `repo_id`, `canonical_url`, `license` |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |
| `funding.csv` | `repo, repo_id, package, has_pypi_funding, pypi_funding_platforms, pypi_funding_url, fetched_at, status` |

Row counts: see the per-ecosystem value funnel in the preview pipeline sheet.
