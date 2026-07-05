# GitLab

Project metadata, owner/namespace data, and per-year commit SHAs for GitLab-hosted
open-source projects — across gitlab.com **and** self-hosted instances (salsa.debian.org,
invent.kde.org, gitlab.gnome.org, gitlab.freedesktop.org, code.videolan.org, …).

Mirrors the GitHub fetchers (`src/sources/github/`) so a GitLab-hosted repo can carry the
same identity, owner, and SHA-anchor signals a GitHub repo does. The GitLab API surface is
identical across instances (`/api/v4`), so multi-instance support is a per-host base URL +
per-host token.

> **Status:** collection layer (project/namespace/SHA-anchor) **plus** Scorecard GitLab mode
> (`src/sources/openssf/scorecard.py --gitlab`, see below). These outputs are produced but
> **not yet consumed** by the Value or Risk stages — wiring GitLab repos into `value.csv` /
> `risk.csv` (validation, `load_top_repos`, clone host-parametrization) is a follow-on plan, so
> the collected security scores don't yet aggregate into `risk.csv`. Coverage/funnel counts
> therefore live in `docs/stats.md` only once that wiring lands — this page describes **how**
> the data is fetched, not **how many**.

## Data Sources

**Projects API**: `GET https://{host}/api/v4/projects/{urlencoded_path}?license=true` — full
project metadata for a `namespace/path` (path URL-encoded, slashes → `%2F`; multi-level
`group/subgroup/project` supported). One call returns the repo **and** its owner kind
(`namespace.kind` = `group`/`user`). 301/302 rename redirects are followed to the terminal
response; the requested path stays the row key. A 404 is recorded as a sparse `valid=False`
row so re-runs honour the TTL instead of re-hammering dead projects.

**Namespaces API**: `GET https://{host}/api/v4/namespaces/{urlencoded_full_path}` — owner
metadata (group vs user). (Note: GitLab's Namespaces endpoint omits `description` for **user**
namespaces, so that column is blank for individual owners.)

**Commits API**: `GET /projects/{id}/repository/commits?ref_name={branch}&since={yr}-01-01&until={yr}-12-31&per_page=1`
— the newest commit in a calendar year (item `[0]` = the year's `last_sha`) plus the in-year
commit count (`X-Total` header). This is the SHA anchor sha-pinned analyses (scc/lizard/churn)
key off. (GitLab omits `X-Total` for result sets > 10,000, so `commits` may under-report in
that rare case; `last_sha` — the anchor — is unaffected.)

**Git metrics** (future): the clone-based fetchers (scc/lizard/contributors/churn/semgrep) are
host-agnostic and will run on GitLab clone URLs once `src/sources/git/clone.py` is
host-parametrized — a follow-on plan, not part of this layer.

**Authentication**: per-host tokens via the `PRIVATE-TOKEN` header. Resolution precedence per
host: `GITLAB_TOKENS` (JSON `{host: token}`) → `GITLAB_TOKEN_<HOST_SLUG>` (dots→underscores,
upper) → `GITLAB_TOKEN` (default applied to known hosts). Missing → anonymous for that host
(public read works at a lower rate limit). Note: the bare `GITLAB_TOKEN` / per-host
`GITLAB_TOKEN_<SLUG>` fallbacks only cover the curated `KNOWN_GITLAB_HOSTS`
(gitlab.com, salsa.debian.org, invent.kde.org, gitlab.gnome.org, gitlab.freedesktop.org);
a valid but non-curated host (e.g. `gitlab.cern.ch`) is only tokenised via an explicit
`GITLAB_TOKENS` JSON entry — otherwise Scorecard's GitLab mode silently skips it (its
checks 401 anonymously). Tokens are **per-instance** — a gitlab.com token does
not authenticate against salsa.debian.org. Rate limiting honours each host's
`RateLimit-Remaining` / `RateLimit-Reset` headers, with a minimum backoff floor and per-host
isolation (an exhausted host never blocks requests to another host).

## Identity

Unified `repo_id`, built by `gitlab_client.make_repo_id`: gitlab.com is the canonical instance
and gets a **bare `gl/{project_id}`** — e.g. `gl/278964` — parallel to GitHub's `gh/{id}` (both
default instances need no host qualifier). Every self-hosted instance is namespaced by its
**lowercased host**, joined with a hyphen so the id carries no path separator:
`gl/{host}-{project_id}` — e.g. `gl/salsa.debian.org-678`. Self-hosted needs the host because
each instance has an independent project-id space, so a bare `gl/{id}` would collide across
instances. The numeric `project_id` comes from that instance's Projects API.

## Raw Data

In `data/sources/gitlab/`:

- **`projects.csv`** — one row per GitLab project (mirrors `github/repos.csv`):
  `project` (= `host/namespace/path`, the key), `valid`, `project_id`, `repo_id`
  (= `gl/{host}-{project_id}`, bare `gl/{project_id}` for gitlab.com), `host`, `owner_type` (`Organization` if `namespace.kind==group`,
  else `User`), `namespace_kind` (raw `group`/`user`), `namespace_path`, `name`,
  `path_with_namespace`, `description`, `homepage` (`web_url`), `default_branch`, `license`
  (SPDX-ish key), `topics`, `stars`, `forks`, `open_issues`, `archived`, `visibility`,
  `created_at`, `last_activity_at`, `fetched_at`.
- **`namespaces.csv`** — owner/group metadata (mirrors `github/users.csv`):
  `namespace` (= `host/full_path`, the key), `namespace_id`, `host`, `kind`, `name`, `path`,
  `full_path`, `web_url`, `description`, `fetched_at`.
- **`commits-years.csv`** — the SHA anchor, keyed on `repo_id`:
  `repo_id`, `git_url`, `project`, `year`, `first_sha` (blank — anchors use `last_sha`),
  `last_sha`, `commits`, `fetched_at`. Standalone for now; a follow-on plan folds it into a
  shared `data/sources/git/commits-years.csv`.

Each fetcher records `fetched_at` and a success flag (`valid`) or a status sidecar, so a
genuinely-absent value is distinguishable from a failed fetch (auditability).

## Freshness

90-day TTL on `fetched_at` (via `src/common/freshness.py`), matching the GitHub owner fetcher.
A re-run inside the window is a no-op; `--force` bypasses it. 404 rows honour the same TTL.

## Scripts

- `src/sources/gitlab/gitlab_client.py` — multi-instance async client (host detection, per-host
  tokens, rate limiter).
- `src/sources/gitlab/fetch_project_data.py` — projects + namespaces →
  `projects.csv` / `namespaces.csv`. CLI: `--target {projects,namespaces,both}`, `--limit`, `--force`.
- `src/sources/gitlab/commits_years.py` — per-year SHA anchor → `commits-years.csv`.
  CLI: `--limit`, `--force`. Selection is per-`(repo_id, year)`, so a newly-added year is picked
  up for already-anchored projects.
- `src/sources/openssf/scorecard.py --gitlab [--host {host} …]` — OpenSSF Scorecard security
  scores for the valid GitLab projects in `projects.csv`, per-host `GITLAB_AUTH_TOKEN`. Uses a
  GitLab-applicable check subset (`GITLAB_SCORECARD_CHECKS`) and tolerates the CLI's non-zero
  exit when a single check errors (recovers the still-valid aggregate JSON). Output shares the
  GitHub scorecard's files: raw JSON in `data/sources/openssf/data.json`, long-format rows in
  `data/sources/git/openssf.csv` keyed on the `gl/{host}-{id}` repo_id.

## Related

The GitHub SHA anchor `data/sources/github/git/commits-years.csv` was also given the durable
`repo_id` (= `gh/{id}`) and `git_url` columns (see `src/sources/git/commits_years.py` — both its
writer and `resolve_head.py` emit them; `--backfill` rewrites the existing file), so both anchors
join on the same identity scheme.
