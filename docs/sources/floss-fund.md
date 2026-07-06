# FLOSS Fund (funding.json)

The [FLOSS Fund](https://dir.floss.fund) maintains a public directory of
`funding.json` manifests — a machine-readable standard through which a FOSS
project (or an entity owning many projects) declares its identity, projects,
and funding channels/plans. We ingest the whole directory as a
funding-**intent** signal for the eligibility stage: a repo registered here —
directly, or via its owner's org-level manifest — has a declared funding
channel (`has_funding_json`). Consumption details:
[funding](../components/funding.md); coverage: [stats](../stats.md).

## Data Source

**Export**: [dir.floss.fund/funding-manifests.tar.gz](https://dir.floss.fund/funding-manifests.tar.gz)
-- one tarball containing `funding-manifests.csv` (one row per registered
manifest, full JSON embedded). No authentication, zero per-repo requests.

## Raw Data

`data/sources/floss-fund/funding-json.csv` -- one row per **project** per
manifest (a manifest may list several projects; one with none keeps a single
row with blank project fields).

| Column(s) | Meaning | Example |
|-----------|---------|---------|
| `id`, `url`, `status` | directory manifest id, manifest URL, directory status | `1395`, `https://tukaani.org/funding.json`, `active` |
| `entity_*` | registrant: name, type, email, webpage | `Lasse Collin`, `individual` |
| `project_*` | name, guid, description, licenses, tags, webpage, repository | `XZ Utils`, `spdx:0BSD,spdx:GPL-2.0-or-later` |
| `project_repository_resolved` | final GitHub URL when the raw repo URL is a redirect | `tukaani.org/xz/redirect-to-github-xz` → `https://github.com/tukaani-project/xz` |
| `funding_channels`, `channel_platforms`, `funding_plans_count` | raw channel types, canonical platform names, plan count | `payment-provider,other`, `liberapay,other`, `1` |
| per-platform handles (`github` … `custom`) | one column per `FUNDING_PLATFORMS` entry: handle parsed from the channel address | `liberapay` = `larhzu` |
| `created_at`, `updated_at` | directory-side manifest metadata (NOT our fetch date) | `2025-02-02 20:22:57 … UTC` |
| `repo_id`, `fetched_at` | stamped by the fetcher: stable GitHub id + UTC run stamp | `gh/553665726`, `2026-06-29T23:37:14+00:00` |

## Scripts

| Script | Purpose |
|--------|---------|
| `src/sources/floss_fund/funding_json.py` | Download + flatten the directory, resolve redirects, stamp `repo_id`/`fetched_at` |
| `src/sources/floss_fund/directory.py` | Shared helpers: GitHub slug normalization, org-page detection, export loading |

```bash
uv run python -m src.sources.floss_fund.funding_json           # no-op within TTL
uv run python -m src.sources.floss_fund.funding_json --ttl 0   # force refresh
```

Freshness is a whole-file mtime check against the shared 365-day funding TTL
(`FUNDING_TTL_DAYS` in `src/common/freshness.py`) — within it, nothing is
downloaded and nothing is rewritten.

## Repo- and org-level manifests

How `src/eligibility/build_funding.py` matches each manifest shape:

| Shape | Detected by | Match |
|-------|-------------|-------|
| Repo-level: `project_repository` (resolved redirect preferred) names a GitHub repo | `export_repo_slug` | id-first on the stamped `repo_id` (rename-proof); blank-id rows fall back to canonical slug |
| Org-level: URL is a GitHub org page (`github.com/<org>`) | `github_org_page` | fundability + channels apply to every in-scope repo the org owns |

## Auditability

| Guarantee | Mechanism |
|-----------|-----------|
| Fetch date per row | `fetched_at` — one UTC stamp per run (file rewritten whole); `created_at`/`updated_at` are directory metadata |
| `repo_id` never invented | blank = org-level, non-GitHub, or unresolvable; ids already on disk seed each rewrite, so a resolved id survives directory refreshes |
| Redirect absence ≠ failure | last-good cache: a URL that once resolved is never blanked by a failed probe, so an empty `project_repository_resolved` means "never resolved to GitHub" |

## Caveats

| Caveat | Detail |
|--------|--------|
| Self-registered directory | presence = *declared* funding intent, not funding received |
| GitHub-only matching | only non-GitHub http(s) repo URLs get a redirect probe; projects hosted elsewhere (e.g. GitLab) keep a blank `repo_id` |
| `status` unfiltered | carried through as-is; loaders in `directory.py` / `build_funding.py` do not filter on it |
