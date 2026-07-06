# GitLab

Project metadata, owner/namespace data, and per-year commit SHAs for GitLab-hosted
open-source projects — across gitlab.com **and** self-hosted instances (salsa.debian.org,
invent.kde.org, gitlab.gnome.org, gitlab.freedesktop.org, code.videolan.org, …).

Mirrors the GitHub fetchers (`src/sources/github/`) so a GitLab-hosted repo can carry the
same identity, owner, and SHA-anchor signals a GitHub repo does. The GitLab API surface is
identical across instances (`/api/v4`), so multi-instance support is a per-host base URL +
per-host token.

> **Status:** fully wired in. GitLab is a first-class platform in the pipeline scope
> (`src/settings.json` `top_repos.platforms = ["github", "gitlab"]`): GitLab rows carry
> through `value.csv`, `src.common.repos.load_top_repos`, and all four risk dimensions into
> `risk.csv`. The clone-based fetchers (sha-metrics = scc + lizard, contributors) and
> Scorecard's GitLab mode (`src/sources/openssf/scorecard.py --gitlab`, see below) run
> against GitLab hosts. Coverage/funnel counts live in [stats.md](../stats.md) — this page
> describes **how** the data is fetched, not **how many**.

## Data Sources

**Projects API**: `GET https://{host}/api/v4/projects/{urlencoded_path}?license=true` — full
project metadata for a `namespace/path` (path URL-encoded, slashes → `%2F`; multi-level
`group/subgroup/project` supported). One call returns the repo **and** its owner kind
(`namespace.kind` = `group`/`user`). 301/302 rename redirects are followed to the terminal
response; the requested path stays the row key. A 404 is recorded as a sparse `valid=False`
row so re-runs honour the TTL instead of re-hammering dead projects.

**Languages API**: `GET https://{host}/api/v4/projects/{id}/languages` — the linguist-style
byte-share breakdown (`{"C": 90.8, "CMake": 3.7, …}`). Fetched best-effort by numeric `id`
(rename/redirect-proof) after each project's `200`; the top-share key becomes the scalar
`language` column (mirroring `github/repos.csv`). Any failure or an empty breakdown → blank
`language`; the row still carries `valid`+`fetched_at`, so a blank never masks a failed fetch.

**Namespaces API**: `GET https://{host}/api/v4/namespaces/{urlencoded_full_path}` — owner
metadata (group vs user). (Note: GitLab's Namespaces endpoint omits `description` for **user**
namespaces, so that column is blank for individual owners.)

**Commits API**: `GET /projects/{id}/repository/commits?ref_name={branch}&since={yr}-01-01&until={yr}-12-31&per_page=1`
— the newest commit in a calendar year (item `[0]` = the year's `last_sha`) plus the in-year
commit count (`X-Total` header). This is the SHA anchor sha-pinned analyses (scc/lizard/churn)
key off. (GitLab omits `X-Total` for result sets > 10,000, so `commits` may under-report in
that rare case; `last_sha` — the anchor — is unaffected.)

**Git metrics**: the clone-based fetchers (sha-metrics=scc+lizard, contributors) are
host-agnostic — each clone is routed through the repo's real `git_url` (the `repo_url`
override in `src/sources/git/clone.py`), so they run on GitLab clone URLs and key their
rows on the `gl/…` repo_id.

**Authentication**: per-host tokens via the `PRIVATE-TOKEN` header. Resolution precedence per
host: `GITLAB_TOKENS` (JSON `{host: token}`) → `GITLAB_TOKEN_<HOST_SLUG>` (dots→underscores,
upper) → `GITLAB_TOKEN` (default applied to known hosts). Missing → anonymous for that host
(public read works at a lower rate limit). Note: the bare `GITLAB_TOKEN` / per-host
`GITLAB_TOKEN_<SLUG>` fallbacks only cover the curated `KNOWN_GITLAB_HOSTS`
(gitlab.com, salsa.debian.org, invent.kde.org, code.videolan.org); any other host
(host detection also accepts any `gitlab.*` hostname) is only tokenised via an explicit
`GITLAB_TOKENS` JSON entry. A tokenless host is still scored: self-hosted instances serve
their REST API anonymously, so Scorecard's GitLab mode scores them token-free (a few
auth-only checks come back inconclusive), and only gitlab.com is skipped without a token —
its anonymous quota is too small for Scorecard's call volume (`SCORECARD_ANON_UNRELIABLE`).
Tokens are **per-instance** — a gitlab.com token does
not authenticate against salsa.debian.org. Rate limiting honours each host's
`RateLimit-Remaining` / `RateLimit-Reset` headers, with a minimum backoff floor and per-host
isolation (an exhausted host never blocks requests to another host).

## Identity

Unified `repo_id`, built by `gitlab_client.make_repo_id`: gitlab.com is the canonical instance
and gets a **bare `gl/{project_id}`** — e.g. `gl/278964` — parallel to GitHub's `gh/{id}` (both
default instances need no host qualifier). Every self-hosted instance is namespaced by its
**host nickname** — the short alias from `HOST_NICKNAMES` in
`src/sources/gitlab/gitlab_client.py` (e.g. `salsa.debian.org` → `debian`,
`invent.kde.org` → `kde`) — joined with a hyphen so the id carries no path separator:
`gl/{nickname}-{project_id}` — e.g. `gl/debian-678`. Self-hosted needs the qualifier because
each instance has an independent project-id space, so a bare `gl/{id}` would collide across
instances. A host without a nickname never gets an id — `make_repo_id` raises until the host
is added to `HOST_NICKNAMES`. The numeric `project_id` comes from that instance's Projects API.

## Raw Data

In `data/sources/gitlab/`:

- **`repos.csv`** — one row per GitLab project (mirrors `github/repos.csv`):
  `project` (= `host/namespace/path`, the key), `valid`, `project_id`, `repo_id`
  (= `gl/{nickname}-{project_id}`, bare `gl/{project_id}` for gitlab.com), `host`, `owner_type` (`Organization` if `namespace.kind==group`,
  else `User`), `namespace_kind` (raw `group`/`user`), `namespace_path`, `name`,
  `path_with_namespace`, `description`, `homepage` (`web_url`), `default_branch`, `license`
  (SPDX-ish key), `language` (primary, from the Languages API), `topics`, `stars`, `forks`,
  `open_issues`, `archived`, `visibility`, `created_at`, `last_activity_at`, `fetched_at`.
- **`namespaces.csv`** — owner/group metadata (mirrors `github/users.csv`):
  `namespace` (= `host/full_path`, the key), `namespace_id`, `host`, `kind`, `name`, `path`,
  `full_path`, `web_url`, `description`, `fetched_at`.
- SHA anchors land in the **shared** `data/sources/git/commits-years.csv` beside the GitHub
  rows (same unified schema: `repo, repo_id, git_url, year, first_sha, last_sha, commits,
  fetched_at`), upserted by `(repo, repo_id, year)` so a GitHub mirror sharing a slug keeps
  its own `gh/…` row.

Each fetcher records `fetched_at` and a success flag (`valid`) or a status sidecar, so a
genuinely-absent value is distinguishable from a failed fetch (auditability).

## Freshness

90-day TTL on `fetched_at` (via `src/common/freshness.py`), matching the GitHub owner fetcher.
A re-run inside the window is a no-op; `--force` bypasses it. 404 rows honour the same TTL.

## Scripts

- `src/sources/gitlab/gitlab_client.py` — multi-instance async client (host detection, per-host
  tokens, rate limiter).
- `src/sources/gitlab/fetch_project_data.py` — projects + namespaces →
  `repos.csv` / `namespaces.csv`. CLI: `--target {projects,namespaces,both}`, `--limit`, `--force`.
- `src/sources/gitlab/commits_years.py` — per-year SHA anchor → the shared
  `data/sources/git/commits-years.csv`. Scope: the GitLab members of `load_top_repos()`
  (risk scope). CLI: `--limit`, `--force`. Selection is per-`(repo_id, year)`, so a
  newly-added year is picked up for already-anchored projects.
- `src/sources/openssf/scorecard.py --gitlab [--host {host} …]` — OpenSSF Scorecard security
  scores for the valid GitLab projects in `repos.csv`, per-host `GITLAB_AUTH_TOKEN`. Uses a
  GitLab-applicable check subset (`GITLAB_SCORECARD_CHECKS`) and tolerates the CLI's non-zero
  exit when a single check errors (recovers the still-valid aggregate JSON). Output shares the
  GitHub scorecard's files: raw JSON in `data/sources/openssf/data.json`, long-format rows in
  `data/sources/git/openssf.csv` keyed on the `gl/{nickname}-{id}` repo_id.

## Related

The GitHub SHA anchor `data/sources/git/commits-years.csv` was also given the durable
`repo_id` (= `gh/{id}`) and `git_url` columns (see `src/sources/git/commits_years.py` — both its
writer and `resolve_head.py` emit them; `--backfill` rewrites the existing file), so both anchors
join on the same identity scheme.
