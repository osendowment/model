# npm

Package downloads, dependencies, and repository mappings for the JavaScript/TypeScript ecosystem.

## Data Sources

**Downloads**: [npm downloads API](https://api.npmjs.org/downloads/point) -- bulk endpoint, up to 128 packages per request (unscoped packages only; scoped `@...` packages are rejected by the bulk endpoint and fetched one request per package-year).

**Dependencies**: [npm registry](https://registry.npmjs.org) -- `/{package}/latest` returns declared runtime dependencies.

**Repo mappings**: [nice-registry](https://github.com/nice-registry/all-the-package-repos) -- community-maintained package-to-repo mapping (~2M packages, 212 MB download).

**Ecosystem totals**: [npm downloads API](https://api.npmjs.org/downloads/point) -- total downloads per year (data starts Jan 2015).

No authentication required (an optional `NPM_TOKEN` in `.env` Bearer-auths registry.npmjs.org dependency lookups for a higher limit there; the downloads API ignores it). npm publishes no fixed rate limit; empirically a sustained ~1 req/s holds clean while a sustained ~2 req/s draws continuous 429s. The fetcher therefore enforces a global 1 req/s limiter (independent of `--concurrency`), pauses all in-flight tasks together on any 429 with tiered backoff (5 tiers, ~200 s cumulative tolerance), and uses a cookieless session so a rate-limit-flagged Cloudflare `_cfuvid` cookie can't keep it throttled.

## Raw Data

In `data/sources/npm/raw/`:
- `downloads.csv` -- package, year, downloads
- `downloads.status.csv` -- package, status (`ok` | `not_found`), checked_at. Per-package fetch-verdict sidecar: a `downloads=0` row alone cannot distinguish a measured zero from a package that 404s on npm, so audits consult this (365-day TTL) instead of re-fetching every all-zero package
- `dependencies.csv` -- package, dep_name, dep_version, fetched_at (edges re-fetched after 365 days -- `/latest` deps drift with releases)
- `npm-stats.csv` -- year, downloads (ecosystem-wide totals)

In `data/sources/npm/nice-registry/`:
- `packages.csv` -- package, repo_url

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/npm/fetch_npm_data.py` | Iterative crawler -- fetches downloads + deps until graph is complete |
| `src/sources/npm/fetch_npm_stats.py` | Fetch ecosystem-wide annual download totals |
| `src/sources/npm/fetch_nice_registry.py` | Download package-to-repo mappings (skipped when the local copy is < 24 h old) |
| `src/sources/npm/process_data.py` | Build outputs from raw data |

```bash
uv run src/sources/npm/fetch_nice_registry.py
uv run src/sources/npm/fetch_npm_data.py [--max-rounds 20] [--concurrency 3] [--limit 50]
uv run python -m src.sources.npm.fetch_npm_stats
uv run python -m src.sources.npm.process_data [--ignore-gaps]
```

## Pipeline

1. **top-packages.csv** -- packages covering 95% of ecosystem downloads
2. **Expand dependency tree** -- follow transitive deps from top packages, fetching missing deps from npm registry
3. **Fetch missing downloads** -- ensure all dep-tree packages have download data
4. **dependency-tree.csv** -- all transitive edges from top packages
5. **github-repos.csv** -- match dep-tree packages against nice-registry
6. **results.csv** -- download-weighted PageRank, value classes A/B/C

## Outputs

In `data/sources/npm/`:

| File | Description |
|------|-------------|
| `top-packages.csv` | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | Transitive dep edges from top packages |
| `github-repos.csv` | Package-to-GitHub-repo mappings |
| `results.csv` | All dep-tree packages with pagerank + value_class |

Row counts: see the per-ecosystem value funnel in the preview stats sheet.
