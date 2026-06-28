# Value Pipeline

Identifies the most important open-source packages across ecosystems by combining
download volume with dependency graph analysis (PageRank).

## Pipeline overview

The three pipeline stages (run in order, each feeds the next):

```
1. **Value** (`src.value.run_value_pipeline`) → `data/value/value.csv` — picks the
   most-depended-on packages per ecosystem and ranks them by
   download-weighted PageRank, then unifies per-package classes into one
   row per GitHub repo. All classes A/B/C are included. (this doc)
2. **Risk** (`src.risk.run_risk_pipeline`) → `data/risk/risk.csv` — concentration
   + complexity + issue-debt scoring for **valid class-A repos** read
   directly from `data/value/value.csv`. Target classes are configured in
   `src/settings.json` under `risk_input.value_classes` (default
   `["A"]`). See [docs/risk.md](risk.md).
3. **Eligibility** (`src.eligibility.run_eligibility_pipeline`) → `data/eligibility/eligibility.csv`
   — restricts to class-A repos with a fresh GitHub API record, an
   OSI-approved license, and a non-EOL signal. Runs after Risk. See [docs/eligibility.md](eligibility.md).
```

### Dataflow at a glance

```
                Value pipeline                    Risk                Eligibility
                ───────────────                   ────                ───────────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C
registries     (95% cum dl)    (BFS)       ↓
                                      value.csv
                                            │
                                            ├─► class A repos ──► contributors + scc
                                            │   (settings.json          │
                                            │    risk_input.            │
                                            │    value_classes)   risk.csv
                                            │
                                            └─► github_repo
                                                    │
                                                    ├──────────────► repos.csv
                                                    │                    │
                                                    │              license + EOL
                                                    │                    │
                                                    │            eligibility.csv
```

Source-specific details live in [`docs/sources/`](sources/) (one `.md` per source).

## Metrics Roadmap

Inputs per dimension, current as of the last pipeline run. Each leaf = one metric, with its data
source(s) and the time period it represents. **Per-language metric lineage now lives in the
language component docs** — [JavaScript/npm](components/javascript.md),
[Python/PyPI](components/python.md), [Rust/crates](components/rust.md), and
[C/C++](components/cpp.md). The cross-ecosystem rollup that unifies them into
`value/value.csv` is below.

> **Note:** `[2021–2025]` reflects the 5-year window; `[most recent]` means the
> latest available pull of that source.

```
Value
│
├── Per-language value pipelines      → components/{javascript,python,rust,cpp}.md
│       downloads → top (95% cum-dl) → dep tree → DL-weighted PageRank (α=0.85) → value_class
│       (per-language metric lineage + sources live in each component doc)
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
3. **Scoring** -- download-weighted PageRank over the dep graph, then classify A/B/C

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
| **A** | 0--75% | Critical infrastructure |
| **B** | 75--95% | Important, widely depended on |
| **C** | 95--100% | Long tail |

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

### Value class distribution (per-package — legacy snapshot)

> **Stale, pending the next full pipeline run.** These are *per-package*
> `value_class` counts read from each `data/sources/<eco>/results.csv`, which is
> still on the legacy **4-class** scheme (A/B/C/D) and is regenerated only when
> the per-ecosystem `process_data` stages re-run. The authoritative 3-class
> *repo-level* distribution (from the regenerated `value.csv`) is in
> [Repo class distribution](#repo-class-distribution) below — read the table
> here as a historical snapshot.

Per-ecosystem and combined per-package class counts (legacy 4-class snapshot).

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
both rely on. C/D-class rows are present in `value.csv` and tracked
through the value pipeline, but are outside the default Risk and
Eligibility scope. C/C++'s A+B Git jumps from 32% to 95% once
non-GitHub upstreams are counted (glibc, gcc, libunistring, glib, mpfr,
etc. live on sourceware / savannah / gitlab hosts, not GitHub).
Non-GitHub upstreams are concentrated in C/D classes, so the A+B
numbers barely move.

## Ecosystems

Each language assembles the steps above from its own sources. The per-language
component docs cover **what data is collected from each source and which pipeline
stage uses it** (Value → Risk → Eligibility):

| Language | Registry / sources | Pipeline doc | Raw-fetch reference |
|---|---|---|---|
| JavaScript / TypeScript | npm | [components/javascript.md](components/javascript.md) | [sources/npm.md](sources/npm.md) |
| Python | PyPI | [components/python.md](components/python.md) | [sources/pypi.md](sources/pypi.md) |
| Rust | crates.io | [components/rust.md](components/rust.md) | [sources/crates.md](sources/crates.md) |
| C / C++ | Debian + Homebrew + Repology + OSS-Fuzz | [components/cpp.md](components/cpp.md) | [debian](sources/debian.md) · [homebrew](sources/homebrew.md) · [repology](sources/repology.md) · [ossfuzz](sources/ossfuzz.md) |

C/C++ has no single registry — it's unified from Debian and Homebrew (joined via
Repology), which is why it has a component doc but no `sources/cpp.md`. The
Wayback-derived install proxies (Debian popcon, Homebrew analytics) and their
snapshot caveats are documented in the [debian](sources/debian.md) and
[homebrew](sources/homebrew.md) source pages.

## Value data sources

Compact source → extracted-fields reference for the Value stage; per-language pipeline mechanics live in the [component docs](#ecosystems) linked above.

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
| `value_class` | A/B/C (see [Value Classes](#value-classes)) |

## Unified output

`data/value/value.csv` is the canonical per-repo table — one row per GitHub
repo, plus one row per orphan package (no `github_repo`) so nothing is
dropped. **All classes A/B/C are included** — the complete long-tail table
is kept. Produced by the `unify` step of `uv run python -m src.value.run_value_pipeline`
(`src/value/unify_value_data.py`), which reads each ecosystem's `results.csv`
and `eol.csv`, groups packages by repo, computes all per-ecosystem and
cross-ecosystem aggregates in one pass, and writes the file sorted by
`top_eco_pct` desc (most important repos first). There is no separate
repo-aggregation step — `unify_value_data.py` produces the per-repo table
directly. Manual repo / `git_url` / `valid` corrections are applied from
`data/value/overrides.csv` (by `unify_value_data.py` + `build_validation.py`).

Per-ecosystem class is computed by summing the group's package PR within
the ecosystem, ranking groups by that sum desc, and applying the same
A/B/C cumulative-share cutoffs as the package-level pipeline (≤75%,
≤95%, rest). The strongest across ecosystems becomes `class`.
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
| `class` | Strongest of the per-ecosystem classes (A < B < C) |
| `class_npm`, `class_pypi`, `class_crates`, `class_cpp` | A/B/C from per-ecosystem cumulative PR share; empty if no package in that ecosystem |

### Repo class distribution

After grouping packages by `github_repo` (or as orphans), `value.csv`
collapses 17,609 package rows into 12,096 repo rows. Counts below are the
current 3-class distribution, derived directly from the regenerated `value.csv`.

| | npm | PyPI | crates.io | C/C++ | Strongest |
|---|---:|---:|---:|---:|---:|
| A | 571 | 163 | 132 | 89 | **953** |
| B | 1,414 | 638 | 533 | 570 | **3,148** |
| C | 2,428 | 1,726 | 2,911 | 961 | **7,995** |

*Strongest* is the count of repos for which the column is the highest
class achieved across any of its ecosystems (`class` column in
`value.csv`). 10,529 of the 12,096 rows are github groups; the other
1,567 are orphan packages (no `github_repo`) kept under sequential ids
so nothing is dropped.

EOL information is intentionally **not** stored here — it belongs to the
eligibility pipeline. `src/eligibility/classify_eligibility.py` joins per-ecosystem
`data/sources/{eco}/eol.csv` with `data/sources/{eco}/results.csv` directly to compute
per-repo `is_eol`, and writes it to `data/eligibility/eligibility.csv`.

Per-package data isn't preserved here either — see each ecosystem's
`data/sources/{eco}/results.csv` and `data/sources/{eco}/eol.csv` for the package-level
rows.

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
GitHub are present in `value.csv` with `github_repo=""` and are
silently excluded from those analyses.

Examples affected: glibc (sourceware.org), gcc (gcc.gnu.org / Savannah),
libunistring (savannah), glib (gitlab.gnome.org), mpfr (gitlab.inria.fr),
curl (curl.se), ImageMagick (own host), many GNU/Apache/X.Org/KDE
projects, kernel-adjacent code.

**Status update**: `value.csv` now exposes `git_url` alongside
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
`value.csv` with a populated `git_url`. To fully fix: per-host
adapters for license/EOL/contributor checks against GitLab API, savannah,
sourceware, etc.

### No package-level quality gate before results.csv

`results.csv` admits everything in the top 95% cumulative download set
plus its transitive deps — no license check, no age cutoff, no popularity
floor, no archive/EOL gate. Quality filtering happens *downstream* in
`classify_eligibility.py` and `check_eol.py`. This is intentional (keeps the
value scoring untouched by signal-quality concerns), but means the raw
value class distribution overstates how many projects we'd actually fund.

### Wayback-derived install stats have gaps

Homebrew and Debian popcon both come via Wayback Machine snapshots, which
sometimes truncate (1 MB cap) or miss a year entirely. `ecosystem_avg_downloads`
in `src/common/params.py` works around this by averaging only over populated years,
so a missing year doesn't deflate the average — but it does mean year-over-year
trend lines are noisy, and a few packages get their `avg_downloads` from
fewer than 5 years of data.
