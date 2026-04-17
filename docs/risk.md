# Risk Pipeline

## Data Sources

**GitHub API** — repo metadata, contributor commit history.
**Git clones** — scc-based code complexity via sparse checkout / tarball.

**Inputs** (produced by earlier pipeline stages):

*Search stage:*
- `data/github/search/top-repos.csv` — GitHub repos with 1K+ stars across tracked languages
- `data/github/search/repo-counts.csv` — cached search API counts for date range optimization

*Contributor metrics (wide: `repo, 2021…2025, 2021-2025`):*
- `data/github/contributors/bus-factor.csv` — yearly bus factor per repo
- `data/github/contributors/hhi.csv` — yearly HHI per repo
- `data/github/contributors/contributors.csv` — yearly human contributor count
- `data/github/contributors/bots.csv` — yearly bot contributor count
- `data/github/contributors/commits.csv` — yearly human commit count
- `data/github/contributors/years.csv` — long format: `repo, year, first_date, last_date`

*Git-based (scc) metrics in `data/github/git/` (wide: `repo, 2021…2025`):*
- `files.csv`, `loc.csv`, `sloc.csv`, `uloc.csv`, `scc-complexity.csv`, `scc-density.csv`
- `complexity.csv` — HEAD snapshot with files/loc/uloc/complexity/cocomo fields
- `years.csv` — `repo, year, last_sha` — commit SHAs per repo per year

## Scripts

| Script | Purpose | Command |
|--------|---------|---------|
| `src/github/fetch_top_repos.py` | GitHub repo search by language/stars | `uv run python -m src.github.fetch_top_repos` |
| `src/github/fetch_contributors_metrics.py` | Contributor analysis — bus factor, HHI | `uv run python -m src.github.fetch_contributors_metrics` |
| `src/github/fetch_git_metrics.py` | scc code analysis via sparse checkout | `uv run python -m src.github.fetch_git_metrics` |
| `src/github/github_client.py` | GitHub API client — token revolver, rate limiting | — |
| `src/github/batch_runner.py` | Async batch processing + CSV I/O | — |
| `src/github/models.py` | Data types — Contributor, RunResult, DateRange, bot detection | — |
| `src/github/display.py` | Rich terminal output — tables, spinners, formatting | — |
| `src/classify_risk.py` | Aggregate into risk classifications | `uv run python -m src.classify_risk` |

### Collection Commands

**Repo Search:**
```bash
uv run python -m src.github.fetch_top_repos --language Python --min-stars 10000
uv run python -m src.github.fetch_top_repos --language C "C++" --min-stars 1000
```

Update all target ecosystems (1K+ stars):
```bash
uv run python -m src.github.fetch_top_repos --language Python --min-stars 1000
uv run python -m src.github.fetch_top_repos --language JavaScript --min-stars 1000
uv run python -m src.github.fetch_top_repos --language TypeScript --min-stars 1000
uv run python -m src.github.fetch_top_repos --language Rust --min-stars 1000
uv run python -m src.github.fetch_top_repos --language C "C++" --min-stars 1000
```

**Contributor Analysis:**
```bash
uv run python -m src.github.fetch_contributors_metrics curl/curl              # single repo
uv run python -m src.github.fetch_contributors_metrics facebook/react --years 2021 2025
uv run python -m src.github.fetch_contributors_metrics                        # batch (all top-repos)
uv run python -m src.github.fetch_contributors_metrics --limit 10             # random sample
```

**Git Metrics (scc):**
```bash
uv run python -m src.github.fetch_git_metrics --limit 40
uv run python -m src.github.fetch_git_metrics --ttl 0         # force refresh
uv run python -m src.github.fetch_git_metrics --year 2025     # year-end snapshot
```

**Risk Classification:**
```bash
uv run python -m src.classify_risk
```

## Outputs

### risk-metrics.csv

Risk classifications per repo.

| Column | Description |
|--------|-------------|
| `repo` | GitHub repo slug (owner/name) |
| `repo_id` | GitHub numeric repo ID |
| `active_contributors` | Contributors with commits in 2021–2025 |
| `hhi_commits` | Herfindahl-Hirschman Index (0–10000) — higher = more concentrated |
| `bus_factor_commits` | Minimum contributors accounting for 50% of commits |
| `loc` | scc-counted lines of code (most recent year from `git/loc.csv`) |
| `concentration_class` | A–D classification (see below) |
| `complexity_class` | A–D classification (see below) |

### Concentration Class

Based on bus factor (BF) and Herfindahl-Hirschman Index (HHI):

| Class | Label | Criteria |
|-------|-------|----------|
| A | critical | BF=1, HHI ≥ 8000 |
| B | high risk | BF ≤ 2, HHI ≥ 5000 |
| C | moderate | BF ≤ 4, HHI ≥ 2500 |
| D | healthy | otherwise |

### Complexity Class

Based on lines of code (scc):

| Class | Label | Criteria |
|-------|-------|----------|
| A | massive | ≥ 1M LOC |
| B | large | 100K – 1M LOC |
| C | moderate | 10K – 100K LOC |
| D | small | < 10K LOC |
