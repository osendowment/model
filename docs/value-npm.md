# Value Pipeline: npm

## Data Sources

**Downloads**: [npm downloads API](https://api.npmjs.org/downloads/point) — bulk endpoint supports up to 128 packages per request.

```
GET https://api.npmjs.org/downloads/point/2024-01-01:2024-12-31/semver,lodash,react
→ {"semver": {"downloads": 16558352576, ...}, "lodash": {...}, ...}
```

**Dependencies**: [npm registry](https://registry.npmjs.org) — `/{package}/latest` returns declared runtime dependencies.

**Repo mappings**: [nice-registry/all-the-package-repos](https://github.com/nice-registry/all-the-package-repos) — community-maintained `package → repo_url` mapping (~2M packages).

### Raw Data Files

In `data/npm/raw/`:
- `downloads.csv` — `package, year, downloads` for all fetched packages
- `dependencies.csv` — `package, dep_name, dep_version, fetched_at`
- `nice-registry/packages.csv` — `package, repo_url`

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/npm/fetch_nice_registry.py` | Download package→repo mappings (one-time, ~212 MB) | `uv run src/npm/fetch_nice_registry.py` |
| `src/npm/fetch_npm_data.py` | Iterative graph crawler — fetches downloads + deps until no gaps | `uv run src/npm/fetch_npm_data.py` |
| `src/npm/process_data.py` | Build all output CSVs from raw data | `uv run src/npm/process_data.py` |

Options:
```bash
uv run src/npm/fetch_npm_data.py --concurrency 10   # tune request rate
uv run src/npm/fetch_npm_data.py --limit 50          # test mode
uv run src/npm/process_data.py --ignore-gaps         # skip fetching, use available data
```

### Pipeline Steps (inside process_data.py)

1. **top-packages.csv** — Filter `raw/downloads.csv` to packages with avg ≥ 1M annual downloads
2. **build dep tree** *(iterative)* — BFS from top packages through `raw/dependencies.csv`. For any tree node not yet in deps, fetch from the npm registry and upsert. Repeat until the full transitive dep tree is covered
3. **fetch missing downloads** — For every dep-tree package, ensure `raw/downloads.csv` has all 5 years. Fetches newest-to-oldest; short-circuits on the first year where the API returns null
4. **dependency-tree.csv** — Write all transitive edges reachable from top packages
5. **github-repos.csv** — Match dep-tree packages against `nice-registry/packages.csv`; extract `owner/repo` slugs
6. **results.csv** — Run download-weighted PageRank over the dep graph and join with download data

## Outputs

### top-packages.csv

Packages with avg ≥ 1M annual downloads.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `avg_downloads` | Average annual downloads across populated years |
| `2021`–`2025` | Downloads for that calendar year |

### dependency-tree.csv

Full transitive dep tree rooted at top packages.

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | Always `declared` for npm |

### github-repos.csv

GitHub repos for packages in the dep tree.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | GitHub repository (`owner/repo`) |

### results.csv

All dep-tree packages scored.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | GitHub repository (`owner/repo`) |
| `avg_downloads` | Average annual downloads |
| `2021`–`2025` | Downloads for that calendar year |
| `top` | `True` if the package meets the ≥ 1M threshold |
| `pagerank` | Download-weighted PageRank in the dependency graph |
| `value_class` | Value class (A/B/C/D) based on cumulative pagerank share — see [value classes](#value-classes) |

### Value Classes

Packages are classified into four tiers based on how much ecosystem value they
account for, measured by cumulative share of total pagerank (sorted descending):

| Class | Cumulative Share | Meaning |
|-------|-----------------|---------|
| **A** | 0–50% | Critical infrastructure — ecosystem breaks without these |
| **B** | 50–75% | Important — widely used, significant dependency chains |
| **C** | 75–90% | Useful — meaningful but not load-bearing |
| **D** | 90–100% | Long tail — niche, leaf nodes, minimal ecosystem impact |

Current distribution (~20K packages):

| Class | Packages | % of total |
|-------|----------|-----------|
| A | ~350 | 1.7% |
| B | ~815 | 4.1% |
| C | ~1,461 | 7.3% |
| D | ~17,454 | 86.9% |
