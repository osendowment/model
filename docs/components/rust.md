# Rust (crates.io)

The crates.io slice of the [Value pipeline](../value.md): how crate download and
dependency data becomes a download-weighted PageRank and an A/B/C value class for
every Rust crate. This page covers the **pipeline assembly**; for raw-fetch
mechanics (the DB dump, download archives, fetch scripts) see the source reference
[`sources/crates.md`](../sources/crates.md).

## Sources & data collected

| Source | Data collected | Raw file (`data/sources/crates/`) |
|---|---|---|
| [crates.io DB dump](https://static.crates.io/db-dump.tar.gz) | crate/version names, dependency edges, and each crate's `repository` URL | `db-dump/{crates,versions,default_versions,dependencies}.csv` (slim extracts; gitignored, regenerable) |
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
   A (≤75%) / B (≤95%) / C (rest).

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
- **Risk** — class-A crates repos enter `src.risk.run_risk_pipeline` (scope set
  by `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — the same class-A repos (archived included) enter the
  automated [Eligibility stage](../eligibility.md)
  (`src.eligibility.run_eligibility_pipeline`), also keyed off `github_repo`.
  The per-ecosystem signals feed it: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/crates/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Outputs

`results.csv` (`data/sources/crates/`) — one row per dep-tree crate, with
`package`, `github_repo`, `avg_downloads`, the `2021`–`2025` columns, `top`,
`pagerank`, and `value_class`, plus the repo-identity columns (`git`,
`eco_guess`, `repo_id`, `mirror_url` — the git URL/slug is rewritten by the
value rollup's ecosyste.ms authority pass,
`src.value.apply_ecosystems_authority`) and `license` (filled by
`fetch_licenses.py`).

### crates.io funnel & classes

See [docs/stats.md → Value](../stats.md#per-ecosystem-value-funnel) for the crates.io funnel counts (top crates → dep tree → results → repo coverage) and class distribution.

The crates.io `repository` field resolves non-GitHub Git hosts, so Git coverage
slightly exceeds GitHub coverage.
