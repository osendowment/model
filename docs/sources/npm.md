# npm

Package downloads, dependencies, and repository mappings for the JavaScript/TypeScript ecosystem.

## Data Sources

**Downloads**: [npm downloads API](https://api.npmjs.org/downloads/point) -- bulk endpoint, up to 128 packages per request.

**Dependencies**: [npm registry](https://registry.npmjs.org) -- `/{package}/latest` returns declared runtime dependencies.

**Repo mappings**: [nice-registry](https://github.com/nice-registry/all-the-package-repos) -- community-maintained package-to-repo mapping (~2M packages, 212 MB download).

**Ecosystem totals**: [npm downloads API](https://api.npmjs.org/downloads/point) -- total downloads per year (data starts Jan 2015).

No authentication required. npm downloads API is rate-limited to ~5 req/s.

## Raw Data

In `data/sources/npm/raw/`:
- `downloads.csv` -- package, year, downloads
- `dependencies.csv` -- package, dep_name, dep_version, fetched_at
- `npm-stats.csv` -- year, downloads (ecosystem-wide totals)

In `data/sources/npm/nice-registry/`:
- `packages.csv` -- package, repo_url

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/npm/fetch_npm_data.py` | Iterative crawler -- fetches downloads + deps until graph is complete |
| `src/sources/npm/fetch_npm_stats.py` | Fetch ecosystem-wide annual download totals |
| `src/sources/npm/fetch_nice_registry.py` | Download package-to-repo mappings (one-time) |
| `src/sources/npm/process_data.py` | Build outputs from raw data |

```bash
uv run src/sources/npm/fetch_nice_registry.py
uv run src/sources/npm/fetch_npm_data.py [--max-rounds 3] [--concurrency 20]
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

| File | Rows | Description |
|------|------|-------------|
| `top-packages.csv` | ~5.8K | Packages covering 95% of downloads (+ `avg_downloads_share`) |
| `dependency-tree.csv` | ~15K edges | Transitive deps from top packages |
| `github-repos.csv` | ~6.3K | Package-to-GitHub-repo mappings |
| `results.csv` | ~5.7K | All dep-tree packages with pagerank + value_class |
