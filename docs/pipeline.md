 # Data Sources by Pipeline

Maps every external data source we ingest to which pipeline(s) it feeds.
Cells list the **specific fields** we extract -- not the entire dataset.

Pipelines (run in this order — each stage feeds the next):

1. **Value** (`src.pipeline.run_value_pipeline`) → `data/value/value.csv` — picks the
   most-depended-on packages per ecosystem and ranks them by
   download-weighted PageRank, then unifies per-package classes into one
   row per GitHub repo. All classes A/B/C/D are included. See [docs/value.md](value.md).
2. **Risk** (`src.pipeline.run_risk_pipeline`) → `data/risk/risk.csv` — concentration
   + complexity + issue-debt scoring for **A/B value-class repos** read
   directly from `data/value/value.csv`. Target classes are configured in
   `src/pipeline/settings.json` under `risk_input.value_classes` (default
   `["A", "B"]`). See [docs/risk.md](risk.md).
3. **Eligibility** (`src.pipeline.run_eligibility_pipeline`) → `data/eligibility/eligibility.csv`
   — restricts to AB-class repos with a fresh GitHub API record, an
   OSI-approved license, and a non-EOL signal. Runs after Risk; its
   intended scope (future work) is repos that are `value_class=A` AND
   highest risk class. See [docs/eligibility.md](eligibility.md).

## Funnel — current pipeline conversions

Counts and drop-rates as of the last full run. Re-run the three pipeline
stages and refresh this table when scope or thresholds change.

| # | Stage | Filter applied | Count | Δ from prev | % of input |
|---|---|---|---:|---:|---:|
| 0 | Package discovery (4 ecos) | all packages from npm/pypi/crates/cpp `results.csv` | 17,609 | — | 100.0% |
|   | ↳ npm | | 6,370 | | 36.2% |
|   | ↳ crates | | 6,218 | | 35.3% |
|   | ↳ pypi | | 3,139 | | 17.8% |
|   | ↳ cpp (Debian + Homebrew) | | 1,882 | | 10.7% |
| 1 | **AB-class packages** | `value_class ∈ {A, B}` (top ~75% of cumulative downloads × pagerank) | **1,628** | **−90.8%** | 9.2% |
| 2 | **Unique GitHub repos (AB)** | dedup packages → repos via `value-data.csv` | **917** | **−43.7%** | 5.2% |
|   | ↳ many-pkgs-per-repo collapse: babel/babel = ~140 npm pkgs, isaacs/glob ships under several names, etc. |  |  |  |  |
| 3 | AB ∩ fetched in `github/repos.csv` | repo has a GitHub API record (run `src.github.fetch_repo_owner_data`) | 892 | −2.7% | 5.1% |
| 4 | AB ∩ valid (not 404) | `valid=True` in `repos.csv` (repo still exists) | 892 | 0.0% | 5.1% |
| 5 | `is_oss=True` | strict OSI membership against `data/sources/osi/oss-licenses.csv` (handles SPDX expressions) | 875 | −1.9% | 5.0% |
| 6 | NOT `is_eol` | every constituent package alive on its registry | 868 | −0.8% | 4.9% |
| **7** | **Risk-scope (A/B)** | `value_class ∈ {A, B}` repos after dropping archived/invalid — input to the Risk pipeline (~224 A + ~676 B ≈ 900 repos; refresh by re-running the pipeline) | **~900** | — | — |
| **8** | **ELIGIBLE** | `valid_repo AND is_oss=True AND NOT is_eol` — runs after Risk; future scope narrows to `value_class=A` ∩ highest risk class | **868** | **0.0%** | **4.9%** |

### Key drop points

- **Stage 0 → 1 (−91%)**: the pareto cut. We deliberately keep only the top-of-pagerank packages in the funding scope. Everything else is fetched & classified for completeness but not eligible.
- **Stage 1 → 2 (−44%)**: monorepo collapse. `babel/babel`, `isaacs/*`, `python/*` etc. ship many AB-class packages from a single GitHub repo. This is normal — the funding decision is per-repo, not per-package.
- **Stage 2 → 3 (−3%)**: GitHub fetch coverage. Closes to ~0% after a refresh of `src.github.fetch_repo_owner_data`.
- **Stage 4 → 5 (−2%)**: license check. The few remaining are `noassertion` (cpp libs without Homebrew formula match) plus the genuine non-OSS rows (CC-BY data packages, MIT-CMU variant, etc.). See `docs/eligibility.md` for the breakdown.
- **Stage 5 → 6 (−1%)**: EOL — small absolute number (~7 archived projects).
- **Stage 7 (Risk-scope)**: Risk runs on A/B value-class repos directly from `value-data.csv`, skipping archived and invalid repos. Contributor + issue metrics aren't yet fetched for the full risk-scope set — running `src.github.fetch_contributors_metrics` + `src.github.fetch_issue_metrics` for all ~900 repos closes this gap.
- **Stage 8 (ELIGIBLE)**: Eligibility now runs after Risk. The 868 count reflects the last full eligibility run; re-run `src.pipeline.run_eligibility_pipeline` to refresh.

### How to refresh these numbers

```
uv run python -m src.pipeline.run_value_pipeline         # rebuilds value-data.csv
uv run python -m src.pipeline.run_risk_pipeline          # rebuilds risk-data.csv
uv run python -m src.pipeline.run_eligibility_pipeline   # rebuilds eligibility-data.csv
```

Then re-count and update the table.

| Source | Value | Risk | Eligibility |
|---|---|---|---|
| **npm registry** (`registry.npmjs.org`) | `downloads`; `dependencies` from each package's manifest | — | latest version's `deprecated` flag → `npm_deprecated` |
| **nice-registry** ([all-the-package-repos](https://github.com/nice-registry/all-the-package-repos)) | `package → repo_url` mapping | — | — |
| **BigQuery PyPI dataset** ([gcp-public-data-pypi](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi)) | per-package annual downloads (5 years) | — | — |
| **PyPI JSON API** (`pypi.org/pypi/<n>/json`) | `requires_dist`, `project_urls` | — | `Development Status :: 7 - Inactive` Trove classifier → `pypi_inactive` |
| **crates.io DB dump** (`static.crates.io/db-dump.tar.gz`) | crates, dependencies, `repository`, `homepage`, `description` | — | default version's `yanked` flag → `crates_yanked` |
| **crates.io archives** (`static.crates.io/archive/version-downloads/`) | monthly per-version download counts | — | — |
| **Debian UDD** (`udd.debian.org`) | C/C++ source list (debtags + section heuristics) | — | — |
| **Debian popcon** (via Wayback Machine) | install-base counts (proxy for downloads) | — | — |
| **Debian `Packages.xz`** (`deb.debian.org`) | binary→source mapping, deps, `homepage`, `vcs_browser`, `section` | — | — |
| **Homebrew formula API** (`formulae.brew.sh/api/formula.json`) | formula list, deps, `homepage`, `source_url`, `desc`, `license`, `language` | — | per-formula `disabled` / `deprecated` flags → `homebrew_disabled` / `homebrew_deprecated` |
| **Homebrew analytics** (via Wayback Machine) | 365-day install counts (proxy for downloads) | — | — |
| **Repology** (`repology.org`) | cross-ecosystem project-name canonicalisation; per-project info HTML scraped for upstream Git URLs | — | — |
| **OSS-Fuzz** ([github.com/google/oss-fuzz](https://github.com/google/oss-fuzz)) | C/C++ security-critical project whitelist; `main_repo` URL from `project.yaml` | — | — |
| **endoflife.date** (`endoflife.date/api/<product>.json`) | — | — | EOL cycle dates for ~20 well-known products (openssl, postgresql, python, ruby, php, ...) → `endoflife_date` overlay |
| **Foundation rosters** (Apache, CNCF, Eclipse, LF, NumFocus, OpenJS, PSF, SFC) | — | — | `repo → host` (foundation slug) via `data/sources/foundations/host-by-repo.csv` |
| **FLOSS Fund manifests** (`dir.floss.fund/funding-manifests.tar.gz`) | — | — | `funding.json` entity, project metadata, `funding_channels` *(fetched, integration TBD)* |
| **GitHub Repos API** (`api.github.com/repos/<owner>/<repo>`) | — | — | `license`, `owner`, `valid_repo`, `repo_url` |
| **GitHub Users API** (`api.github.com/users/<login>`) | — | — | owner display name + `blog` URL → `repo_owner`, `repo_owner_url` |
| **GitHub Contributors stats API** (`api.github.com/repos/.../stats/contributors`) | — | per-contributor weekly commit history → bus factor, HHI | — |
| **GitHub git tree** (sparse checkout + [scc](https://github.com/boyter/scc)) | — | lines of code, complexity per language → `data/sources/git/scc.csv` | — |
| **Lizard + multimetric** (sparse checkout) | — | per-function McCabe + Halstead + Sonar cognitive + maintainability index → `data/sources/git/lizard.csv` | — |
| **Semgrep** (sparse checkout, `p/default` rulepack) | — | SAST findings → `data/sources/git/semgrep.csv` | — |
| **GitHub Issues Search API** (`api.github.com/search/issues`) | — | per-year issue open / close counts | — |
| **OpenSSF Scorecard API** (`api.securityscorecards.dev`) | — | security score (0–10) per repo → `data/sources/git/openssf.csv` | — |
| **deps.dev API** (`api.deps.dev`) | — | mirrored Scorecard `score` + checks (fall-back) → `data/sources/git/depsdev.csv` | — |

## Long-format snapshot files (`data/sources/git/`)

All sha-pinned raw metrics share one canonical schema:

```
repo, repo_id, commit_sha, metric, value, checked_at
```

Key = `(repo, commit_sha, metric)`. New runs upsert by key — historical
snapshots for prior SHAs are preserved as a time-series. Empty `value` /
empty `commit_sha` rows are dropped. Floats are written in shortest
round-trip form (`42` not `42.0`, `8.5` not `8.500000000001`).

Files:

| File | Tool | Metrics |
|------|------|---------|
| `data/sources/git/scc.csv` | [scc](https://github.com/boyter/scc) | `loc`, `sloc`, `files`, `uloc`, `complexity`, `complexity_density` |
| `data/sources/git/lizard.csv` | [lizard](https://github.com/terryyin/lizard) + [multimetric](https://github.com/priv-kweihmann/multimetric) | `cyclomatic_*`, `halstead_*`, `cognitive_*`, `maintainability_index`, `files` |
| `data/sources/git/semgrep.csv` | [semgrep](https://semgrep.dev) | `<rulepack>.<metric>` (e.g. `p_default.total`, `p_default.error`) |
| `data/sources/git/openssf.csv` | [scorecard CLI](https://github.com/ossf/scorecard) | `score` + 18 individual checks (`maintained`, `code_review`, …) |
| `data/sources/git/depsdev.csv` | [deps.dev](https://api.deps.dev) | mirrored Scorecard `score` + checks |

The canonical writer/reader is `src/git/long_format.py` (`upsert_snapshot`,
`upsert_rows`, `read`, `project_to_wide`, `latest_sha_per_repo`).

### Sha-pinning convention

Each repo has per-year `last_sha` resolved by `src/git/commits_years.py`
into `data/sources/github/git/commits-years.csv`. Fetchers walk per-repo years
2025 → 2024 → … → 2021 and pick the most-recent year with `commits > 0`
and a non-empty `last_sha`. That sha is the `commit_sha` for every row
the fetcher writes. No HEAD fallback persists — if no usable year exists
for a repo, no row is written for it.

### High-level projection (long → wide)

The pipeline stages project the long files into per-repo wide rows for
downstream consumers:

- `data/risk/complexity.csv` ← `src.pipeline.risk.build_complexity` projects
  `data/sources/git/scc.csv` + `data/sources/git/lizard.csv` using
  `commits-years.last_sha` (2025 → 2021 walk; first sha with `loc > 0`).
  Also folds in the **hotspot** score (Tornhill `churn × complexity`):
  joins `data/sources/github/git/churn.csv` (`churn_5y_total`) with the EOY-2025
  scc complexity snapshot to emit `churn_5y_total`, `hotspot_raw`,
  `hotspot_log`, `hotspot_percentile`.
- `data/risk/security.csv` ← `src.pipeline.risk.build_security` projects
  `data/sources/git/openssf.csv`, `data/sources/git/depsdev.csv`, `data/sources/git/semgrep.csv`
  using the same per-year sha priority.
- `data/risk/risk.csv` ← `src.pipeline.run_risk_pipeline` joins complexity + security
  + concentration + issue-debt and computes the final risk score.

## Dataflow at a glance

```
                Value pipeline                    Risk                Eligibility
                ───────────────                   ────                ───────────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C/D
registries     (95% cum dl)    (BFS)       ↓
                                      value-data.csv
                                            │
                                            ├─► A/B class repos ──► contributors + scc
                                            │   (settings.json          │
                                            │    risk_input.            │
                                            │    value_classes)   risk-data.csv
                                            │
                                            └─► github_repo
                                                    │
                                                    ├──────────────► repos.csv
                                                    │                    │
                                                    │              license + EOL
                                                    │                    │
                                                    │            eligibility-data.csv
```

Source-specific details live in [`docs/sources/`](sources/) (one `.md` per source).
