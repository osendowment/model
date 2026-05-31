# Value Pipeline

Identifies the most important open-source packages across ecosystems by combining
download volume with dependency graph analysis (PageRank).

## Pipeline overview

The three pipeline stages (run in order, each feeds the next):

```
1. **Value** (`src.value.run_value_pipeline`) → `data/value/value.csv` — picks the
   most-depended-on packages per ecosystem and ranks them by
   download-weighted PageRank, then unifies per-package classes into one
   row per GitHub repo. All classes A/B/C/D are included. (this doc)
2. **Risk** (`src.risk.run_risk_pipeline`) → `data/risk/risk.csv` — concentration
   + complexity + issue-debt scoring for **A/B value-class repos** read
   directly from `data/value/value.csv`. Target classes are configured in
   `src/settings.json` under `risk_input.value_classes` (default
   `["A", "B"]`). See [docs/risk.md](risk.md).
3. **Eligibility** (`src.eligibility.run_eligibility_pipeline`) → `data/eligibility/eligibility.csv`
   — restricts to AB-class repos with a fresh GitHub API record, an
   OSI-approved license, and a non-EOL signal. Runs after Risk. See [docs/eligibility.md](eligibility.md).
```

### Dataflow at a glance

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

## Metrics Roadmap

Inputs per dimension, current as of the last pipeline run. Each leaf = one metric, with its data
source(s) and the time period it represents. Sources differ per ecosystem
(npm / pypi / crates / cpp) and are listed inline.

> **Note:** `[2025 EOY]` means *as of the last commit to the default (main)
> branch in 2025* — not the calendar year-end snapshot. `[most recent]` means
> the latest available pull of that source.

```
Value
│
├── JavaScript / TypeScript (npm)
│   ├── downloads_2021..2025      ← api.npmjs.org/downloads             [2021–2025]
│   ├── avg_downloads             ← derived (mean over populated years) [2021–2025]
│   ├── avg_downloads_share       ← derived (pkg / ecosystem total)     [2021–2025]
│   ├── top                       ← derived (95% cum-download cutoff)   [2021–2025]
│   ├── dep edges (package→dep)   ← registry.npmjs.org                  [most recent]
│   ├── pagerank                  ← derived (DL-weighted PR, α=0.85)    [2021–2025]
│   ├── value_class               ← derived (A/B/C/D, cum-PR share)     [2021–2025]
│   └── package→repo              ← nice-registry                       [most recent]
│
├── Python (PyPI)
│   ├── downloads_2021..2025      ← BigQuery PyPI dataset               [2021–2025]
│   ├── avg_downloads             ← derived                             [2021–2025]
│   ├── avg_downloads_share       ← derived                             [2021–2025]
│   ├── top                       ← derived (95% cum-download cutoff)   [2021–2025]
│   ├── dep edges (package→dep)   ← pypi.org/pypi/{p}/json              [most recent]
│   ├── pagerank                  ← derived                             [2021–2025]
│   ├── value_class               ← derived                             [2021–2025]
│   └── package→repo              ← BigQuery github mapping             [most recent]
│
├── Rust (crates.io)
│   ├── downloads_2021..2025      ← crates.io daily archives            [2021–2025]
│   ├── avg_downloads             ← derived                             [2021–2025]
│   ├── avg_downloads_share       ← derived                             [2021–2025]
│   ├── top                       ← derived (95% cum-download cutoff)   [2021–2025]
│   ├── dep edges (package→dep)   ← crates.io DB-dump dependencies      [most recent]
│   ├── pagerank                  ← derived                             [2021–2025]
│   ├── value_class               ← derived                             [2021–2025]
│   └── package→repo              ← DB-dump `repository` field          [most recent]
│
├── C / C++ (Debian + Homebrew + Repology)
│   ├── debian_avg_downloads      ← Debian popcon (Wayback snapshots)   [2021–2025]
│   ├── homebrew_avg_downloads    ← Homebrew analytics (Wayback)        [2021–2025]
│   ├── downloads_score           ← derived (debian + homebrew composite)[2021–2025]
│   ├── dep edges (package→dep)   ← Debian Packages.xz (Depends/Pre-)   [most recent]
│   │                                + Homebrew formula.json (runtime)  [most recent]
│   ├── pagerank                  ← derived                             [2021–2025]
│   ├── value_class               ← derived                             [2021–2025]
│   └── package→repo              ← Repology project URLs               [most recent]
│
└── Cross-ecosystem rollup → value/value.csv
    ├── id                        ← derived (rank by top_eco_pct desc)    [2021–2025]
    ├── github_repo               ← per-eco package→repo union            [most recent]
    ├── gh_repo_id                ← GitHub Repos API (numeric repo id)     [most recent]
    ├── git_url                   ← per-eco git.csv union                  [most recent]
    │                                (GitLab/Codeberg/Sourcehut/Bitbucket
    │                                 /custom hosts when no GH match)
    ├── valid                     ← build_validation (True/False/empty)    [most recent]
    │                                (rollup of GitHub API + git ls-remote
    │                                 caches → value/validation.csv)
    ├── ecosystems                ← derived (eco set per repo)             [2021–2025]
    ├── packages                  ← derived (package count per repo)       [2021–2025]
    ├── top_eco                   ← derived (best percentile eco)          [2021–2025]
    ├── top_eco_pkg               ← derived (highest-PR pkg in top_eco)    [2021–2025]
    ├── top_eco_pct               ← derived (100 − pr_cum_pct, 0–100)      [2021–2025]
    ├── class_{npm,pypi,crates,cpp}
    │                              ← derived (per-eco cum-PR share)        [2021–2025]
    └── class                     ← derived (strongest across ecos)        [2021–2025]
```

## How It Works

1. **Top packages** -- select packages covering 95% of ecosystem-wide cumulative downloads
2. **Dependency tree** -- follow transitive dependencies from top packages
3. **Scoring** -- download-weighted PageRank over the dep graph, then classify A/B/C/D

### Top Package Selection

Packages sorted by avg annual downloads (descending). Walking down the list,
accumulate downloads until the running total reaches 95% of the **ecosystem-wide
total** (from the `downloads_<year>` rows of `data/value/stats.csv`). Every package above that cutoff is "top".

### PageRank

Directed dependency graph where A->B means "A depends on B". Personalized PageRank
with alpha=0.85, biased toward high-download packages.

### Value Classes

Packages sorted by PageRank descending. Cumulative PageRank share determines class:

| Class | Cumulative Share | Meaning |
|-------|-----------------|---------|
| **A** | 0--50% | Critical infrastructure |
| **B** | 50--75% | Important, widely depended on |
| **C** | 75--90% | Useful but not load-bearing |
| **D** | 90--100% | Long tail |

> See [Current Limitations](#current-limitations) for known scope gaps
> (cpp runtime-only deps, GitHub-only project identity, etc.).

### Funnel

Packages remaining after each pipeline stage, plus the share with a known
upstream repo at the end. *GH %* counts only github.com; *Git %* also
counts gitlab, bitbucket, sourcehut, codeberg, and `custom` hosts
(savannah, sourceware, kernel.org, etc.) -- see [Unified output](#unified-output).

| Ecosystem | Top packages | After dep tree | Results | With GitHub | GH % | With Git | Git % |
|-----------|-------------:|---------------:|--------:|------------:|-----:|---------:|------:|
| npm       | 5,765  | 6,370  | 6,370  | 6,281  | 99% | 6,281  | 99% |
| PyPI      | 2,460  | 3,139  | 3,139  | 1,728  | 55% | 1,728  | 55% |
| crates.io | 3,719  | 6,218  | 6,218  | 5,967  | 96% | 6,130  | 99% |
| C/C++     | 1,643  | 2,648  | 1,882  | 482    | 26% | 770    | 41% |
| **Total** | **13,587** | **18,375** | **17,609** | **14,458** | **82%** | **14,909** | **85%** |

*Top packages* covers 95% of cumulative downloads. *After dep tree* is
`|top ∪ transitive deps|` -- the universe analysed for PageRank. *Results*
keeps every node from that universe (top packages with no edges still get
a row, with PageRank = 0). C/C++'s `Results` is smaller than `After dep
tree` because the cpp pipeline applies an `is_cpp` filter — language-agnostic
distro packages get dropped. PyPI is stuck at 55% because the BigQuery
extract was github-only at fetch time. C/C++ jumps from 26% GitHub to 41%
Git because non-GitHub upstreams (sourceware, savannah, gitlab.gnome.org,
etc.) now resolve via per-eco `git.csv`.

### Value class distribution

Per-ecosystem and combined counts of A/B/C/D classes in `value-data.csv`.

| Ecosystem | A | B | C | D | Total | A+B GH | A+B Git |
|-----------|--:|--:|--:|--:|------:|-------:|--------:|
| npm       | 331 | 748   | 1,183 | 4,108  | 6,370 | 100% | 100% |
| PyPI      | 54  | 157   | 414   | 2,514  | 3,139 | 76%  | 76%  |
| crates.io | 49  | 197   | 449   | 5,523  | 6,218 | 99%  | 100% |
| C/C++     | 10  | 82    | 291   | 1,499  | 1,882 | 32%  | 95%  |
| **Total** | **444** | **1,184** | **2,337** | **13,644** | **17,609** | **93%** | **96%** |

*A+B GH* and *A+B Git* are the share of A and B class packages with a
known GitHub repo and any Git URL respectively — the load-bearing subset
that the Risk pipeline (default scope: A/B) and the Eligibility pipeline
both rely on. C/D-class rows are present in `value-data.csv` and tracked
through the value pipeline, but are outside the default Risk and
Eligibility scope. C/C++'s A+B Git jumps from 32% to 95% once
non-GitHub upstreams are counted (glibc, gcc, libunistring, glib, mpfr,
etc. live on sourceware / savannah / gitlab hosts, not GitHub).
Non-GitHub upstreams are concentrated in C/D classes, so the A+B
numbers barely move.

## Ecosystems

### npm

**Data sources:**
- [npm downloads API](https://api.npmjs.org/downloads/point) -- downloads per package
- [npm registry](https://registry.npmjs.org) -- dependencies
- [nice-registry](https://github.com/nice-registry/all-the-package-repos) -- package-to-repo mapping

**Raw data** (`data/sources/npm/raw/`):
- `downloads.csv` -- package, year, downloads
- `dependencies.csv` -- package, dep_name, dep_version, fetched_at

**Scripts:**

| Script | Purpose |
|--------|---------|
| `src/sources/npm/fetch_npm_data.py` | Iterative crawler (downloads + deps) |
| `src/sources/npm/fetch_npm_stats.py` | Ecosystem-wide annual totals |
| `src/sources/npm/fetch_nice_registry.py` | Package-to-repo mappings |
| `src/sources/npm/process_data.py` | Build outputs |

See [sources/npm.md](sources/npm.md) for details.

### PyPI

**Data sources:**
- [BigQuery PyPI dataset](https://console.cloud.google.com/marketplace/product/gcp-public-data-pypi/pypi) -- downloads (manual export, ~$235)
- [PyPI JSON API](https://pypi.org/pypi/{package}/json) -- dependencies

**Raw data** (`data/sources/pypi/`):
- `bigquery/bq-package-downloads.csv` -- ~849K packages x 5 years
- `raw/package-dependencies.csv` -- package, dependency, type, fetched_at
- `raw/package-github-mapping.csv` -- package-to-GitHub URL

**Scripts:**

| Script | Purpose |
|--------|---------|
| `src/sources/pypi/fetch_pypi_data.py` | Iterative dep crawler |
| `src/sources/pypi/process_data.py` | Build outputs |

See [sources/pypi.md](sources/pypi.md) for details.

### crates.io

**Data sources:**
- [crates.io DB dump](https://static.crates.io/db-dump.tar.gz) -- crate/version mappings + deps
- [crates.io daily archives](https://static.crates.io/archive/version-downloads/) -- per-version download counts

**Raw data** (`data/sources/crates/`):
- `db-dump/` -- crates.csv, versions.csv, default_versions.csv, dependencies.csv
- `version-downloads/YYYY-MM.csv` -- monthly per-version totals

**Scripts:**

| Script | Purpose |
|--------|---------|
| `src/sources/crates/fetch_db_dump.py` | Download + extract DB dump |
| `src/sources/crates/fetch_version_downloads.py` | Download monthly archives |
| `src/sources/crates/process_data.py` | Build outputs (~20s) |

See [sources/crates.md](sources/crates.md) for details.

### Debian

**Data sources:**
- [Debian UDD](https://udd.debian.org) -- C/C++ package list (debtags + section heuristics)
- [Debian popcon](https://popcon.debian.org) -- install counts (via Wayback Machine)
- [Packages.xz](https://deb.debian.org/debian/dists/stable/main/binary-amd64/Packages.xz) -- dependencies + metadata

**Raw data** (`data/sources/debian/raw/`):
- `cpp-packages.csv` -- C/C++ package universe (from UDD debtags)
- `downloads.csv` -- package, year, downloads (from popcon snapshots)
- `dependencies.csv` -- package, dep_name, dep_version, fetched_at
- `package-metadata.csv` -- package, source, homepage, vcs_browser, section
- `aliases.csv` -- t64 transition name mappings

**Scripts:**

| Script | Purpose |
|--------|---------|
| `src/sources/debian/fetch_debian_data.py` | Fetch packages, popcon, deps |
| `src/sources/debian/process_data.py` | Build outputs |

**Limitations:**
- **Popcon is opt-in** -- only ~250K Debian machines participate, so numbers
  are a sample of installed base, not total downloads
- **Sparse Wayback coverage** -- only 11 snapshots available across 2021--2025,
  with some years having only 1 snapshot (2023: Jul 2 only). Currently each
  year picks the snapshot closest to Dec 31, but this is a rough proxy
- **Not comparable to package manager downloads** -- popcon measures "machines
  with package installed", not download events

Available Wayback snapshots for `popcon.debian.org/by_inst.gz` (2021--2025):
```
2021: Sep 28, Oct 22
2022: Sep 04, Sep 21, Sep 29, Dec 25
2023: Jul 02
2024: Jan 06, Dec 31
2025: Sep 15, Nov 17
```

### Homebrew

**Data sources:**
- [Homebrew formula API](https://formulae.brew.sh/api/formula.json) -- formula list + deps
- [Homebrew analytics](https://formulae.brew.sh/api/analytics/install/365d.json) -- 365-day rolling
  install counts (via Wayback Machine)

**Raw data** (`data/sources/homebrew/raw/`):
- `formulas.csv` -- name, desc, homepage, source_url, license, tap, language
- `dependencies.csv` -- formula, dep_name, dep_type, fetched_at
- `downloads.csv` -- formula, year, downloads

**Scripts:**

| Script | Purpose |
|--------|---------|
| `src/sources/homebrew/fetch_homebrew_data.py` | Fetch formulas, analytics |
| `src/sources/homebrew/process_data.py` | Build outputs |

**Limitations:**
- **Opt-in analytics** -- users can disable with `brew analytics off`; numbers
  are a fraction of actual installs
- **Rolling 365-day windows** -- each snapshot represents "installs in the 365
  days ending at snapshot date", not a calendar year. A May 2023 snapshot is
  used as proxy for "2022" but actually covers Jun 2022 -- May 2023
- **Sparse + truncated snapshots** -- Wayback coverage is thin; some captures
  are truncated at exactly 1 MB (only the high-install head is recoverable
  via regex parsing). No usable 2021 snapshot exists
- **Not comparable to npm/PyPI/crates** -- represents macOS install events,
  not cross-platform package downloads

Available Wayback snapshots for `analytics/install/365d.json` (2023--2026):
```
2023: May 09, May 31, Sep 30
2024: May 22, Oct 07
2025: Jan 21, Apr 27, Sep 11, Dec 05
2026: Mar 06
```
No snapshots before 2023. No `install-on-request` snapshots before Sep 2022.

### Other C/C++ sources

- [Repology](https://repology.org) -- cross-ecosystem name mapping
- [OSS-Fuzz](https://github.com/google/oss-fuzz) -- fuzz testing coverage

**Additional raw data:**
- `data/sources/repology/packages.csv` -- project name mappings
- `data/sources/ossfuzz/projects.csv` -- C/C++ fuzz targets

See [sources/cpp.md](sources/cpp.md), [sources/debian.md](sources/debian.md), [sources/homebrew.md](sources/homebrew.md), [sources/repology.md](sources/repology.md) for details.

## Value data sources

Compact source → extracted-fields reference for the Value stage; per-ecosystem mechanics are detailed in [Ecosystems](#ecosystems) above.

| Source | Fields extracted for Value |
|---|---|
| **npm registry** (`registry.npmjs.org`) | `downloads`; `dependencies` from each package's manifest |
| **nice-registry** ([all-the-package-repos](https://github.com/nice-registry/all-the-package-repos)) | `package → repo_url` mapping |
| **BigQuery PyPI dataset** | per-package annual downloads (5 years) |
| **PyPI JSON API** (`pypi.org/pypi/<n>/json`) | `requires_dist`, `project_urls` |
| **crates.io DB dump** (`static.crates.io/db-dump.tar.gz`) | crates, dependencies, `repository`, `homepage`, `description` |
| **crates.io archives** | monthly per-version download counts |
| **Debian UDD** (`udd.debian.org`) | C/C++ source list (debtags + section heuristics) |
| **Debian popcon** (via Wayback Machine) | install-base counts (proxy for downloads) |
| **Debian `Packages.xz`** | binary→source mapping, deps, `homepage`, `vcs_browser`, `section` |
| **Homebrew formula API** (`formulae.brew.sh`) | formula list, deps, `homepage`, `source_url`, `desc`, `license`, `language` |
| **Homebrew analytics** (via Wayback Machine) | 365-day install counts (proxy for downloads) |
| **Repology** (`repology.org`) | cross-ecosystem name canonicalisation; upstream Git URLs |
| **OSS-Fuzz** | C/C++ security-critical project whitelist; `main_repo` URL from `project.yaml` |

## Output Files

Each ecosystem produces four files in `data/sources/{ecosystem}/`:

### top-packages.csv

Packages covering 95% of ecosystem downloads.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `avg_downloads` | Average annual downloads (2021--2025) |
| `avg_downloads_share` | Fraction of ecosystem-wide total downloads |
| `2021`--`2025` | Downloads per year |

### dependency-tree.csv

Transitive dependency edges from top packages.

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | `declared` (npm/pypi), `normal`/`build`/`dev` (crates) |

### github-repos.csv

Package-to-GitHub-repo mappings for all dep-tree packages.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | `owner/repo` slug |

### results.csv

All dep-tree packages with downloads, PageRank, and value class.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `github_repo` | `owner/repo` slug |
| `avg_downloads` | Average annual downloads |
| `2021`--`2025` | Downloads per year |
| `top` | `True` if package is in the 95% cumulative set |
| `pagerank` | Download-weighted PageRank score |
| `value_class` | A/B/C/D (see [Value Classes](#value-classes)) |

## Unified output

`data/value/value.csv` is the canonical per-repo table — one row per GitHub
repo, plus one row per orphan package (no `github_repo`) so nothing is
dropped. **All classes A/B/C/D are included** — D-class rows are no longer
dropped. Produced by `uv run python -m src.value.run_value_pipeline`, which reads each
ecosystem's `results.csv` and `eol.csv`, groups packages by repo, computes
all per-ecosystem and cross-ecosystem aggregates, and writes the file
sorted by `top_eco_pct` desc (most important repos first).

Per-ecosystem class is computed by summing the group's package PR within
the ecosystem, ranking groups by that sum desc, and applying the same
A/B/C/D cumulative-share cutoffs as the package-level pipeline (≤50%,
≤75%, ≤90%, rest). The strongest across ecosystems becomes `class`.
This avoids comparing PR magnitudes across ecosystems (each ecosystem's
PR mass sums to 1 within its own graph). Recomputing PageRank on a
repo-level dep graph was considered and skipped because cross-ecosystem
deps don't exist in our data; the repo graph is still four disconnected
subgraphs.

| Column | Description |
|--------|-------------|
| `id` | Sequential numeric id (sorted by `top_eco_pct` desc) |
| `github_repo` | Lowercase `owner/repo` slug; empty for orphans |
| `git_url` | Canonical git URL — GitHub when available, otherwise GitLab / Codeberg / Sourcehut / Bitbucket / custom (sourceware.org, savannah, gitlab.gnome.org, etc.). First non-empty value from per-ecosystem `data/sources/{eco}/git.csv`, picked in priority order. Empty when none of the per-eco files have a git URL for any constituent package. |
| `ecosystems` | Comma-separated list of ecosystems where the repo has packages (e.g. `crates,npm`) |
| `packages` | Total package count in the repo |
| `top_eco` | Ecosystem where the repo is highest-ranked (max PR percentile). `npm` / `pypi` / `crates` / `cpp`. |
| `top_eco_pkg` | Highest-PR package in `top_eco` (e.g. `@babel/helper-plugin-utils` for babel/babel) |
| `top_eco_pct` | PR percentile in `top_eco` (`100 − pr_cum_pct`). 0–100, **higher = better**. babel/babel = 92.25; tail near 0. |
| `class` | Strongest of the per-ecosystem classes (A < B < C < D) |
| `class_npm`, `class_pypi`, `class_crates`, `class_cpp` | A/B/C/D from per-ecosystem cumulative PR share; empty if no package in that ecosystem |

### Repo class distribution

After grouping packages by `github_repo` (or as orphans), `value-data.csv`
collapses 17,609 package rows into 12,842 repo rows.

| | npm | PyPI | crates.io | C/C++ | Strongest |
|---|---:|---:|---:|---:|---:|
| A | 144 | 53 | 31 | 10 | **238** |
| B | 430 | 151 | 102 | 81 | **763** |
| C | 769 | 389 | 256 | 291 | **1,704** |
| D | 3,087 | 2,347 | 3,231 | 1,491 | **10,137** |

*Strongest* is the count of repos for which the column is the highest
class achieved across any of its ecosystems (`class` column in
`value-data.csv`). 9,691 of the 12,842 rows are github groups; the other
3,151 are orphan packages (no `github_repo`) kept under sequential ids
so nothing is dropped.

EOL information is intentionally **not** stored here — it belongs to the
eligibility pipeline. `src/eligibility.py` joins per-ecosystem
`data/sources/{eco}/eol.csv` with `data/sources/{eco}/results.csv` directly to compute
per-repo `is_eol`, and writes it to `data/eligibility/eligibility.csv`.

Per-package data isn't preserved here either — see each ecosystem's
`data/sources/{eco}/results.csv` and `data/sources/{eco}/eol.csv` for the package-level
rows.

## Repo-level enrichment of value-data.csv

`src/aggregate_by_repo.py` adds repo-level columns to `data/value/value.csv`
(no separate per-repo file — value-data is the single source of truth for
both per-package and per-repo views). Group by `github_repo` (or use any
orphan row directly) and the repo-level columns are already there.

**Pipeline order**: each ecosystem's `check_eol.py` → `src.value.run_value_pipeline`
→ `aggregate_by_repo.py`. Re-running `src.value.run_value_pipeline` overwrites
`value-data.csv` without the enriched columns; re-run `aggregate_by_repo.py`
afterwards to restore them.

**Three-stage pipeline**: `src.value.run_value_pipeline` (this script) →
`src.risk.run_risk_pipeline` (concentration + complexity for A/B value-class repos) →
`src.eligibility.run_eligibility_pipeline` (filters to AB ∩ OSS ∩ alive).

**Grouping**: rows sharing a non-empty `github_repo` are merged into one
group; rows with an empty `github_repo` (e.g. cpp packages like `glibc`,
`gcc`) are treated as their own one-package groups so nothing is dropped.

**Per-ecosystem class**: within each ecosystem, the group's PR is the
**sum** of its packages' PR. Groups (with at least one package in that
ecosystem) are sorted by that sum desc, the cumulative share is computed,
and the per-ecosystem class is assigned via the same A/B/C/D cutoffs as
the package-level value pipeline (≤50%, ≤75%, ≤90%, rest). The strongest
across ecosystems becomes `repo_class`.

This avoids comparing PR magnitudes across ecosystems (which aren't
comparable — each ecosystem's PR mass sums to 1 within its own graph).
Recomputing PageRank on a repo-level dep graph was considered and skipped
because cross-ecosystem deps don't exist in our data; the repo graph is
still four disconnected subgraphs.

**Sort**: rows in `value-data.csv` end up sorted by `top_eco_pct` desc, so
the highest-importance packages come first.

## Current Limitations

Known scope choices and gaps in the current pipeline. Add new entries here
as they're identified — keeps caveats out of code comments and source-doc
footnotes.

### cpp dependency tree is runtime-only

Build-time tooling (cmake, pkgconf, autoconf, gettext, etc.), Debian
`Recommends`/`Suggests`, and Homebrew `build` deps **do not** propagate
PageRank. The cpp pipeline drops them at two points:

- Debian: `fetch_debian_data.py` only stores `Depends` + `Pre-Depends`.
- Homebrew: raw deps include both `runtime` and `build`, but
  `src/sources/cpp/process_data.py` filters to `runtime` only.

Consequence: PageRank reflects who *runs* with whom, not who *builds*
whom. Critical build infrastructure (cmake, pkgconf) is undervalued
relative to its real-world load-bearing role. To fix later: extend the
cpp edge schema to keep the source-side type and add a build-aware PR
overlay.

### Project identity is GitHub-only

Every downstream stage (eligibility, EOL, risk, GitHub-derived contributor
metrics) keys off the `github_repo` field. Projects that don't live on
GitHub are present in `value-data.csv` with `github_repo=""` and are
silently excluded from those analyses.

Examples affected: glibc (sourceware.org), gcc (gcc.gnu.org / Savannah),
libunistring (savannah), glib (gitlab.gnome.org), mpfr (gitlab.inria.fr),
curl (curl.se), ImageMagick (own host), many GNU/Apache/X.Org/KDE
projects, kernel-adjacent code.

**Status update**: `value-data.csv` now exposes `git_url` alongside
`github_repo`, so non-GitHub upstreams are no longer silently dropped at
the value-pipeline level. Coverage:

| Ecosystem | GitHub % | Git % (incl. non-GH) | A+B GitHub % | A+B Git % |
|---|---:|---:|---:|---:|
| npm | 99% | 99% | 100% | 100% |
| PyPI | 55% | 55% | 76% | 76% |
| crates.io | 96% | 99% | 99% | 100% |
| C/C++ | 26% | 41% | 32% | **95%** |
| **Total** | **82%** | **85%** | **93%** | **96%** |

Downstream stages (eligibility, EOL, GitHub contributor metrics) still
key off `github_repo`, so a non-GitHub-only project (glibc, gcc, etc.)
still slips out of those analyses even though it's now visible in
`value-data.csv` with a populated `git_url`. To fully fix: per-host
adapters for license/EOL/contributor checks against GitLab API, savannah,
sourceware, etc.

### No package-level quality gate before results.csv

`results.csv` admits everything in the top 95% cumulative download set
plus its transitive deps — no license check, no age cutoff, no popularity
floor, no archive/EOL gate. Quality filtering happens *downstream* in
`eligibility.py` and `check_eol.py`. This is intentional (keeps the
value scoring untouched by signal-quality concerns), but means the raw
value class distribution overstates how many projects we'd actually fund.

### Wayback-derived install stats have gaps

Homebrew and Debian popcon both come via Wayback Machine snapshots, which
sometimes truncate (1 MB cap) or miss a year entirely. `ecosystem_avg_downloads`
in `src/params.py` works around this by averaging only over populated years,
so a missing year doesn't deflate the average — but it does mean year-over-year
trend lines are noisy, and a few packages get their `avg_downloads` from
fewer than 5 years of data.
