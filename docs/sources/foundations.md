# FOSS Foundations

Project rosters of the major FOSS foundations and fiscal hosts: which projects
each foundation legally stewards. One scraper per foundation in
`src/sources/funding/` writes one CSV under `data/sources/funding/foundations/`.

**Scope split with the funding source**: this page covers the rosters only.
Joining them to repos — `match_repos.py`, the slug/org-prefix/domain priority
rules, and `host-by-repo.csv` — is documented in [funding.md](funding.md).
Downstream, the matched `host` feeds `nonprofit` and `host_score` in the
eligibility funding dimension ([components/funding.md](../components/funding.md));
coverage numbers live in the preview pipeline sheet.

## Rosters

The **Host** column is the `host` value `match_repos.py` emits. Three rosters
carry a `parent/child` slug because their foundation has a parent
(`PARENT_FOUNDATION` in `match_repos.py`): CNCF and OpenJS live under the
Linux Foundation, GNU under the FSF. On overlap the child wins over `lf`.

| Host | Roster file | Source | Method |
|------|-------------|--------|--------|
| `apache` | `apache-software-foundation.csv` | [projects.apache.org](https://projects.apache.org/json/foundation/projects.json) | JSON fetch |
| `lf/cncf` | `cloud-native-computing-foundation.csv` | `cncf/landscape` `landscape.yml` | YAML fetch; keeps only entries with `maturity` set (real CNCF projects, not landscape-only vendors) |
| `eclipse` | `eclipse-foundation.csv` | [projects.eclipse.org/api/projects](https://projects.eclipse.org/api/projects) | paginated JSON |
| `fsf` | `free-software-foundation.csv` | — | **hand-curated** list in `fsf.py` (no network) |
| `gnome` | `gnome.csv` | gitlab.gnome.org group API | paginated JSON; `github_repo` = the `GNOME/<name>` GitHub mirror slug |
| `fsf/gnu` | `gnu-project.csv` | [gnu.org/manual/blurbs.html](https://www.gnu.org/manual/blurbs.html) | HTML scrape + curated dict of verified GitHub mirrors |
| `lf` | `linux-foundation.csv` | `jmertic/lf-landscape` `landscape.yml` | YAML fetch; skips the "LF Members" category (dues-paying companies, not hosted projects) |
| `numfocus` | `numfocus.csv` | numfocus.org listing + per-project pages | two-step HTML scrape |
| `lf/openjs` | `openjs-foundation.csv` | `openjs-foundation/cross-project-council` README | markdown-table parse, tagged by stage section |
| `psf` | `python-software-foundation.csv` | GitHub orgs `pypa` + `python` | GitHub API (token revolver); forks skipped |
| `sfc` | `software-freedom-conservancy.csv` | sfconservancy.org/projects/current | HTML scrape + curated GitHub-slug-by-domain overlay |
| `xorg` | `x-org-foundation.csv` | gitlab.freedesktop.org `xorg` group API | paginated JSON; `github_repo` = `gitlab-freedesktop-mirrors/<name>` mirror slug |

## Running

```bash
uv run python -m src.sources.funding.<slug>   # e.g. …funding.apache
```

| Flag | Effect |
|------|--------|
| `--ttl N` | Skip the fetch if the roster is younger than N days (default `FUNDING_TTL_DAYS` = 365; `0` forces) |
| `--force` | Ignore the TTL **and** bypass the min-row guard |

## Schema

Columns every roster carries (per `src/sources/funding/_common.py`):

| Column | Meaning | Example |
|--------|---------|---------|
| `name` | Project name as the foundation lists it | `Apache Accumulo` |
| `github_repo` | `owner/name` GitHub slug, best-effort; blank when no authoritative repo/mirror exists | `aeraki-mesh/aeraki` |
| `ecosystem`, `package` | Package-registry URL (npm/pypi/crates/Homebrew→`cpp`) detected on the project's pages | `pypi`, `abi3audit` |
| `fetched_at` | UTC ISO stamp of the scrape, set by `write_projects` | `2026-06-29T23:07:14+00:00` |

`domain` — apex domain of the project homepage (www-stripped), the third
match key — appears on **most** rosters, not all. The `psf` roster derives its
projects from GitHub org listings and writes a `homepage` column but no
`domain` column, so PSF projects match by slug or org prefix only.

Per-roster extras carried through from each source:

| Roster | Extra columns |
|--------|---------------|
| apache | `shortname`, `homepage`, `repo_url`, `category`, `programming_language`, `created`, `license`, `description` |
| cncf / lf | `homepage_url`, `repo_url`, `category`, `subcategory`, `accepted`, `description` (+ `maturity` for cncf, `license` for lf) |
| eclipse | `project_id`, `homepage`, `state`, `license`, `description` |
| openjs | `stage`, `homepage`, `description` |
| psf | `homepage`, `default_branch`, `license`, `language`, `topics`, `stars`, `archived`, `created_at`, `pushed_at`, `description`, `category` (`pypa`/`python`) |
| fsf / gnome / gnu / sfc / xorg / numfocus | `project_slug`, `website` (+ `description` except numfocus) |

## Keying & auditability

| Mechanism | behavior |
|-----------|-----------|
| Row key | The foundation's own project identity (`name` / `project_slug` / `project_id`) — **not** a GitHub repo. `github_repo` is an attribute; repo ids attach downstream in `host-by-repo.csv` |
| `fetched_at` | Stamped on every row at write time — each host match is traceable to the scrape that produced it |
| Min-row guard | A refresh producing < 50% of the existing row count is refused (a blocked/partial scrape never clobbers a good file); `--force` overrides |
| TTL gate | A re-run inside the TTL window fetches nothing and rewrites nothing |
| Atomic write | Temp file + `os.replace` — no partially-written CSVs |

## Caveats

| Roster | Caveat |
|--------|--------|
| fsf | Purely hand-curated: the FSF publishes no scrapeable repo-level project list, and the Free Software Directory is a catalog (not funding) — deliberately excluded. FSF-funded software flows through the GNU roster; `github.com/FSFE` (FSF Europe) is a separate entity, never attributed |
| gnu | `github_repo` comes only from a curated dict of authoritative mirrors (`gcc-mirror/gcc`, `coreutils/coreutils`, …) — never guessed; blank rows match by the `gnu.org` domain instead |
| sfc | Most members develop off GitHub (kernel.org, GitLab, self-hosted); the curated by-domain overlay covers only members whose official home/mirror is GitHub |
| cncf, openjs | Both live under the Linux Foundation, so their projects also appear in the LF landscape — the matcher prefers the more specific slug ([funding.md](funding.md)) |
| numfocus, sfc, gnu | Fetched with a browser-like User-Agent (numfocus and sfc block the default client UA) |
| apache | Some rows carry `programming_language` split character-wise (`J \| a \| v \| a`) when the upstream field is a string rather than a list |
