# npm (JavaScript / TypeScript)

Package downloads, dependencies, licenses, and repository mappings for the
JavaScript/TypeScript ecosystem. This page covers the npm slice of the
[Value pipeline](../value.md) end to end: fetch mechanics first, then how
downloads and the dependency tree become a download-weighted PageRank and an
A/B/C value class for every package.

## Data Sources

| Signal | Endpoint | Lands in | Notes |
|---|---|---|---|
| Downloads | [api.npmjs.org/downloads/point](https://api.npmjs.org/downloads/point) | `raw/downloads.csv` | Per-package annual totals (2021–2025). Bulk endpoint, 128 packages per request. It rejects scoped `@…` packages, so those cost one request per package-year |
| Ecosystem totals | [api.npmjs.org/downloads/point](https://api.npmjs.org/downloads/point) | `raw/npm-stats.csv` | Total downloads per year — the 95% denominator. Data starts Jan 2015 |
| Dependencies | [registry.npmjs.org](https://registry.npmjs.org) | `raw/dependencies.csv` | `/{package}/latest` returns declared **runtime** dependencies |
| Licenses | [registry.npmjs.org](https://registry.npmjs.org) | `raw/licenses.csv` | `/{package}` full metadata — the license of the `dist-tags.latest` version |
| EOL | [registry.npmjs.org](https://registry.npmjs.org) | `eol.csv` | Abbreviated metadata; a non-empty `deprecated` string marks the package EOL |
| Package funding | [registry.npmjs.org](https://registry.npmjs.org) | `funding.csv` | `/{package}/latest` → `.funding`, the field `npm fund` reads |
| Repo mappings | [nice-registry](https://github.com/nice-registry/all-the-package-repos) | `nice-registry/packages.csv` | The full npm name→repo index, as a 212 MB `packages.json` |

No authentication required. An optional `NPM_TOKEN` in `.env` Bearer-auths
registry.npmjs.org for a higher limit there; the downloads API ignores it.

**Rate limiting.** npm publishes no fixed limit. A sustained ~1 req/s holds
clean; a sustained ~2 req/s draws continuous 429s. `fetch_npm_data.py`
therefore enforces a global 1 req/s limiter (`RATE_PER_SEC = 1.0`)
independent of `--concurrency`. On any 429 it pauses all in-flight tasks
together and backs off in 5 tiers (~200 s cumulative tolerance). It uses a
cookieless session, so a rate-limit-flagged Cloudflare `_cfuvid` cookie
cannot keep it throttled.

## Raw Data

In `data/sources/npm/raw/`:

| File | Schema | Notes |
|---|---|---|
| `downloads.csv` | `package, year, downloads` | |
| `downloads.status.csv` | `package, status, checked_at` | Per-package fetch verdict (`ok` \| `not_found`), 365-day TTL. A `downloads=0` row alone cannot separate a measured zero from a 404, so audits read this instead of re-fetching every all-zero package |
| `dependencies.csv` | `package, dep_name, dep_version, fetched_at` | Edges re-fetched after 365 days — `/latest` deps drift with releases |
| `licenses.csv` | `package, license, fetched_at` | Lowercase SPDX cache, 365-day TTL. `fetch_licenses.py` joins it into `results.csv` |
| `npm-stats.csv` | `year, downloads` | Ecosystem-wide totals — the 95% denominator |
| `top-packages.csv` | — | **Stale artifact.** Nothing in `src/`, `scripts/`, or `tests/` reads or writes it; the live file is `data/sources/npm/top-packages.csv` |

In `data/sources/npm/nice-registry/`:

| File | Schema | Notes |
|---|---|---|
| `packages.csv` | `package, repo_url` | Non-null entries of `packages.json`. Cached for **365 days** (`TTL_DAYS` in `fetch_nice_registry.py`) — a 212 MB download over a slow-moving index. `--refresh` forces it. A refresh introduces newly published packages whose repos have never been validated, so the next value run needs network for `build_validation`. |
| `metadata.csv` | `package, github_repo, repo_url` | **Stale artifact.** No reader or writer anywhere in the repo |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/npm/fetch_npm_data.py` | Iterative crawler — fetches downloads + deps until the graph is complete |
| `src/sources/npm/fetch_npm_stats.py` | Fetch ecosystem-wide annual download totals |
| `src/sources/npm/fetch_nice_registry.py` | Download the package→repo index. Skips when the local copy is inside the 365-day TTL |
| `src/sources/npm/process_data.py` | Build the outputs from raw data |
| `src/sources/npm/fetch_licenses.py` | Fetch SPDX licenses → `raw/licenses.csv`, then join into `results.csv` |
| `src/sources/npm/check_eol.py` | Flag packages whose latest version is `deprecated` → `eol.csv` |
| `src/sources/npm/fetch_funding.py` | Read the registry `funding` field for top npm packages → `funding.csv` |

```bash
uv run python -m src.sources.npm.fetch_nice_registry
uv run python -m src.sources.npm.fetch_npm_stats
uv run python -m src.sources.npm.fetch_npm_data [--max-rounds 20] [--concurrency 3] [--limit 50]
uv run python -m src.sources.npm.process_data [--ignore-gaps] [--concurrency 5]
uv run python -m src.sources.npm.fetch_licenses [--force] [--apply-only] [--limit 100]
uv run python -m src.sources.npm.check_eol [--refresh] [--limit 100] [--concurrency 50]
uv run python -m src.sources.npm.fetch_funding [--force] [--limit 20]
```

`fetch_licenses`, `check_eol`, and `fetch_funding` run as the `npm-lic`,
`npm-eol`, and `npm-funding` steps of `src.eligibility.run_eligibility_pipeline`.

## Value Pipeline

1. **top-packages.csv** — sort packages by average annual downloads. Keep the
   packages that cover 95% of the ecosystem-wide total (`raw/npm-stats.csv`).
2. **Expand the dependency tree** — follow transitive runtime deps from the top
   set. Fetch missing deps from the registry.
3. **Fetch missing downloads** — every dep-tree package gets download data.
4. **dependency-tree.csv** — all transitive runtime edges from the top packages.
5. **github-repos.csv** — match every dep-tree package against nice-registry.
6. **results.csv** — download-weighted personalized PageRank (α = 0.85) over the
   directed dep graph (`A → B` means *A depends on B*). Sort by PageRank
   descending; cumulative-share cutoffs assign value class A (≤75%),
   B (≤95%), C (rest).

`src.value.npm_pipeline` orchestrates the four steps fetch-data → fetch-stats →
fetch-repos → process. The shared Value mechanics are described in
[value.md](../value.md).

Metric lineage (`←` = data source, `[…]` = period):

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

## Outputs

In `data/sources/npm/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads — `package, avg_downloads, avg_downloads_share, 2021`–`2025` |
| `dependency-tree.csv` | Transitive runtime dep edges from the top packages — `package, dependency, type`; `type` is always `declared` |
| `github-repos.csv` | Package → GitHub repo mappings — `package, github_repo` |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | One row per dep-tree package — schema below |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |
| `funding.csv` | `repo, repo_id, package, has_npm_funding, npm_funding_url, fetched_at, status` |

`results.csv` columns:

| Column | Description |
|---|---|
| `package` | Package name |
| `github_repo` | `owner/repo` slug |
| `git`, `eco_guess` | Canonical git URL + identity provenance (`eco` / `native` / `override`), rewritten by the value rollup's ecosyste.ms authority pass (`src.value.apply_ecosystems_authority`) |
| `avg_downloads`, `2021`–`2025` | Downloads |
| `top` | `True` if in the 95% cumulative set |
| `pagerank` | Download-weighted PageRank score |
| `value_class` | A/B/C |
| `repo_id` | Host-namespaced repo id — `gh/<numeric id>` on GitHub, `gl/<nickname>-<numeric id>` on a custom GitLab host, bare `gl/<numeric id>` on gitlab.com (`to_repo_id` in `src/common/repos.py`) |
| `canonical_url` | Upstream clone URL, set when the hosted repo is a mirror |
| `license` | SPDX license (filled by `fetch_licenses.py`) |

Row counts: see the per-ecosystem value funnel in the preview pipeline sheet.
It also carries the npm funnel (top packages → dep tree → results → repo
coverage) and the class distribution.

## Downstream Use

- **Value** — each package's `value_class` is grouped by repo into
  `data/value/value.csv` as the `class_npm` column; the strongest class across
  ecosystems becomes `class`.
- **Risk** — class-A npm repos enter `src.risk.run_risk_pipeline` (scope set by
  `risk_input.value_classes` in `src/settings.json`).
- **Eligibility** — the same class-A repos (archived included) enter the
  automated [Eligibility stage](../eligibility.md)
  (`src.eligibility.run_eligibility_pipeline`), joined by `repo_id`.
  The per-ecosystem signals feed it: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/npm/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

npm has the cleanest upstream identity of the four ecosystems: `package.json`
carries a `repository` field, and nice-registry indexes it for the whole
registry, so nearly every dep-tree package resolves to a repo and reaches Risk
and Eligibility.
