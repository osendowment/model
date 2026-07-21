# GitLab

Project metadata, owner/namespace data, and per-year commit SHAs for GitLab-hosted
open-source projects — across gitlab.com **and** self-hosted instances (salsa.debian.org,
invent.kde.org, gitlab.gnome.org, gitlab.freedesktop.org, code.videolan.org, …).

These fetchers mirror `src/sources/github/`, so a GitLab-hosted repo carries the same identity,
owner, and SHA-anchor signals a GitHub repo does. The `/api/v4` surface is identical across
instances, so multi-instance support is one base URL and one token per host.

GitLab is a first-class platform in the pipeline scope (`src/settings.json`
`top_repos.platforms = ["github", "gitlab"]`). GitLab rows carry through `value.csv`,
`src.common.repos.load_top_repos`, and all four risk dimensions into `risk.csv`. The
clone-based fetchers (sha-metrics = scc + lizard, contributors) and Scorecard's GitLab mode
(`scorecard.py --gitlab`) run against GitLab hosts. Coverage counts live in the preview
pipeline sheet — this page describes **how** the data is fetched, not **how many**.

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

**Commits API**: `GET /projects/{id}/repository/commits?ref_name={branch}&since={yr}-01-01&until={yr}-12-31&per_page=100`
— the year's SHA anchor for the sha-pinned analyses (scc/lizard), plus its commit count.
GitLab serves **no `X-Total` header** on this endpoint — only `X-Page` / `X-Next-Page` — so the
fetcher walks every page newest-first and sums the row counts: page 1 item `[0]` is `last_sha`,
the final page's last item is `first_sha`, and `commits` is the total rows seen. A page error
aborts that year and writes **no** row; a truncated count is worse than a missing one, and the
next run simply refetches. A genuinely commit-less year writes `commits=0`, so it is never
refetched forever.

**Git metrics**: the clone-based fetchers (sha-metrics=scc+lizard, contributors) are
host-agnostic — each clone is routed through the repo's real `git_url` (the `repo_url`
override in `src/sources/git/clone.py`), so they run on GitLab clone URLs and key their
rows on the `gl/…` repo_id.

**Authentication**: per-host tokens via the `PRIVATE-TOKEN` header. Resolution precedence per
host: `GITLAB_TOKENS` (JSON `{host: token}`) → `GITLAB_TOKEN_<HOST_SLUG>` (dots→underscores,
upper) → `GITLAB_TOKEN` (default applied to known hosts). Missing → anonymous for that host
(public read works at a lower rate limit). Tokens are **per-instance** — a gitlab.com token does
not authenticate against salsa.debian.org.

The bare `GITLAB_TOKEN` / per-host `GITLAB_TOKEN_<SLUG>` fallbacks cover only the 17
`KNOWN_GITLAB_HOSTS` (= the keys of `HOST_NICKNAMES`, listed under [Identity](#identity)).
Host *detection* additionally accepts any `gitlab.*` hostname, but such a host is tokenised
only through an explicit `GITLAB_TOKENS` JSON entry.

A tokenless host is still scored: self-hosted instances serve their REST API anonymously, so
Scorecard's GitLab mode scores them token-free (a few auth-only checks come back inconclusive).
Only gitlab.com is skipped without a token — its anonymous quota is too small for Scorecard's
call volume (`SCORECARD_ANON_UNRELIABLE`). Rate limiting honours each host's
`RateLimit-Remaining` / `RateLimit-Reset` headers, with a minimum backoff floor and per-host
isolation (an exhausted host never blocks another host).

## Identity

Unified `repo_id`, built by `gitlab_client.make_repo_id`. gitlab.com is the canonical instance
and gets a **bare `gl/{project_id}`** — e.g. `gl/278964` — parallel to GitHub's `gh/{id}`. Every
self-hosted instance is namespaced by its **host nickname**, joined with a hyphen so the id
carries no path separator: `gl/{nickname}-{project_id}` — e.g. `gl/debian-678`. The qualifier is
required because each instance has an independent project-id space. The numeric `project_id`
comes from that instance's Projects API.

`HOST_NICKNAMES` in `src/sources/gitlab/gitlab_client.py` is the full list — 17 hosts, and
also the exact membership of `KNOWN_GITLAB_HOSTS`. A host outside it never gets an id:
`make_repo_id` raises until the host is added.

| Host | Nickname | Host | Nickname |
|---|---|---|---|
| `gitlab.com` | *(bare `gl/{id}`)* | `gitlab.inria.fr` | `inria` |
| `salsa.debian.org` | `debian` | `gitlab.isc.org` | `isc` |
| `invent.kde.org` | `kde` | `gitlab.cern.ch` | `cern` |
| `gitlab.gnome.org` | `gnome` | `gitlab.dkrz.de` | `dkrz` |
| `gitlab.freedesktop.org` | `freedesktop` | `gitlab.kitware.com` | `kitware` |
| `code.videolan.org` | `videolan` | `gitlab.exherbo.org` | `exherbo` |
| `gitlab.xiph.org` | `xiph` | `gitlab.xfce.org` | `xfce` |
| `gitlab.redox-os.org` | `redox` | `gitlab.linss.com` | `linss` |
| `gitlab.ibr.cs.tu-bs.de` | `tubs` | | |

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
- **`issues.csv`** — long per (repo, year) issue counts (mirrors `github/issues.csv`):
  `repo, repo_id, year, metric, value, fetched_at`. Read by `src/risk/build_workload.py`.
- **`funding-files.csv`** — in-repo funding declarations: `repo, repo_id, project,
  has_funding_yml, funding_yml_path, has_funding_links, funding_link_platforms,
  has_funding_json_file, status, fetched_at`. Read by `src/eligibility/build_funding.py`.
- SHA anchors land in the **shared** `data/sources/git/commits-years.csv` beside the GitHub
  rows (same unified schema: `repo, repo_id, git_url, year, first_sha, last_sha, commits,
  fetched_at`), upserted by `(repo, repo_id, year)` so a GitHub mirror sharing a slug keeps
  its own `gh/…` row.
- **`commits-years.csv`** (`repo_id, git_url, project, year, first_sha, last_sha, commits,
  fetched_at`) — a **stale artefact**, not a live output. No module in `src/` writes or reads
  it; `commits_years.py` writes the shared `data/sources/git/commits-years.csv` instead. Treat
  it as unused and expect it to be deleted.

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
- `src/sources/gitlab/fetch_issue_metrics.py` — per (repo, year) opened / closed issue counts →
  `data/sources/gitlab/issues.csv` (the GitLab twin of `github/issues.csv`; `build_workload`
  reads both). CLI: `--limit`, `--force`.
- `src/sources/gitlab/fetch_funding_files.py` — in-repo funding declarations →
  `data/sources/gitlab/funding-files.csv`. Probes each GitLab top repo's default branch
  (public raw endpoint, no token) for FUNDING.yml / `.github/FUNDING.yml` /
  `.gitlab/FUNDING.yml` (parsed to platform keys), `funding.json`, and
  `.well-known/funding-manifest-urls`. The GitLab twin of `github/funding-yml.csv`;
  `build_funding` joins it by `repo_id`. Rows carry `status` + `fetched_at`
  (empty results recheck on the short funding TTL). CLI: `--limit`, `--force`.
  A separate curated mapping, `data/eligibility/gitlab-hosts.csv`, marks a repo hosted
  on an institution's own GitLab instance (salsa.debian.org, gitlab.gnome.org, …) as
  institutionally host-backed → `intent` (gitlab.com deliberately maps to nothing).
- `src.sources.openssf.scorecard --gitlab [--host {host} …]` — OpenSSF Scorecard security
  scores for the valid GitLab projects in `repos.csv`. Each subprocess receives its host's token
  as `GITLAB_AUTH_TOKEN` and runs the GitLab-applicable check subset
  (`GITLAB_SCORECARD_CHECKS`). It tolerates the CLI's non-zero exit when a single check errors,
  recovering the still-valid aggregate JSON. Output shares the GitHub scorecard's files: raw JSON
  in `data/sources/openssf/data.json`, long-format rows in `data/sources/git/openssf.csv`, keyed
  on the `gl/{nickname}-{id}` repo_id.

```bash
uv run python -m src.sources.gitlab.fetch_project_data --target both
uv run python -m src.sources.gitlab.commits_years --limit 10
uv run python -m src.sources.gitlab.fetch_issue_metrics
uv run python -m src.sources.gitlab.fetch_funding_files
uv run python -m src.sources.openssf.scorecard --gitlab
```
