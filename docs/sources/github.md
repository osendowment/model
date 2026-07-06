# GitHub

Repository metadata, contributor metrics, and code complexity for open-source projects.

## Data Sources

**Search API**: `api.github.com/search/repositories` -- find repos by language and star count. Bypasses 1K result cap via `created_at` date cohorts with binary splitting.

**Repos API**: `api.github.com/repos/{owner}/{repo}` -- fetch metadata for individual repos (used for ecosystem backfill).

**Contributors API**: `api.github.com/repos/{owner}/{repo}/contributors` -- lifetime per-contributor commit totals, keyed by login. (The `/stats/contributors` weekly-history endpoint is intentionally not used -- it returns HTTP 202 "computing" indefinitely for most repos.)

**Git metrics**: sparse checkout or tarball download + [scc](https://github.com/boyter/scc) for code analysis (LOC/SLOC/ULOC, file counts, complexity).

**Authentication required**: GitHub personal access tokens. Supports multiple tokens via `GITHUB_TOKENS` env var (comma-separated) with automatic rotation. 5,000 req/hr per token.

## Raw Data

In `data/sources/github/search/`:
- `top-repos.csv` -- searched repos with metadata (stars, forks, license, language, etc.; counts in the preview stats sheet)
- `repo-counts.csv` -- cached search API counts (skip repeat queries)

Contributor raw data (long format; bus factor / HHI are computed downstream by
`src/risk/build_concentration.py` from these rows):
- `data/sources/github/contributor-commits.csv` -- one row per (repo, contributor) from the
  `/contributors` API: `repo, repo_id, git_url, login, contributions, account_type`
- `data/sources/github/contributor-commits.status.csv` -- per-repo fetch status sidecar:
  `repo, repo_id, git_url, status, n_contributors, fetched_at`

All git-clone / git-analysis raw data lives under `data/sources/git/` (host-agnostic:
every row carries both `repo_id` — `gh/` or `gl/` — and the `git_url` it was cloned
from, so a fetcher clones the real host rather than assuming `github.com/{repo}`):
- `commits-years.csv` -- per (repo, year) `last_sha` + `commits` (foundation for sha-pinned snapshots)
- `churn.csv` -- 5y added/deleted lines per repo (range-based)
- long-format sha-pinned (schema: `repo, repo_id, git_url, commit_sha, metric, value, checked_at`):
  - `scc.csv` -- scc metrics: `loc`, `sloc`, `files`, `uloc`, `complexity`, `complexity_density`
  - `lizard.csv` -- lizard cyclomatic + cognitive + Halstead + maintainability index + files
  - `openssf.csv` -- OpenSSF Scorecard `score` + 18 per-check scores
  - `depsdev.csv` -- deps.dev-mirrored Scorecard score + checks (fall-back when local row missing)
- `contributor-commits.csv` -- git-clone contributor method, long raw: `repo, repo_id, git_url,
  author_name, author_email, year, commits` (+ `contributor-commits.status.csv` per-repo status sidecar)
- `urls.csv` -- non-GitHub clone-URL validation cache (`url, valid, method, checked_at`;
  written by the value stage's `build_git_urls` / `build_validation`)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/github/fetch_top_repos.py` | Search repos by language/stars; backfill ecosystem repos |
| `src/sources/github/fetch_contributors_metrics.py` | Fetch per-contributor commit totals (raw rows for bus factor / HHI) |
| `src/sources/github/bf_contributors.py` | Bus-factor contributor membership (which logins make up `bf_commits_gh_alltime`) |
| `src/sources/github/fetch_maintainer_sponsors.py` | Personal GitHub Sponsors listing per bus-factor maintainer (→ funding-intent `bf_maintainer_fundable`) |
| `src/sources/git/commits_years.py` | Resolve per (repo, year) `last_sha` + `commits` |
| `src/sources/git/contributors.py` | git-clone contributor commits (long raw + status sidecar) |
| `src/sources/github/fetch_churn.py` | 5y line churn per repo (clone-based) → `data/sources/git/churn.csv` |
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
