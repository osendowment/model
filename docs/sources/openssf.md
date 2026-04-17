# OpenSSF Scorecard

Security scoring for open-source repositories via the [OpenSSF Scorecard](https://securityscorecards.dev/) project.

## Data Source

**Tool**: `scorecard` CLI (install via `brew install scorecard`). Runs security checks against a GitHub repo and returns a 0-10 score.

Requires a GitHub token (`GITHUB_AUTH_TOKEN` or `GITHUB_TOKEN` env var).

## Raw Data

- `data/openssf/data.json` -- full scorecard API response per repo (13.7 MB)
- `data/openssf/scores.csv` -- repo, score, checked_at (~1.7K repos)

## Scripts

| Script | Purpose |
|--------|---------|
| `src/openssf/scorecard.py` | Run scorecard for repos, upsert results |

```bash
uv run src/openssf/scorecard.py owner/repo [owner/repo2 ...]
uv run src/openssf/scorecard.py --file repos.txt [--concurrency 5]
```
