# GitHub

Repository and owner metadata, funding declarations, issue counts, and
contributor lists for GitHub-hosted projects.

## Data Sources

| API | Endpoint | What it gives |
|-----|----------|---------------|
| Search | `/search/repositories` | Repos by language + star count. Beats the 1K result cap with `created_at` date cohorts and binary splitting |
| Repos | `/repos/{owner}/{repo}` | Per-repo metadata: license, language, stars, `archived`, `pushed_at`, owner |
| Users / Orgs | `/users/{login}`, `/orgs/{login}` | Owner identity: display name, company, blog, followers |
| Contributors | `/repos/{owner}/{repo}/contributors` | Lifetime per-login commit counts. `/stats/contributors` is unusable — it answers HTTP 202 indefinitely |
| Issues | `/search/issues` | Per (repo, year) opened / closed counts |
| Commits | `/repos/{owner}/{repo}/commits` | Per (repo, year) first/last SHA + in-year count — the anchor every sha-pinned analysis keys off (fetched by [`src/sources/git/`](git.md)) |
| GraphQL | `/graphql` | `fundingLinks`, `sponsorshipsAsSponsor`, `hasSponsorsListing`, and `nameWithOwner` + `databaseId` for rename resolution |

**Authentication required.** Pass one or more personal access tokens in
`GITHUB_TOKENS` (comma-separated); `github_client.py` rotates them round-robin
and tracks each one's 5,000 req/hr budget separately.

## Raw Data

In `data/sources/github/` — one row per repo unless stated otherwise:

| File | Key columns | Written by | Read by |
|------|-------------|------------|---------|
| `repos.csv` | `repo, valid, repo_id, owner_login, owner_id, owner_type, description, homepage, canonical_url, default_branch, license, language, topics, stars, forks, open_issues, has_issues, archived, created_at, pushed_at, fetched_at` (32 cols) | `fetch_repo_owner_data.py`; `resolve_licenses.py` rewrites `license` in place | `src/common/repos.py` (the slug → `repo_id` map), `src/value/build_git_urls.py` + `build_validation.py` + `apply_ecosystems_authority.py`, `src/risk/build_workload.py`, `src/eligibility/build_{active,funding,licenses}.py`, `src/sources/funding/match_repos.py` |
| `users.csv` | `login, user_id, type, name, blog, company, location, email, bio, twitter_username, public_repos, followers, created_at, fetched_at` — keyed by owner login | `fetch_repo_owner_data.py` | no current reader — retained as the owner-profile cache |
| `issues.csv` | `repo, repo_id, year, metric, value, fetched_at` — long, per (repo, year) | `fetch_issue_metrics.py` | `src/risk/build_workload.py` |
| `funding-yml.csv` | `repo, repo_id, has_funding_links, has_funding_yml, funding_link_platforms, ` + one handle column per `FUNDING_PLATFORMS` entry (`github` … `custom`), `fetched_at` | `fetch_funding_yml.py` | `src/eligibility/build_funding.py`, `src/sources/opencollective/fetch_budgets.py` |
| `sponsors.csv` | `repo, repo_id, gh_sponsorships_in, gh_sponsors_enabled, sponsors_status, fetched_at` — inbound sponsorships of the repo owner | `fetch_sponsors.py` | `src/eligibility/build_funding.py` |
| `sponsorships.csv` | `login, sponsoring_count, sponsoring_status, fetched_at` — outbound sponsoring, keyed by owner login | `fetch_sponsorships.py` | `src/eligibility/build_funding.py` |
| `maintainer-sponsors.csv` | `user_id, login, has_sponsors_listing, status, fetched_at` — keyed by maintainer login | `fetch_maintainer_sponsors.py` | `src/eligibility/build_funding.py` |
| `canonical-repos.csv` | `repo, canonical_repo, repo_id, status, fetched_at` | `fetch_canonical.py` | `src/value/apply_ecosystems_authority.py` |
| `contributor-commits.csv` | `repo, repo_id, git_url, login, contributions, account_type` — long, the GitHub-API contributor method (login-keyed), distinct from the git-clone file of the same name under `data/sources/git/` | `fetch_contributors_metrics.py`, via `batch_runner.batch_update` | `bf_contributors.load_bf_contributors` → `src/eligibility/build_funding.py` |
| `contributor-commits.status.csv` | `repo, repo_id, git_url, status, n_contributors, fetched_at` — status ∈ `ok` / `empty` / `error`, so a repo with no rows is never confused with a failed fetch | `batch_runner.py` | the TTL gate of `fetch_contributors_metrics.py` |
| `search/top-repos.csv` | `repo, repo_id, user_name, user_id, user_type, license, language, topics, stars, forks, open_issues, archived, created_at, pushed_at, fetched_at` | `fetch_top_repos.py` | `fetch_top_repos.py` only — the search corpus is a self-upserted cache. Downstream stages read the wider `repos.csv` instead |
| `search/repo-counts.csv` | `language, min_stars, date_from, date_to, count, fetched_at` — cached search-API counts, so a repeat run skips range-building | `fetch_top_repos.py` | `fetch_top_repos.py` |

Three files in the folder are **stale artifacts** — no module in `src/`,
`scripts/`, or `tests/` reads or writes them. Treat them as unused:
`github-users.csv`, `repo-contrib-metrics.csv`, `repo-git-metrics.csv`.

All git-clone / git-analysis raw data lives under `data/sources/git/` (host-agnostic:
every row carries both `repo_id` — `gh/` or `gl/` — and the `git_url` it was cloned
from, so a fetcher clones the real host rather than assuming `github.com/{repo}`):
- `commits-years.csv` -- per (repo, year) `last_sha` + `commits` (foundation for sha-pinned snapshots)
- long-format sha-pinned (schema: `repo, repo_id, git_url, commit_sha, metric, value, checked_at`):
  - `scc.csv` -- scc metrics: `loc`, `sloc`, `files`, `uloc`, `complexity`, `complexity_density`
  - `lizard.csv` -- lizard cyclomatic + cognitive + Halstead + maintainability index + files
  - `openssf.csv` -- OpenSSF Scorecard `score` + 18 per-check scores + a `scan_error` marker
  - `depsdev.csv` -- deps.dev-mirrored Scorecard score + checks (fall-back when local row missing)
- `contributor-commits.csv` -- git-clone contributor method, long raw: `repo, repo_id, git_url,
  author_name, author_email, year, commits` (+ `contributor-commits.status.csv` per-repo status
  sidecar). Bus factor / HHI are computed downstream by `src/risk/build_concentration.py` from these rows
- `urls.csv` -- non-GitHub clone-URL validation cache (`url, valid, method, checked_at`;
  written by the value stage's `build_git_urls` / `build_validation`)

## Scripts

Every module in `src/sources/github/`:

| Script | Purpose |
|--------|---------|
| `fetch_top_repos.py` | Search repos by language/stars; backfill ecosystem repos → `search/` |
| `fetch_repo_owner_data.py` | `GET /repos/{owner}/{repo}` + `/users`\|`/orgs` for every class-A/B repo → `repos.csv` + `users.csv` |
| `fetch_canonical.py` | Resolve each value-scope repo to its current `nameWithOwner` + `databaseId`, so a rename never breaks a join → `canonical-repos.csv` |
| `resolve_licenses.py` | Second pass over repos GitHub reported as `NOASSERTION`/empty; writes the resolved SPDX id back into `repos.csv` |
| `fetch_issue_metrics.py` | Per (repo, year) opened / closed issue counts (→ workload) |
| `fetch_churn.py` | Bare clone + `git log --numstat` → 2021–2025 lines added/deleted → `data/sources/git/churn.csv` |
| `fetch_contributors_metrics.py` | Fetch `/repos/{repo}/contributors` → `contributor-commits.csv` + status sidecar. Lifetime aggregates only: `/stats/contributors` answers HTTP 202 "computing" indefinitely for most repos |
| `bf_contributors.py` | Read side of that file: the bus-factor maintainer set that cumulatively wrote ≥50% of a repo |
| `fetch_funding_yml.py` | Declared funding channels per repo via GraphQL: resolved `fundingLinks` + the raw FUNDING.yml → `funding-yml.csv` |
| `fetch_sponsors.py` | Inbound GitHub Sponsors counts for each repo owner → `sponsors.csv` |
| `fetch_sponsorships.py` | Outbound `sponsorshipsAsSponsor` counts per owner account → `sponsorships.csv` |
| `fetch_maintainer_sponsors.py` | Personal GitHub Sponsors listing per bus-factor maintainer (→ funding-intent `bf_maintainer_fundable`) |
| `github_client.py` | API client with token rotation + rate limiting |
| `batch_runner.py` | Async batch processing + atomic contributor-CSV upserts |
| `models.py` | Data types (Contributor, RunResult, bot detection) |
| `display.py` | Rich terminal output |

Clone-based fetchers that GitHub repos also depend on live in
[`src/sources/git/`](git.md): `commits_years.py` (per (repo, year) `first_sha`
/ `last_sha` / `commits`), `contributors.py` (git-clone contributor commits),
`fetch_scc.py`, and `fetch_sha_metrics.py` (one sparse checkout → scc + both
lizard passes).

### Running

Repo search, by language and minimum star count:

```bash
uv run python -m src.sources.github.fetch_top_repos --language Python --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language JavaScript TypeScript --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language Rust --min-stars 1000
uv run python -m src.sources.github.fetch_top_repos --language C "C++" --min-stars 1000
```

Backfill the ecosystem A/B repos the search missed:

```bash
uv run python -m src.sources.github.fetch_top_repos --backfill-only [--limit 20]
```

Repo + owner metadata, and the fetchers that build on it:

```bash
uv run python -m src.sources.github.fetch_repo_owner_data
uv run python -m src.sources.github.resolve_licenses
uv run python -m src.sources.github.fetch_canonical
uv run python -m src.sources.github.fetch_contributors_metrics
uv run python -m src.sources.github.fetch_issue_metrics
uv run python -m src.sources.github.fetch_funding_yml
uv run python -m src.sources.github.fetch_sponsors
uv run python -m src.sources.github.fetch_sponsorships
uv run python -m src.sources.github.fetch_maintainer_sponsors
uv run python -m src.sources.github.fetch_churn
```

Clone-based commands (scc, lizard, contributors, SHA anchors) are on
[git.md](git.md).
