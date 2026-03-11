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

## risk.py

Aggregates contributor metrics into concentration risk classifications (A/B/C/D) per repo.

```bash
uv run python -m src.risk
```
