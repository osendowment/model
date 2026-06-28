# JavaScript / TypeScript (npm)

The npm slice of the [Value pipeline](../value.md): how npm download and
dependency data becomes a download-weighted PageRank and an A/B/C value class
for every JavaScript/TypeScript package. This page covers the **pipeline
assembly**; for raw-fetch mechanics (endpoints, rate limits, fetch scripts) see
the source reference [`sources/npm.md`](../sources/npm.md).

## Sources & data collected

| Source | Data collected | Raw file (`data/sources/npm/`) |
|---|---|---|
| [npm downloads API](https://api.npmjs.org/downloads/point) | per-package annual downloads (2021–2025) | `raw/downloads.csv` |
| [npm downloads API](https://api.npmjs.org/downloads/point) | ecosystem-wide annual totals (the 95% denominator) | `raw/npm-stats.csv` |
| [npm registry](https://registry.npmjs.org) | declared runtime dependencies (`package → dep`) | `raw/dependencies.csv` |
| [nice-registry](https://github.com/nice-registry/all-the-package-repos) | `package → repo_url` mapping (~2M packages) | `nice-registry/packages.csv` |

No authentication required; the downloads API is rate-limited to ~5 req/s.

## Value pipeline

npm data flows through the shared Value mechanics (full description in
[`value.md`](../value.md)):

1. **Top packages** — sort by avg annual downloads, keep packages covering 95% of
   the ecosystem-wide total (from `npm-stats.csv`).
2. **Dependency tree** — follow transitive runtime deps from the top set, fetching
   any missing deps from the registry.
3. **package → repo** — match every dep-tree package against nice-registry.
4. **PageRank** — download-weighted personalized PageRank (α = 0.85) over the
   directed dep graph (`A → B` means *A depends on B*).
5. **Value class** — sort by PageRank desc; cumulative-share cutoffs assign
   A (≤75%) / B (≤95%) / C (rest).

Orchestrated by `src.value.npm_pipeline` (fetch-data → fetch-stats → fetch-repos →
process). Metric lineage (`←` = data source, `[…]` = period):

```
JavaScript / TypeScript (npm)
├── downloads_2021..2025   ← api.npmjs.org/downloads             [2021–2025]
├── avg_downloads          ← derived (mean over populated years) [2021–2025]
├── avg_downloads_share    ← derived (pkg / ecosystem total)     [2021–2025]
├── top                    ← derived (95% cum-download cutoff)   [2021–2025]
├── dep edges (package→dep)← registry.npmjs.org                  [most recent]
├── pagerank               ← derived (DL-weighted PR, α=0.85)    [2021–2025]
├── value_class            ← derived (A/B/C, cum-PR share)       [2021–2025]
└── package→repo           ← nice-registry                       [most recent]
```

## Where it's used downstream

- **Value** — each package's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_npm` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — class-A npm repos enter `src.risk.run_risk_pipeline` (scope set by
  `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — now a **manual review** of the top candidates (OSS license,
  EOL, independence), not an automated pipeline stage. The per-ecosystem license/EOL
  signals (`fetch_licenses.py`, `check_eol.py` → `data/sources/npm/eol.csv`) are still
  produced and feed that review; there is no automated eligibility output.

## Outputs

`results.csv` (`data/sources/npm/`) — one row per dep-tree package:

| Column | Description |
|---|---|
| `package` | Package name |
| `github_repo` | `owner/repo` slug |
| `avg_downloads`, `2021`–`2025` | Downloads |
| `top` | `True` if in the 95% cumulative set |
| `pagerank` | Download-weighted PageRank score |
| `value_class` | A/B/C |

### npm funnel & classes

Carried from the cross-ecosystem tables in [`value.md`](../value.md):

| Stage | Count |
|---|---:|
| Top packages (95% downloads) | 5,765 |
| After dep tree | 6,370 |
| Results | 6,370 |
| With GitHub repo | 6,281 (99%) |

| Class (`value.csv`) | A | B | C | Total |
|---|--:|--:|--:|--:|
| Repos (`class_npm`) | 571 | 1,414 | 2,428 | 4,413 |

Per-package class counts await the next full pipeline run — the per-package
`results.csv` `value_class` is still on the legacy 4-class scheme.

class-A repos with a GitHub repo: **100%** — npm has the cleanest upstream identity of
the four ecosystems, so essentially all load-bearing npm packages reach Risk (and
the manual eligibility review).
