# npm

Package downloads, dependencies, and repository mappings for the
JavaScript/TypeScript ecosystem.

## Data Sources

| Signal | Endpoint | Notes |
|---|---|---|
| Downloads | [api.npmjs.org/downloads/point](https://api.npmjs.org/downloads/point) | Bulk endpoint, 128 packages per request. It rejects scoped `@…` packages, so those cost one request per package-year |
| Ecosystem totals | [api.npmjs.org/downloads/point](https://api.npmjs.org/downloads/point) | Total downloads per year. Data starts Jan 2015 |
| Dependencies | [registry.npmjs.org](https://registry.npmjs.org) | `/{package}/latest` returns declared **runtime** dependencies |
| Licenses | [registry.npmjs.org](https://registry.npmjs.org) | `/{package}` full metadata — the license of the `dist-tags.latest` version |
| EOL | [registry.npmjs.org](https://registry.npmjs.org) | Abbreviated metadata; a non-empty `deprecated` string marks the package EOL |
| Package funding | [registry.npmjs.org](https://registry.npmjs.org) | `/{package}/latest` → `.funding`, the field `npm fund` reads |
| Repo mappings | [nice-registry](https://github.com/nice-registry/all-the-package-repos) | The full npm name→repo index, as a 212 MB `packages.json` |

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
| `licenses.csv` | `package, license, fetched_at` | Lowercase SPDX cache, 90-day TTL. `fetch_licenses.py` joins it into `results.csv` |
| `npm-stats.csv` | `year, downloads` | Ecosystem-wide totals — the 95% denominator |
| `top-packages.csv` | — | **Stale artefact.** Nothing in `src/`, `scripts/`, or `tests/` reads or writes it; the live file is `data/sources/npm/top-packages.csv` |

In `data/sources/npm/nice-registry/`:

| File | Schema | Notes |
|---|---|---|
| `packages.csv` | `package, repo_url` | Non-null entries of `packages.json` |
| `metadata.csv` | `package, github_repo, repo_url` | **Stale artefact.** No reader or writer anywhere in the repo |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/npm/fetch_npm_data.py` | Iterative crawler — fetches downloads + deps until the graph is complete |
| `src/sources/npm/fetch_npm_stats.py` | Fetch ecosystem-wide annual download totals |
| `src/sources/npm/fetch_nice_registry.py` | Download the package→repo index. Skips when the local copy is under 24 h old |
| `src/sources/npm/process_data.py` | Build the outputs from raw data |
| `src/sources/npm/fetch_licenses.py` | Fetch SPDX licenses → `raw/licenses.csv`, then join into `results.csv` |
| `src/sources/npm/check_eol.py` | Flag packages whose latest version is `deprecated` → `eol.csv` |
| `src/sources/npm/fetch_funding.py` | Read the registry `funding` field for top npm packages → `funding.csv` |

```bash
uv run python -m src.sources.npm.fetch_nice_registry
uv run python -m src.sources.npm.fetch_npm_stats
uv run src/sources/npm/fetch_npm_data.py [--max-rounds 20] [--concurrency 3] [--limit 50]
uv run python -m src.sources.npm.process_data [--ignore-gaps] [--concurrency 5]
uv run python -m src.sources.npm.fetch_licenses [--force] [--apply-only] [--limit 100]
uv run python -m src.sources.npm.check_eol [--refresh] [--limit 100] [--concurrency 50]
uv run python -m src.sources.npm.fetch_funding [--force] [--limit 20]
```

`fetch_licenses`, `check_eol`, and `fetch_funding` run as the `npm-lic`,
`npm-eol`, and `npm-funding` steps of `src.eligibility.run_eligibility_pipeline`.

## Pipeline

1. **top-packages.csv** — packages covering 95% of ecosystem downloads.
2. **Expand the dependency tree** — follow transitive deps from the top set,
   fetching missing deps from the registry.
3. **Fetch missing downloads** — every dep-tree package gets download data.
4. **dependency-tree.csv** — all transitive edges from the top packages.
5. **github-repos.csv** — match dep-tree packages against nice-registry.
6. **results.csv** — download-weighted PageRank, value classes A/B/C.

## Outputs

In `data/sources/npm/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive runtime dep edges from the top packages |
| `github-repos.csv` | Package → GitHub repo mappings |
| `git.csv` | Package → upstream git URL per host (`github`, `gitlab`, `bitbucket`, `sourcehut`, `codeberg`, `custom`, `eco_guess`); written by the value stage |
| `results.csv` | Every dep-tree package with `pagerank`, `value_class`, `repo_id`, `canonical_url`, `license` |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |
| `funding.csv` | `repo, repo_id, package, has_npm_funding, npm_funding_url, fetched_at, status` |

Row counts: see the per-ecosystem value funnel in the preview pipeline sheet.
