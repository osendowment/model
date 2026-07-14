# OSV

[OSV.dev](https://osv.dev) — Google's open-source vulnerability database. The
risk pipeline's [security component](../components/security.md) uses it as the
source of per-repo CVE rows (published 2021–2025).

## Fetch mechanics

| Aspect | Behaviour |
|---|---|
| Endpoint | `POST https://api.osv.dev/v1/query` with `{"package": {"name", "ecosystem"}}`; no auth |
| Pagination | ≤1000 vulns/page; follows `next_page_token` |
| Rate limiting | ~1 req/s per worker; 429/5xx retried with backoff 1s → 8s |
| Dedupe | identity = `{id} ∪ aliases[]`; canonical id = lex-smallest member; date = earliest `published` |
| Window | keeps CVEs published 2021–2025 |
| TTL | repo skipped while its sidecar row is < `--ttl-days` (default 365) old |
| Re-fetch | replaces that repo's `cves.csv` rows wholesale (per-repo upsert); flushes every 15s |

## Query strategy

OSV does not index `pkg:github/*` purls, so each top repo (valid class-A set)
is queried by every `(ecosystem, package)` tuple mapped to it:

| Mapping source | OSV ecosystem |
|---|---|
| `data/sources/npm/results.csv` | `npm` |
| `data/sources/pypi/results.csv` | `PyPI` |
| `data/sources/crates/results.csv` | `crates.io` |
| `data/sources/cpp/results.csv` | `Debian` — binary package name, no release suffix (aggregates all releases) |
| `data/risk/cve-package-overrides.csv` | curated — **replaces** a listed repo's package list wholesale |

GitLab repos are covered: ecosystem rows with an empty `github_repo` but a
`gl/` `repo_id` map to the GitLab repo's canonical slug. The overrides file
fixes package-name mismaps (e.g. `python/cpython` → the per-version Debian
`python3.x` packages) and adds repos absent from every `results.csv`.

## Raw Data

`data/sources/osv/cves.csv` — one row per (repo × CVE):

| Column | Description | Example |
|---|---|---|
| `repo` | canonical repo slug | `ai/nanoid` |
| `repo_id` | stable repo id (`gh/…` or `gl/…`) | `gh/99401299` |
| `date` | CVE `published` date (UTC, ISO) | `2022-01-21` |
| `cve` | canonical id (lex-smallest of `{id} ∪ aliases`) | `CVE-2021-23566` |

`data/sources/osv/queried.csv` — sidecar of successfully scanned repos:

| Column | Description | Example |
|---|---|---|
| `repo` | canonical repo slug | `7rulnik/source-map-js` |
| `repo_id` | stable repo id | `gh/330043541` |
| `packages_queried` | `eco:pkg` list actually queried | `npm:source-map-js` |
| `fetched_at` | UTC fetch timestamp | `2026-07-05T16:32:21+00:00` |

Auditability — the sidecar separates true zeros from missing fetches
(failed lookups never stamp the sidecar, so re-runs retry them):

| State | `cves.csv` | `queried.csv` |
|---|---|---|
| Scanned, ≥1 CVE | rows present | row with `fetched_at` |
| Scanned, 0 CVEs | no rows | row with `fetched_at` |
| Never fetched / fetch failed | no rows | no row |

The security build resolves this three-way: count / confirmed `0` / blank.

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/osv/fetch_cves.py` | Fetch + dedupe CVEs per top repo |

```bash
uv run python -m src.sources.osv.fetch_cves                         # full run
uv run python -m src.sources.osv.fetch_cves --limit 20 -v           # quick test
uv run python -m src.sources.osv.fetch_cves --force                 # ignore TTL
uv run python -m src.sources.osv.fetch_cves --repos python/cpython  # targeted, bypasses TTL
```

## Caveats

| Caveat | Effect |
|---|---|
| CVE mapping is package-name-bound | mismapped repos under-count; known cases are fixed via the overrides file; a residual `0` means "no mapped CVEs", not "no vulnerabilities" — see [security.md](../components/security.md#limitations) |
| Repo has no package mapping | skipped entirely — appears in neither output file; coverage in the preview pipeline sheet |
