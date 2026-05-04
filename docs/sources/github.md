# GitHub

Repository metadata, contributor metrics, and code complexity for open-source projects.

## Data Sources

**Search API**: `api.github.com/search/repositories` -- find repos by language and star count. Bypasses 1K result cap via `created_at` date cohorts with binary splitting.

**Repos API**: `api.github.com/repos/{owner}/{repo}` -- fetch metadata for individual repos (used for ecosystem backfill).

**Contributor stats API**: `api.github.com/repos/{owner}/{repo}/stats/contributors` -- per-contributor weekly commit history. Returns 202 while computing (retried with backoff).

**Git metrics**: sparse checkout or tarball download + [scc](https://github.com/boyter/scc) for code analysis (LOC, complexity, COCOMO).

**Authentication required**: GitHub personal access tokens. Supports multiple tokens via `GITHUB_TOKENS` env var (comma-separated) with automatic rotation. 5,000 req/hr per token.

## Raw Data

In `data/github/search/`:
- `top-repos.csv` -- ~32K repos with metadata (stars, forks, license, language, etc.)
- `repo-counts.csv` -- cached search API counts (skip repeat queries)

In `data/github/contributors/` (wide format: `repo, 2021...2025, 2021-2025`):
- `bus-factor.csv` -- minimum contributors for 50% of commits
- `hhi.csv` -- Herfindahl-Hirschman Index (0-10000)
- `contributors.csv` -- human contributor count
- `bots.csv` -- bot contributor count
- `commits.csv` -- human commit count
- `years.csv` -- long format: repo, year, first_date, last_date

In `data/github/git/`:
- `commits-years.csv` -- per (repo, year) `last_sha` + `commits` (foundation for sha-pinned snapshots)
- `churn.csv` -- 5y added/deleted lines per repo (range-based)

In `data/git/` (long-format sha-pinned, schema: `repo, repo_id, commit_sha, metric, value, checked_at`):
- `scc.csv` -- scc metrics: `loc`, `sloc`, `files`, `uloc`, `complexity`, `complexity_density`
- `lizard.csv` -- lizard cognitive + cyclomatic + halstead + maintainability_index
- `semgrep.csv` -- semgrep findings (rulepack-prefixed metrics, e.g. `p_default.total`)
- `openssf.csv` -- OpenSSF Scorecard `score` + 18 per-check scores
- `depsdev.csv` -- deps.dev-mirrored Scorecard score + checks (fall-back when local row missing)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/github/fetch_top_repos.py` | Search repos by language/stars; backfill ecosystem repos |
| `src/github/fetch_contributors_metrics.py` | Contributor analysis (bus factor, HHI) |
| `src/git/commits_years.py` | Resolve per (repo, year) `last_sha` + `commits` |
| `src/git/fetch_scc.py` | scc code analysis via sparse checkout (writes long format) |
| `src/github/fetch_advanced_complexity.py` | Lizard cyclomatic + multimetric Halstead + MI |
| `src/github/fetch_cognitive.py` | Sonar cognitive complexity (Lizard + AST) |
| `src/github/fetch_semgrep.py` | Semgrep findings (security/correctness rulepacks) |
| `src/github/github_client.py` | API client with token rotation + rate limiting |
| `src/github/batch_runner.py` | Async batch processing + CSV I/O |
| `src/github/models.py` | Data types (Contributor, RunResult, bot detection) |
| `src/github/display.py` | Rich terminal output |

### Repo search (by language, 1K+ stars)

```bash
uv run python -m src.github.fetch_top_repos --language Python --min-stars 1000
uv run python -m src.github.fetch_top_repos --language JavaScript TypeScript --min-stars 1000
uv run python -m src.github.fetch_top_repos --language Rust --min-stars 1000
uv run python -m src.github.fetch_top_repos --language C "C++" --min-stars 1000
```

### Backfill ecosystem AB repos

```bash
uv run python -m src.github.fetch_top_repos --backfill-only
uv run python -m src.github.fetch_top_repos --backfill-only --limit 20
```

### Contributor metrics

```bash
uv run python -m src.github.fetch_contributors_metrics                  # batch all
uv run python -m src.github.fetch_contributors_metrics curl/curl        # single repo
uv run python -m src.github.fetch_contributors_metrics --limit 10       # sample
```

### Git metrics (scc — long format)

```bash
uv run python -m src.git.fetch_scc --limit 40
uv run python -m src.git.fetch_scc --force                   # bypass freshness skip
```

Per-year `last_sha` foundation:

```bash
uv run python -m src.git.commits_years --limit 40
```
