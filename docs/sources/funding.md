# Funding (FOSS-foundation hosts)

This page documents the **`funding` source** — `src/sources/funding/` +
`data/sources/funding/`: per-foundation project rosters and the derived
`host-by-repo.csv` join. It is distinct from the eligibility funding
**component** ([components/funding.md](../components/funding.md)), which
consumes this source to build `intent` / `nonprofit` / `host_score`.
Per-foundation roster details live in [foundations.md](foundations.md).

## Scripts

One scraper per foundation writes `data/sources/funding/foundations/<full-name>.csv`;
`match_repos.py` joins them all against the repo corpus.

| Script | Host slug | Roster origin |
|---|---|---|
| `apache.py` | `apache` | projects.apache.org JSON API |
| `cncf.py` | `lf/cncf` | CNCF `landscape.yml` (rows with `maturity` only) |
| `eclipse.py` | `eclipse` | projects.eclipse.org API (paginated) |
| `lf.py` | `lf` | `jmertic/lf-landscape` `landscape.yml` |
| `numfocus.py` | `numfocus` | HTML scrape: listing + per-project pages |
| `openjs.py` | `lf/openjs` | Cross-Project Council README tables |
| `psf.py` | `psf` | GitHub org listings `pypa/*` + `python/*` |
| `sfc.py` | `sfc` | HTML scrape + **curated** GitHub-slug overlay |
| `fsf.py` | `fsf` | **fully hand-curated** (no scrapeable roster) |
| `gnu.py` | `fsf/gnu` | gnu.org blurbs scrape + **curated** mirror overlay |
| `gnome.py` | `gnome` | gitlab.gnome.org group API (GitHub mirror = `GNOME/<name>`) |
| `xorg.py` | `xorg` | gitlab.freedesktop.org/xorg group API |
| `match_repos.py` | — | joins rosters → `host-by-repo.csv` |

Qualified `parent/child` slugs encode foundation hierarchy (CNCF and OpenJS
live under the Linux Foundation; GNU is FSF-stewarded). On overlap the more
specific child wins over `lf`.

## How host-by-repo.csv is derived

`match_repos.py` classifies every repo in `data/sources/github/repos.csv`
(the full fetched set — a superset of the risk/eligibility scope). First
match wins:

| Priority | `host_source` | Rule |
|---|---|---|
| 1 | `project_list` | exact `owner/name` slug from a roster's `github_repo` |
| 2 | `org_prefix` | curated org → host map (`apache/*`, `pypa/*`→psf, `GNOME/*`, `gcc-mirror/*`→gnu, …) |
| 3 | `domain` | homepage apex vs roster domains, then suffix rules (`*.apache.org`, `*.gnu.org`, `x.org`, …) |
| — | *(empty)* | no tracked foundation hosts the repo |

Guards against over-attribution (all in `match_repos.py`):

| Guard | Keeps out |
|---|---|
| `GENERIC_DOMAINS` | platform domains (github.com, pypi.org, …) never index as foundation domains |
| `NON_HOSTING_DOMAINS` | cited ≠ hosted: `peps.python.org`, docs/registry URLs (`pypi.python.org`, `pypi.org`), spec hubs (`json-schema.org`) |
| suffix is `x.org` only, not `freedesktop.org` | wayland/dbus/pipewire are not X.Org |
| no org rule for `gitlab-freedesktop-mirrors`, `mirror/*`, `bminor/*` | mixed-content mirror orgs — exact roster slug only |
| FSFE excluded | Free Software Foundation *Europe* is a separate legal entity |

## Data

| File | Keyed by | Columns |
|---|---|---|
| `foundations/<full-name>.csv` | project | per-foundation (see [foundations.md](foundations.md)); all carry `name`, `github_repo`, `ecosystem`, `package`, `fetched_at`, and most a `domain` |
| `host-by-repo.csv` | repo | `repo, repo_id, host, host_source, host_checked` |

Example `host-by-repo.csv` row:
`apache/airflow, gh/33884891, apache, project_list, 2026-06-29T23:06:31+00:00`

Roster `ecosystem`/`package` (npm/pypi/crates/homebrew links found in project
metadata) are informational — the matcher joins by slug/org/domain only.

## Running

```bash
uv run python -m src.sources.funding.apache        # any scraper; --ttl N / --force
uv run python -m src.sources.funding.match_repos   # rebuild host-by-repo.csv
```

All 12 scrapers + the matcher run as fetch steps of
`uv run python -m src.eligibility.run_eligibility_pipeline`.

## Auditability

| Mechanism | Behavior |
|---|---|
| `fetched_at` | stamped on every roster row by `write_projects` (`_common.py`) |
| TTL gate | re-run inside `FUNDING_TTL_DAYS` (365) fetches nothing; `--force` / `--ttl 0` bypass |
| MIN-ROW GUARD | a scrape with <50% of existing rows cannot overwrite a good roster (`--force` to accept a real shrink) |
| `repo_id` | copied from `github/repos.csv` at match time — downstream joins are rename-proof |
| `host_checked` | the matched foundation's roster `fetched_at` (never the run time → re-runs are byte-identical); blank when unmatched |
| contract | `scripts/pipeline_health.py` enforces `repo_id` + `host_checked` on `host-by-repo.csv` |
| tests | `tests/test_funding_scrapers.py` covers stamp, guard, TTL gate, and the `classify` join |

## Consumers & caveats

`src/eligibility/build_funding.py` reads `host` by `repo_id`: a scraped host
defaults to `host_type=nonprofit` and feeds `intent`, `nonprofit`, and
`host_score` — a per-repo/org row in `data/eligibility/overrides.csv` wins
over the scraped value. See [components/funding.md](../components/funding.md).

- Institutional hosts outside these rosters (e.g. Xiph.Org) are curated in
  `data/eligibility/overrides.csv`, not here.
- Rosters may contain duplicate project rows; harmless — the matcher indexes
  into sets. Non-matching metadata columns (category, language, description)
  are informational only.
- Match coverage and per-host counts: the preview stats sheet.
