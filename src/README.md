# src

## github/

GitHub API modules for repo search and contributor analysis.

| Module | Purpose |
|--------|---------|
| `models.py` | Data types — Contributor, RunResult, DateRange, bot detection |
| `api.py` | GitHub API interaction — sync + async fetch, rate limiting |
| `contributors.py` | Core algorithms, git clone analysis, and CLI entry point |
| `display.py` | Rich terminal output — tables, spinners, formatting |
| `batch.py` | Async batch processing + CSV I/O |
| `search.py` | GitHub repo search by language/stars |
| `locs.py` | LOC estimation via GitHub /languages endpoint |

### Contributor Analysis

```bash
# Single repo — auto-detects years from first activity
uv run python -m src.github.contributors curl/curl

# Explicit year range
uv run python -m src.github.contributors facebook/react --years 2021 2025

# Batch — reads top-repos.csv, writes repo-contrib-metrics.csv
uv run python -m src.github.contributors

# First 10 repos only
uv run python -m src.github.contributors --top 10
```

### Repo Search

```bash
# Search by language and star count
uv run python -m src.github.search --language Python --min-stars 10000

# Multiple languages, custom output
uv run python -m src.github.search --language C "C++" --min-stars 1000 \
    --output data/github/c-repos.csv
```

## eligibility.py

Classifies repos by OSS license eligibility (OSI-approved licenses).

```bash
uv run python -m src.eligibility
```

### LOC Estimation

```bash
# Single repo
uv run python -m src.github.locs curl/curl

# Batch — reads top-repos.csv, writes locs.csv
uv run python -m src.github.locs --top 100

# Force refresh (ignore TTL cache)
uv run python -m src.github.locs --ttl 0
```

## risk.py

Aggregates contributor metrics into risk classifications per repo.

### Concentration Class

Based on bus factor (BF) and Herfindahl-Hirschman Index (HHI):

| Class | Label | Criteria |
|-------|-------|----------|
| A | critical | BF=1, HHI ≥ 8000 |
| B | high risk | BF ≤ 2, HHI ≥ 5000 |
| C | moderate | BF ≤ 4, HHI ≥ 2500 |
| D | healthy | otherwise |

### Complexity Class

Based on estimated lines of code (from `locs.py`):

| Class | Label | Criteria |
|-------|-------|----------|
| A | massive | ≥ 1M LOC |
| B | large | 100K – 1M LOC |
| C | moderate | 10K – 100K LOC |
| D | small | < 10K LOC |

```bash
uv run python -m src.risk
```
