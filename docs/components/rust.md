# Rust (crates.io)

The crates.io slice of the [Value pipeline](../value.md): how crate download and
dependency data becomes a download-weighted PageRank and an A/B/C/D value class for
every Rust crate. This page covers the **pipeline assembly**; for raw-fetch
mechanics (the DB dump, download archives, fetch scripts) see the source reference
[`sources/crates.md`](../sources/crates.md).

## Sources & data collected

| Source | Data collected | Raw file (`data/sources/crates/`) |
|---|---|---|
| [crates.io DB dump](https://static.crates.io/db-dump.tar.gz) | crate/version names, dependency edges, and each crate's `repository` URL | `db-dump/{crates,versions,default_versions,dependencies}.csv` |
| [crates.io download archives](https://static.crates.io/archive/version-downloads/) | daily per-version download counts (aggregated into per-crate annual totals) | `version-downloads/YYYY-MM.csv` |

No authentication required; the archive endpoint supports parallel byte-range
requests.

## Value pipeline

crates data flows through the shared Value mechanics (full description in
[`value.md`](../value.md)):

1. **Load mappings** from the DB dump (crates, versions, default_versions,
   dependencies).
2. **Aggregate downloads** — monthly per-version totals → per-crate annual totals.
3. **Top packages** — keep crates covering 95% of the ecosystem-wide download
   total.
4. **Dependency tree** — follow transitive deps through **default-version** deps
   only (yanked versions excluded).
5. **crate → repo** — parse the `repository` field from crates.io metadata.
6. **PageRank** — download-weighted personalized PageRank (α = 0.85) over the dep
   graph.
7. **Value class** — sort by PageRank desc; cumulative-share cutoffs assign
   A (≤50%) / B (≤75%) / C (≤90%) / D (rest).

Orchestrated by `src.value.crates_pipeline` (fetch-db-dump → fetch-downloads →
process). Metric lineage (`←` = data source, `[…]` = period):

```
Rust (crates.io)
├── downloads_2021..2025   ← crates.io daily archives        [2021–2025]
├── avg_downloads          ← derived                          [2021–2025]
├── avg_downloads_share    ← derived                          [2021–2025]
├── top                    ← derived (95% cum-dl)             [2021–2025]
├── dep edges (package→dep)← crates.io DB-dump dependencies   [most recent]
├── pagerank               ← derived                          [2021–2025]
├── value_class            ← derived                          [2021–2025]
└── package→repo           ← DB-dump `repository` field       [most recent]
```

## Where it's used downstream

- **Value** — each crate's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_crates` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — A/B-class crates repos enter `src.risk.run_risk_pipeline` (scope set
  by `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — A/B repos that also pass the OSI-license and non-EOL gates
  reach `data/eligibility/eligibility.csv`.

## Outputs

`results.csv` (`data/sources/crates/`) — one row per dep-tree crate, with
`package`, `github_repo`, `avg_downloads`, the `2021`–`2025` columns, `top`,
`pagerank`, and `value_class`.

### crates.io funnel & classes

Carried from the cross-ecosystem tables in [`value.md`](../value.md):

| Stage | Count |
|---|---:|
| Top crates (95% downloads) | 3,719 |
| After dep tree | 6,218 |
| Results | 6,218 |
| With GitHub repo | 5,967 (96%) |
| With any Git URL | 6,130 (99%) |

| Class | A | B | C | D | Total |
|---|--:|--:|--:|--:|--:|
| Packages | 49 | 197 | 449 | 5,523 | 6,218 |
| Repos (`value.csv`) | 31 | 102 | 256 | 3,231 | — |

A+B repos: 99% have a GitHub repo, 100% have some Git URL. The crates.io
`repository` field also resolves non-GitHub Git hosts, so Git coverage (99%)
slightly exceeds GitHub (96%).
