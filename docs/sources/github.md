# GitHub

Repository metadata, contributor metrics, and code complexity for open-source projects.

## Data Sources

**Search API**: `api.github.com/search/repositories` -- find repos by language and star count. Bypasses 1K result cap via `created_at` date cohorts with binary splitting.

**Repos API**: `api.github.com/repos/{owner}/{repo}` -- fetch metadata for individual repos (used for ecosystem backfill).

**Contributor stats API**: `api.github.com/repos/{owner}/{repo}/stats/contributors` -- per-contributor weekly commit history. Returns 202 while computing (retried with backoff).

**Git metrics**: sparse checkout or tarball download + [scc](https://github.com/boyter/scc) for code analysis (LOC, complexity, COCOMO).

**Authentication required**: GitHub personal access tokens. Supports multiple tokens via `GITHUB_TOKENS` env var (comma-separated) with automatic rotation. 5,000 req/hr per token.

## Raw Data

In `data/sources/github/search/`:
- `top-repos.csv` -- ~32K repos with metadata (stars, forks, license, language, etc.)
- `repo-counts.csv` -- cached search API counts (skip repeat queries)

In `data/sources/github/contributors/` (wide format: `repo, 2021...2025, 2021-2025`):
- `bus-factor.csv` -- minimum contributors for 50% of commits
- `hhi.csv` -- Herfindahl-Hirschman Index (0-10000)
- `contributors.csv` -- human contributor count
- `bots.csv` -- bot contributor count
- `commits.csv` -- human commit count
- `years.csv` -- long format: repo, year, first_date, last_date

All git-clone / git-analysis raw data lives under `data/sources/git/` (host-agnostic:
every row carries both `repo_id` — `gh/` or `gl/` — and the `git_url` it was cloned
from, so a fetcher clones the real host rather than assuming `github.com/{repo}`):
- `commits-years.csv` -- per (repo, year) `last_sha` + `commits` (foundation for sha-pinned snapshots)
- `churn.csv` -- 5y added/deleted lines per repo (range-based)
- long-format sha-pinned (schema: `repo, repo_id, git_url, commit_sha, metric, value, checked_at`):
  - `scc.csv` -- scc metrics: `loc`, `sloc`, `files`, `uloc`, `complexity`, `complexity_density`
  - `lizard.csv` -- lizard cyclomatic + cognitive + files
  - `openssf.csv` -- OpenSSF Scorecard `score` + 18 per-check scores
  - `depsdev.csv` -- deps.dev-mirrored Scorecard score + checks (fall-back when local row missing)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/github/fetch_top_repos.py` | Search repos by language/stars; backfill ecosystem repos |
| `src/sources/github/fetch_contributors_metrics.py` | Contributor analysis (bus factor, HHI) |
| `src/sources/github/bf_contributors.py` | Bus-factor contributor membership (which logins make up `bf_commits_gh_alltime`) |
| `src/sources/github/fetch_maintainer_sponsors.py` | Personal GitHub Sponsors listing per bus-factor maintainer (→ funding-intent `bf_maintainer_fundable`) |
| `src/sources/git/commits_years.py` | Resolve per (repo, year) `last_sha` + `commits` |
| `src/sources/git/fetch_scc.py` | scc code analysis via sparse checkout (writes long format; helpers reused by the unified SHA-metrics fetcher) |
| `src/sources/git/fetch_sha_metrics.py` | Unified SHA-pinned metrics: one sparse checkout → scc + both lizard passes (cyclomatic McCabe + Sonar cognitive) → `scc.csv` + `lizard.csv` |
| `src/sources/github/github_client.py` | API client with token rotation + rate limiting |
| `src/sources/github/batch_runner.py` | Async batch processing + CSV I/O |
| `src/sources/github/models.py` | Data types (Contributor, RunResult, bot detection) |
| `src/sources/github/display.py` | Rich terminal output |

### Repo search (by language, 1K+ stars)

```bash
uv run python -m src.sources.github.fetch_top_repos --language Python --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language JavaScript TypeScript --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language Rust --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language C "C++" --min-stars 1000
```

### Backfill ecosystem AB repos

```bash
uv run python -m src.sources.github.fetch_top_repos --backfill-only
uv run python -m src.sources.github.fetch_top_repos --backfill-only --limit 20
```

### Contributor metrics

```bash
uv run python -m src.sources.github.fetch_contributors_metrics                  # batch all
uv run python -m src.sources.github.fetch_contributors_metrics curl/curl        # single repo
uv run python -m src.sources.github.fetch_contributors_metrics --limit 10       # sample
```

### Git metrics (scc — long format)

```bash
uv run python -m src.sources.git.fetch_scc --limit 40
uv run python -m src.sources.git.fetch_scc --force                   # bypass freshness skip
```

Per-year `last_sha` foundation:

```bash
uv run python -m src.sources.git.commits_years --limit 40
```
