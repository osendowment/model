# Datasets

All data is derived from public sources (GitHub API, PyPI).

## GitHub

| File | Description |
|------|-------------|
| [top-repos.csv](github/top-repos.csv) | GitHub repos with 1K+ stars across tracked languages |
| [repo-contrib-metrics.csv](github/repo-contrib-metrics.csv) | Yearly contributor metrics (bus factor, HHI, contributor count) |
| [repo-year-sha.csv](github/repo-year-sha.csv) | Commit SHAs per repo per year (coordination file for git-metrics pipeline) |
| [repo-search-counts.csv](github/repo-search-counts.csv) | Cached search API counts for date range optimization |
| [locs.csv](github/locs.csv) | Estimated LOC per repo from GitHub /languages endpoint |
| [language-stats.csv](github/language-stats.csv) | Language metadata: scc names, file extensions, bytes-per-line, scc support flag |

## PyPI

| File | Description |
|------|-------------|
| [top-packages.csv](pypi/top-packages.csv) | PyPI packages with 1M+ avg annual downloads (2020–2024) |
| [package-dependencies.csv](pypi/package-dependencies.csv) | Package dependency graph |
| [package-github-mapping.csv](pypi/package-github-mapping.csv) | PyPI package → GitHub repo mapping |

## Derived

| File | Description |
|------|-------------|
| [eligibility.csv](eligibility.csv) | OSS license eligibility per repo |
| [risk-metrics.csv](risk-metrics.csv) | Risk classifications per repo (see columns below) |

### risk-metrics.csv columns

| Column | Description |
|--------|-------------|
| `repo` | GitHub repo slug (owner/name) |
| `repo_id` | GitHub numeric repo ID |
| `active_contributors` | Contributors with commits in 2021–2025 |
| `hhi_commits` | Herfindahl-Hirschman Index (0–10000) — higher = more concentrated |
| `bus_factor_commits` | Minimum contributors accounting for 50% of commits |
| `est_locs` | Estimated lines of code (from GitHub /languages endpoint) |
| `concentration_class` | A (critical) / B (high risk) / C (moderate) / D (healthy) — based on BF + HHI |
| `complexity_class` | A (massive, ≥1M) / B (large, 100K–1M) / C (moderate, 10K–100K) / D (small, <10K) — based on LOC |
