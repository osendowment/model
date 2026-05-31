# Python (PyPI)

The PyPI slice of the [Value pipeline](../value.md): how PyPI download and
dependency data becomes a download-weighted PageRank and an A/B/C/D value class for
every Python package. This page covers the **pipeline assembly**; for raw-fetch
mechanics (the BigQuery export, the JSON API, fetch scripts) see the source
reference [`sources/pypi.md`](../sources/pypi.md).

## Sources & data collected

| Source | Data collected | Raw file (`data/sources/pypi/`) |
|---|---|---|
| [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) | per-package annual downloads 2021–2025 — manual export (~47 TB / ~$235; mirror installers excluded) | `bigquery/bq-package-downloads.csv` |
| [PyPI JSON API](https://pypi.org/pypi/{package}/json) | runtime deps from `info.requires_dist` (PEP 508 specifiers; only runtime kept) | `raw/package-dependencies.csv` |
| External dataset (manually sourced) | `package → GitHub URL` mapping | `raw/package-github-mapping.csv` |

No authentication required except BigQuery for the download export.

## Value pipeline

PyPI data flows through the shared Value mechanics (full description in
[`value.md`](../value.md)):

1. **Load downloads** from the BigQuery export (~849K packages).
2. **Top packages** — keep packages covering 95% of the ecosystem-wide download
   total.
3. **Dependency tree** — follow transitive runtime deps from the top set.
4. **package → repo** — parse GitHub URLs from the mapping file.
5. **PageRank** — download-weighted personalized PageRank (α = 0.85) over the dep
   graph.
6. **Value class** — sort by PageRank desc; cumulative-share cutoffs assign
   A (≤50%) / B (≤75%) / C (≤90%) / D (rest).

Orchestrated by `src.value.pypi_pipeline` (fetch-data → fetch-urls → process).
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
```

## Where it's used downstream

- **Value** — each package's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_pypi` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — A/B-class PyPI repos enter `src.risk.run_risk_pipeline` (scope set by
  `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — A/B repos that also pass the OSI-license and non-EOL gates
  reach `data/eligibility/eligibility.csv`.

## Outputs

`results.csv` (`data/sources/pypi/`) — one row per dep-tree package, with
`package`, `github_repo`, `avg_downloads`, the `2021`–`2025` columns, `top`,
`pagerank`, and `value_class`.

### PyPI funnel & classes

Carried from the cross-ecosystem tables in [`value.md`](../value.md):

| Stage | Count |
|---|---:|
| Top packages (95% downloads) | 2,460 |
| After dep tree | 3,139 |
| Results | 3,139 |
| With GitHub repo | 1,728 (55%) |

| Class | A | B | C | D | Total |
|---|--:|--:|--:|--:|--:|
| Packages | 54 | 157 | 414 | 2,514 | 3,139 |
| Repos (`value.csv`) | 53 | 151 | 389 | 2,347 | — |

A+B repos with a GitHub repo: **76%**.

## Limitations

- **55% GitHub coverage — the lowest of the four ecosystems.** The BigQuery extract
  carried only GitHub URLs at fetch time, so non-GitHub upstreams (GitLab,
  self-hosted) have no `package → repo` link. Because Risk and Eligibility key off
  `github_repo`, this caps how many PyPI repos those stages can score, even for
  A/B-class packages (A+B GitHub coverage is 76%, not ~100% like npm/crates).
