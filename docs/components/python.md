# Python (PyPI)

The PyPI slice of the [Value pipeline](../value.md): how PyPI download and
dependency data becomes a download-weighted PageRank and an A/B/C value class for
every Python package. This page covers the **pipeline assembly**; for raw-fetch
mechanics (the BigQuery export, the JSON API, fetch scripts) see the source
reference [`sources/pypi.md`](../sources/pypi.md).

## Sources & data collected

| Source | Data collected | Raw file (`data/sources/pypi/`) |
|---|---|---|
| [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) | per-package annual downloads 2021–2025 — manual export (~47 TB / ~$235; mirror installers excluded) | `bigquery/bq-package-downloads.csv` |
| [PyPI JSON API](https://pypi.org/pypi/{package}/json) | runtime deps from `info.requires_dist` (PEP 508 specifiers; only runtime kept) | `raw/package-dependencies.csv` |
| BigQuery extract (one-shot, manual) | `package → GitHub URL` mapping, pre-filtered to `github.com` | `raw/package-github-mapping.csv` |
| [PyPI JSON API](https://pypi.org/pypi/{package}/json) | full URL set — `info.project_urls` + `info.home_page` — for every host | `raw/package-urls.csv` |

No authentication required except BigQuery for the download export.

## Value pipeline

PyPI data flows through the shared Value mechanics (full description in
[`value.md`](../value.md)):

1. **Load downloads** from the BigQuery export.
2. **Top packages** — keep packages covering 95% of the ecosystem-wide download
   total.
3. **Dependency tree** — follow transitive runtime deps from the top set.
4. **package → repo** — parse GitHub URLs from the mapping file; the value
   stage adds non-GitHub hosts from `raw/package-urls.csv`.
5. **PageRank** — download-weighted personalized PageRank (α = 0.85) over the dep
   graph.
6. **Value class** — sort by PageRank desc; cumulative-share cutoffs assign
   A (≤75%) / B (≤95%) / C (rest).

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
                             + pypi.org project_urls  [most recent]
```

## Where it's used downstream

- **Value** — each package's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_pypi` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — class-A PyPI repos enter `src.risk.run_risk_pipeline` (scope set by
  `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — the same class-A repos (archived included) enter the
  automated [Eligibility stage](../eligibility.md)
  (`src.eligibility.run_eligibility_pipeline`), joined by `repo_id`.
  The per-ecosystem signals feed it: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/pypi/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Outputs

`results.csv` (`data/sources/pypi/`) — one row per dep-tree package:
`package`, `github_repo`, `git`, `eco_guess`, `avg_downloads`, `2021`–`2025`,
`top`, `pagerank`, `value_class`, `repo_id`, `canonical_url`, `license`.

`repo_id` is host-namespaced — `gh/<numeric id>` or `gl/<host>-<numeric id>`
(`to_repo_id` in `src/common/repos.py`). `canonical_url` holds the upstream
clone URL when the hosted repo is a mirror. The value rollup's ecosyste.ms
authority pass (`src.value.apply_ecosystems_authority`) rewrites the git URL
and slug; `fetch_licenses.py` fills `license`.

### PyPI funnel & classes

See the preview pipeline sheet → Value for the PyPI funnel counts (top packages → dep tree → results → repo coverage) and class distribution.

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
