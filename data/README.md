# Datasets

All data is derived from public sources (GitHub API, PyPI).

## GitHub

| File | Description |
|------|-------------|
| [top-repos.csv](github/top-repos.csv) | GitHub repos with 1K+ stars across tracked languages |
| [repo-contrib-metrics.csv](github/repo-contrib-metrics.csv) | Yearly contributor metrics (bus factor, HHI, contributor count) |
| [repo-search-counts.csv](github/repo-search-counts.csv) | Cached search API counts for date range optimization |

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
| [risk-metrics.csv](risk-metrics.csv) | Concentration risk classification (A/B/C/D) per repo |
