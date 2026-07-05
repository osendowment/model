# OpenSSF (Scorecard + Criticality Score)

Two independent OpenSSF metrics, fetched by two independent tools:

- **Scorecard** — *security posture*, 0–10 ([securityscorecards.dev](https://securityscorecards.dev/)). Feeds the risk stage's security dimension.
- **Criticality Score** — *importance/criticality*, 0–1 ([ossf/criticality_score](https://github.com/ossf/criticality_score)). Joined onto `value.csv` as the `criticality` column ([value.md](../value.md#unified-value-table)).

Both are GitHub-only (each tool collects exclusively from the GitHub API) and require a GitHub token (`GITHUB_AUTH_TOKEN` / `GITHUB_TOKEN`, round-robin via `GITHUB_TOKENS`).

## Scorecard

**Tool**: `scorecard` CLI (install via `brew install scorecard`). Runs security checks against a GitHub repo and returns a 0-10 score.

### Raw Data

- `data/sources/openssf/data.json` -- full scorecard API response per repo (13.7 MB; canonical raw cache)
- `data/sources/git/openssf.csv` -- long-format sha-pinned snapshot (`repo, repo_id, commit_sha, metric, value, checked_at`); one row per check + one for `score`

## Criticality Score

**Tool**: `criticality_score` Go binary
(`go install github.com/ossf/criticality_score/v2/cmd/criticality_score@latest`).
Computes the Rob Pike `original_pike` blend of GitHub signals: project age,
recency, contributor/org counts, commit/release cadence, issue activity, and a
dependents proxy.

We run it with `-depsdev-disable` (no GCP/BigQuery needed), so the dependents
proxy is `github_mention_count` via GitHub's `search/commits` — whose strict
secondary rate limit dictates the fetcher design: one repo per invocation,
throttled, with per-repo backoff-retry. The `depsdev_enabled` column records
the mode per row.

**Scope**: every valid class-A GitHub repo, **archived included** (`load_top_slugs(skip_archived=False)`) — the `value.csv` `openssf_crit` column is gated non-empty over exactly this set. TTL 365 days; error rows always retry. An idempotent healing pass runs per invocation: it backfills `repo_id`s that were unresolvable at fetch time and drops rows superseded by a repo rename (old-slug row vs new-slug row for the same id).

### Raw Data

- `data/sources/openssf/criticality.csv` -- wide, one row per repo: `criticality_score` + every raw signal, `checked_at`, `status` (`ok`/`error`), `error_reason`

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/openssf/scorecard.py` | Run scorecard for repos, upsert results |
| `src/sources/openssf/criticality.py` | Batch-run criticality_score; TTL + healing; upsert results |

```bash
uv run src/sources/openssf/scorecard.py owner/repo [owner/repo2 ...]
uv run src/sources/openssf/scorecard.py --file repos.txt [--concurrency 5]
uv run python -m src.sources.openssf.criticality               # full scope, incremental
uv run python -m src.sources.openssf.criticality owner/repo    # specific repos
```
