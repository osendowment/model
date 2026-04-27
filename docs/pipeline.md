 # Data Sources by Pipeline

Maps every external data source we ingest to which pipeline(s) it feeds.
Cells list the **specific fields** we extract -- not the entire dataset.

Pipelines:

- **Value** — picks the most-depended-on packages per ecosystem and ranks
  them by download-weighted PageRank. See [docs/value.md](value.md).
- **Eligibility** — decides whether a repo qualifies for funding
  (open-source license + not end-of-life). See [docs/eligibility.md](eligibility.md).
- **Risk** — measures sustainability risk from contributor concentration
  and codebase complexity. See [docs/risk.md](risk.md).

| Source | Value | Eligibility | Risk |
|---|---|---|---|
| **npm registry** (`registry.npmjs.org`) | `downloads`; `dependencies` from each package's manifest | latest version's `deprecated` flag → `npm_deprecated` | — |
| **nice-registry** ([all-the-package-repos](https://github.com/nice-registry/all-the-package-repos)) | `package → repo_url` mapping | — | — |
| **BigQuery PyPI dataset** ([gcp-public-data-pypi](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi)) | per-package annual downloads (5 years) | — | — |
| **PyPI JSON API** (`pypi.org/pypi/<n>/json`) | `requires_dist`, `project_urls` | `Development Status :: 7 - Inactive` Trove classifier → `pypi_inactive` | — |
| **crates.io DB dump** (`static.crates.io/db-dump.tar.gz`) | crates, dependencies, `repository`, `homepage`, `description` | default version's `yanked` flag → `crates_yanked` | — |
| **crates.io archives** (`static.crates.io/archive/version-downloads/`) | monthly per-version download counts | — | — |
| **Debian UDD** (`udd.debian.org`) | C/C++ source list (debtags + section heuristics) | — | — |
| **Debian popcon** (via Wayback Machine) | install-base counts (proxy for downloads) | — | — |
| **Debian `Packages.xz`** (`deb.debian.org`) | binary→source mapping, deps, `homepage`, `vcs_browser`, `section` | — | — |
| **Homebrew formula API** (`formulae.brew.sh/api/formula.json`) | formula list, deps, `homepage`, `source_url`, `desc`, `license`, `language` | per-formula `disabled` / `deprecated` flags → `homebrew_disabled` / `homebrew_deprecated` | — |
| **Homebrew analytics** (via Wayback Machine) | 365-day install counts (proxy for downloads) | — | — |
| **Repology** (`repology.org`) | cross-ecosystem project-name canonicalisation; per-project info HTML scraped for upstream Git URLs | — | — |
| **OSS-Fuzz** ([github.com/google/oss-fuzz](https://github.com/google/oss-fuzz)) | C/C++ security-critical project whitelist; `main_repo` URL from `project.yaml` | — | — |
| **endoflife.date** (`endoflife.date/api/<product>.json`) | — | EOL cycle dates for ~20 well-known products (openssl, postgresql, python, ruby, php, ...) → `endoflife_date` overlay | — |
| **Foundation rosters** (Apache, CNCF, Eclipse, LF, NumFocus, OpenJS, PSF, SFC) | — | `repo → host` (foundation slug) via `data/foundations/host-by-repo.csv` | — |
| **FLOSS Fund manifests** (`dir.floss.fund/funding-manifests.tar.gz`) | — | `funding.json` entity, project metadata, `funding_channels` *(fetched, integration TBD)* | — |
| **GitHub Repos API** (`api.github.com/repos/<owner>/<repo>`) | — | `license`, `owner`, `valid_repo`, `repo_url` | — |
| **GitHub Users API** (`api.github.com/users/<login>`) | — | owner display name + `blog` URL → `repo_owner`, `repo_owner_url` | — |
| **GitHub Contributors stats API** (`api.github.com/repos/.../stats/contributors`) | — | — | per-contributor weekly commit history → bus factor, HHI |
| **GitHub git tree** (sparse checkout + [scc](https://github.com/boyter/scc)) | — | — | lines of code, complexity per language |
| **GitHub Issues Search API** (`api.github.com/search/issues`) | — | — | per-year issue open / close counts |
| **OpenSSF Scorecard API** (`api.securityscorecards.dev`) | — | — | security score (0–10) per repo |

## Dataflow at a glance

```
                Value pipeline                 Eligibility            Risk
                ───────────────                ───────────            ────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C/D
registries     (95% cum dl)    (BFS)       ↓
                                      value-data.csv
                                            │
                                            └─► github_repo
                                                    │
                                                    ├──────────────► repos.csv
                                                    │                    │
                                                    │              license + EOL
                                                    │                    │
                                                    │            eligibility-data.csv
                                                    │
                                                    └────────────────────────────► contributors + scc
                                                                                          │
                                                                                    risk-data.csv
```

Source-specific details live in [`docs/sources/`](sources/) (one `.md` per source).
