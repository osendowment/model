# deps.dev (+ Best-Practices badge)

Google's dependency-graph index ([api.deps.dev](https://api.deps.dev)) plus the
[bestpractices.dev](https://www.bestpractices.dev) badge API — free, no auth.
One fetcher covers every valid class-A repo (archived included,
`load_top_repos()`).

## Endpoints

| Service | Endpoint | Fields taken |
|---------|----------|--------------|
| deps.dev | `GET /v3/projects/github.com%2F{owner}%2F{repo}` | mirrored OpenSSF Scorecard (`overallScore`, `date`, scanned `commit` SHA, per-check scores), `license`, `openIssuesCount` |
| bestpractices.dev | `GET /projects.json?url=https://github.com/{owner}/{repo}` | first match's `badge_level`, `tiered_percentage` |

## Raw Data

`data/sources/depsdev/repos.csv` — wide, one row per repo, **not** sha-pinned:

| Column | Description | Example |
|--------|-------------|---------|
| `repo` | GitHub slug | `ajv-validator/ajv` |
| `repo_id` | stable id from `value.csv` (never invented) | `gh/35914020` |
| `depsdev_scorecard_overall` | mirrored Scorecard 0–10 (convenience copy; sha-pinned copy in the long file) | `5.3` |
| `depsdev_scorecard_date` | mirror's scan date | `2026-04-20` |
| `depsdev_license` | SPDX-ish license from the project payload | `MIT` |
| `depsdev_open_issues` | GitHub open-issue count as seen by deps.dev | `327` |
| `bestpractices_badge_id` | `in_progress` / `passing` / `silver` / `gold` / `""` (not enrolled) | `in_progress` |
| `bestpractices_tiered_percentage` | 0–300, summed across tiers | `99` |
| `fetched_at` | UTC timestamp of this run | `2026-05-03T19:09:22+00:00` |

`data/sources/git/depsdev.csv` — long, sha-pinned, shared
`src.sources.git.long_format` schema; upserted by (repo, sha, metric), so
prior-SHA snapshots are preserved:

| Column | Description | Example |
|--------|-------------|---------|
| `repo`, `repo_id`, `git_url` | repo identity | `acornjs/acorn`, `gh/5932749`, `https://github.com/acornjs/acorn.git` |
| `commit_sha` | `scorecard.repository.commit` — the SHA the mirror scanned | `3f40dbe6…` |
| `metric` | `score` + one snake_cased row per check (openssf naming); `-1` kept as upstream "not applicable" sentinel | `binary_artifacts` |
| `value` | overall/check score | `5` |
| `checked_at` | mirror's `scorecard.date` | `2026-04-20T00:00:00Z` |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/depsdev/fetch.py` | Fetch all four endpoints; write both CSVs |

```bash
uv run python -m src.sources.depsdev.fetch                  # full run (risk pipeline step)
uv run python -m src.sources.depsdev.fetch --limit 5 -v     # quick test
uv run python -m src.sources.depsdev.fetch --force          # ignore TTL
```

| Behavior | Value |
|----------|-------|
| TTL (`--ttl-days`) | 365 d — skips rows with recent `fetched_at`; timestamp-only gate (empty values are legit "no data") |
| deps.dev throttle | 20 workers (`--concurrency`), ≥50 ms gap per worker |
| bestpractices.dev throttle | global: 1 in-flight, 250 ms gap |
| Retries | 429/5xx, 4 attempts, exponential backoff |
| Persistence | flush to disk every 15 s |

## Consumers

`src.risk.build_security` uses the long file as the **fallback** Scorecard
source when the local openssf row is missing (`openssf_score_source =
"depsdev"`) and takes `bestpractices_badge_id` from the wide file — see
[components/security.md](../components/security.md) and [risk.md](../risk.md).
Coverage counts live in [stats.md](../stats.md).

## Caveats

| Caveat | Detail |
|--------|--------|
| 404 vs transport failure | not distinguished — both leave fields blank; `fetched_at` proves the repo was processed, but "not indexed / not enrolled" and "failed after retries" look identical |
| Wide scorecard columns | convenience copy only; the sha-pinned long file is the source of truth for the security build |
