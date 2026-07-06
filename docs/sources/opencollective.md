# Open Collective

Crowdfunding / fiscal-hosting platform where many OSS projects collect
donations. Two datasets are pulled: the **reverse map** (which GitHub
repo/org each collective declares it funds) and **annual gross budgets** per
collective. Both feed `src/eligibility/build_funding.py` — see
[funding.md](../components/funding.md) for slug attribution and scoring;
coverage counts are in the preview stats sheet.

## Data Source

**API**: [api.opencollective.com/graphql/v2](https://api.opencollective.com/graphql/v2)
(GraphQL, POST). Works unauthenticated but rate-limits hard (HTTP 429);
`OPENCOLLECTIVE_PERSONAL_TOKEN` in `.env` (sent as the `Personal-Token`
header) lifts the limit. A custom User-Agent is always sent (the default is
rejected).

| Query | What it fetches |
|-------|-----------------|
| Collectives index | Every `COLLECTIVE` account (paginated, 1000/page); keeps only those with a GitHub link |
| Budgets | Per slug: `stats.totalAmountReceived` (gross incoming) for each calendar year 2021–2025, one aliased query |

GitHub link priority: `repositoryUrl` → `GITHUB` social link → other social
links → `website`. Only `github.com` URLs count (a non-GitHub website is
skipped, never guessed at); search-result URLs (`?q=`) are rejected — a
filtered listing is not a profile/repo claim; reserved paths (`sponsors`,
`orgs`, …) are not owners.

Budget slugs are discovered from four sources unioned over class-A-scope
repos (FUNDING.yml handles, FLOSS Fund export, curated overrides, the
reverse map) — details in funding.md.

## Raw Data

Both files are keyed by **OC slug, not GitHub repo** — a documented
exemption from the repo_id schema contract (non-repo-keyed; absent from
`SOURCE_SCHEMA_CONTRACT` in `scripts/pipeline_health.py`). The repo join
happens downstream in `build_funding`.

`data/sources/opencollective/collectives.csv`:

| Column | Description | Example |
|--------|-------------|---------|
| `slug` | OC collective slug | `socketio` |
| `name` | display name | `socket.io` |
| `github_owner` | linked GitHub owner, lowercased | `socketio` |
| `github_repo` | `owner/repo` for repo-level links; empty for org-only links (`webpack` links only `github.com/webpack`) | `socketio/socket.io` |
| `github_url` | the URL the link was taken from | `https://github.com/socketio/socket.io` |
| `fetched_at` | UTC timestamp, one per run | `2026-06-29T23:55:44+00:00` |

Repo-level and org-only rows stay distinct because attribution differs:
full budget to the repo vs. split across the org's class-A repos.

`data/sources/opencollective/budgets.csv`:

| Column | Description | Example (`babel`) |
|--------|-------------|-------------------|
| `slug` | OC collective slug | `babel` |
| `raised_2021`…`raised_2025` | gross raised per calendar year, major units; blank = no data | `310955.86` |
| `currency` | collective's own settlement currency (not USD-normalized) | `USD` |
| `oc_status` | `ok` / `not_found` / `error` | `ok` |
| `fetched_at` | UTC fetch timestamp, per row | `2026-06-29T23:55:45+00:00` |

## Auditability

| Mechanism | Detail |
|-----------|--------|
| `oc_status` | separates a real zero from a failure: `ok` (account resolved), `not_found` (no such collective), `error` (request failed after retries) |
| Error rows | never fresh — retried next run, not cached as "no funding" |
| TTL (365 d, `src/common/freshness.py`) | budgets gated per row on `fetched_at` + `oc_status`; collectives index gated per file (mtime) |
| Rate limiting | 429s retried up to 6× honouring `Retry-After` (cap 90 s); default concurrency 2 |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/opencollective/fetch_collectives.py` | Download the OC ↔ GitHub reverse map |
| `src/sources/opencollective/fetch_budgets.py` | Fetch 5-year gross budgets per discovered slug |

```bash
uv run python -m src.sources.opencollective.fetch_collectives [--force]
uv run python -m src.sources.opencollective.fetch_budgets [--limit N] [--force] [--concurrency 2]
```

## Caveats

| Caveat | Detail |
|--------|--------|
| Currency mix | amounts are in each collective's own currency (`aio-libs` is EUR) — cross-slug comparisons ignore FX |
| Gross figures | `totalAmountReceived` is incoming donations before host/platform fees |
| Collisions | first slug wins when two collectives claim the same repo/org |
