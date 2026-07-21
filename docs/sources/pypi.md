# PyPI (Python)

The Python source. This page covers the full path for the PyPI ecosystem:
fetch → process → score. It describes how package downloads and the
dependency tree become a download-weighted PageRank and an A/B/C value class.
The shared mechanics live in the [Value pipeline](../value.md).

## Data sources

| Signal | Source | File (`data/sources/pypi/`) | Notes |
|---|---|---|---|
| Downloads | [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) (`bigquery-public-data.pypi.file_downloads`) | `bigquery/bq-package-downloads.csv` | Manual export of per-package annual downloads 2021–2025. The query processes ~47 TB (~$235 at $5/TB). Mirror installers excluded: bandersnatch, z3c.pypimirror, warehouse |
| Dependencies | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `raw/package-dependencies.csv` | `info.requires_dist` (PEP 508). **Runtime only** — specifiers marked `; extra ==` are skipped. Fetched at 50 req/s, 20-way concurrency |
| package → repo | BigQuery extract (one-shot, manual) | `raw/package-github-mapping.csv` | Pre-filtered to `github.com` URLs at SQL time |
| Project URLs | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `raw/package-urls.csv` | `info.project_urls` + `info.home_page`, for every host; cached per package in `raw/api-cache/` |
| Licenses | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `raw/licenses.csv` | First hit wins: `info.license_expression` (PEP 639) → OSI `classifiers` → free-form `info.license` |
| EOL | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `eol.csv` | Classifier `Development Status :: 7 - Inactive` |
| Package funding | [PyPI JSON API](https://pypi.org/pypi/{package}/json) | `funding.csv` | Funding URLs inside `info.project_urls` |

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

**Repo mappings use two layers.** `github-repos.csv` comes from the one-shot
BigQuery extract already filtered to `github.com` URLs
(`raw/package-github-mapping.csv`, manual). Separately,
`fetch_pypi_urls.py` writes the full URL set to `raw/package-urls.csv`, and
the value-stage git-URL builder classifies those by host.

## Raw data

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

## Value pipeline

`src.value.pypi_pipeline` orchestrates the value path
(fetch-data → fetch-urls → process):

1. **Load downloads** from the BigQuery export.
2. **Load the dependency graph** from the raw deps.
3. **top-packages.csv** — packages covering 95% of the ecosystem-wide download
   total.
4. **dependency-tree.csv** — follow transitive runtime deps from the top set.
5. **github-repos.csv** — parse GitHub URLs from the mapping file. The value
   stage adds non-GitHub hosts from `raw/package-urls.csv`.
6. **results.csv** — download-weighted personalized PageRank (α = 0.85) over
   the dep graph. Sort by PageRank desc; cumulative-share cutoffs assign
   value class A (≤75%) / B (≤95%) / C (rest).

Metric lineage (`←` = data source, `[…]` = period):

```
Python (PyPI)
├── downloads_2021..2025   ← BigQuery PyPI dataset    [2021–2025]
├── avg_downloads          ← derived                  [2021–2025]
├── avg_downloads_share    ← derived                  [2021–2025]
├── top                    ← derived (95% cum-dl)     [2021–2025]
├── dep edges (package→dep)← pypi.org/pypi/{p}/json   [most recent]
├── pagerank               ← derived                  [2021–2025]
├── value_class            ← derived                  [2021–2025]
└── package→repo           ← BigQuery github mapping  [most recent]
                             + pypi.org project_urls  [most recent]
```

## Outputs

In `data/sources/pypi/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive runtime dep edges from the top packages |
| `github-repos.csv` | Package → GitHub repo mappings |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | One row per dep-tree package — see below |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |
| `funding.csv` | `repo, repo_id, package, has_pypi_funding, pypi_funding_platforms, pypi_funding_url, fetched_at, status` |

### results.csv

Columns: `package`, `github_repo`, `git`, `eco_guess`, `avg_downloads`,
`2021`–`2025`, `top`, `pagerank`, `value_class`, `repo_id`, `canonical_url`,
`license`.

`repo_id` is host-namespaced — `gh/<numeric id>` or `gl/<host>-<numeric id>`
(`to_repo_id` in `src/common/repos.py`). `canonical_url` holds the upstream
clone URL when the hosted repo is a mirror. The value rollup's ecosyste.ms
authority pass (`src.value.apply_ecosystems_authority`) rewrites the git URL
and slug; `fetch_licenses.py` fills `license`.

Row counts: see the per-ecosystem value funnel in the preview pipeline sheet.

## Where it is used downstream

- **Value** — each package's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_pypi` column. The strongest class across
  ecosystems becomes `class`.
- **Risk** — class-A PyPI repos enter the [Risk stage](../risk.md)
  (`src.risk.run_risk_pipeline`; scope set by `risk_input.value_classes` in
  `src/settings.json`).
- **Eligibility** — the same class-A repos (archived included) enter the
  automated [Eligibility stage](../eligibility.md)
  (`src.eligibility.run_eligibility_pipeline`), joined by `repo_id`.
  The per-ecosystem signals feed it: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/pypi/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Limitations

- **The BigQuery mapping is GitHub-only.** `raw/package-github-mapping.csv`
  was pre-filtered to `github.com` URLs at SQL time, so a package whose
  upstream lives on GitLab, Codeberg, or a self-hosted server gets no
  `package → repo` link from it. `fetch_pypi_urls.py` closes part of that gap:
  it re-queries `info.project_urls` per package into `raw/package-urls.csv`,
  which the value-stage git-URL builder classifies by host. Risk and
  Eligibility score `platform in {github, gitlab}`, so a GitLab upstream
  recovered this way is in scope; a self-hosted one still needs a verified
  mirror in `data/value/overrides.csv`.
