# Value Pipeline

Identifies the most important open-source packages across ecosystems by combining
download volume with dependency graph analysis (PageRank).

## Pipeline overview

Two automated stages (run in order, each feeds the next), followed by a
separate **manual** review:

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
```

**Eligibility** is no longer an automated pipeline stage. It is a manual
review of the top-ranked candidates surfaced by Value + Risk, checking OSS
license, end-of-life (EOL) status, and independence (no corporate
trademarks, no associated startups, community-led). The source signals
that inform it (OSI license lists, foundation membership, per-ecosystem
`check_eol.py` / `fetch_licenses.py`) are still collected under
`src/sources/`, but there is no automated eligibility runner and no
eligibility stage output.

### Dataflow at a glance

```
                Value pipeline                    Risk           Manual review
                ───────────────                   ────           ─────────────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C
registries     (95% cum dl)    (BFS)       ↓
                                      value.csv
                                            │
                                            └─► class A repos ──► contributors + scc
                                                (settings.json          │
                                                 risk_input.            │
                                                 value_classes)    risk.csv
                                                                       │
                                                                       ▼
                                                              top candidates
                                                                       │
                                                              manual eligibility
                                                              (OSS license · EOL ·
                                                               independence)
```

The two automated stages stop at `risk.csv`. Eligibility is a manual review
of the top candidates — the `repo`-keyed license/EOL signals below
are inputs to that review, not an automated output.

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
    ├── repo                      ← per-eco package→repo union (any host) [most recent]
    ├── platform                  ← host class of git_url                 [most recent]
    │                                (github/gitlab/codeberg/bitbucket
    │                                 /sourcehut/custom, empty for orphans)
    ├── repo_id                   ← GitHub Repos API id, `gh/<numeric>`   [most recent]
    │                                (empty for non-GitHub platforms)
    ├── git_url                   ← per-eco git.csv union                  [most recent]
    │                                (GitLab/Codeberg/Sourcehut/Bitbucket
    │                                 /custom hosts when no GH match)
    ├── mirror_url                ← GitHub Repos API `mirror_url`          [most recent]
    │                                (upstream a github mirror syncs from;
    │                                 e.g. gcc-mirror/gcc → gcc.gnu.org)
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

Packages remaining after each pipeline stage (top → dep tree → results), plus
the share with a known upstream repo at the end. *GH %* counts only github.com;
*Git %* also counts gitlab, bitbucket, sourcehut, codeberg, and `custom` hosts
(savannah, sourceware, kernel.org, etc.) -- see [Unified output](#unified-output).
*Top packages* covers 95% of cumulative downloads; *After dep tree* is
`|top ∪ transitive deps|` (the universe analysed for PageRank); *Results* keeps
every node from that universe (top packages with no edges still get a row with
PageRank = 0). C/C++'s `Results` is smaller than `After dep tree` because the
cpp pipeline applies an `is_cpp` filter — language-agnostic distro packages get
dropped.

See [docs/stats.md → Value](stats.md#per-ecosystem-value-funnel) for the
per-ecosystem funnel counts and repo-coverage percentages.

## Ecosystems

Each language assembles the steps above from its own sources. The per-language
component docs cover **what data is collected from each source and which pipeline
stage uses it** (Value → Risk, plus the manual eligibility review):

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

`data/value/value.csv` is the canonical per-repo table — one row per repo
(on any host), plus one row per orphan package (no `repo`) so nothing is
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
| `repo` | Lowercase repo slug on its `platform` — GitHub `owner/repo`, GitLab's arbitrarily-nested `owner/…/repo`, Sourcehut `~user/repo`, custom best-effort path. Empty only for orphans (no upstream repo at all). |
| `platform` | Host class of `git_url`: `github` / `gitlab` / `bitbucket` / `sourcehut` / `codeberg` / `custom`. Empty for orphan rows with no URL. Downstream GitHub-only consumers (risk, eligibility) filter on `platform == github`. |
| `repo_id` | Stable repo id namespaced by platform: `gh/<numeric>` (GitHub Repos API id) for a resolved GitHub repo; empty for non-GitHub platforms (no numeric id) and unresolved/404 repos. |
| `git_url` | Canonical git clone URL — `https://github.com/<repo>.git` for GitHub repos (so a valid repo always carries both `repo` and `git_url`), otherwise the non-GitHub canonical (GitLab / Codeberg / Sourcehut / Bitbucket / custom: sourceware.org, savannah, gitlab.gnome.org, etc.). For non-GitHub repos it's the first non-empty value from per-ecosystem `data/sources/{eco}/git.csv`, canonicalised by `verify_git_urls`. Empty only for orphan packages with no upstream repo at all. |
| `mirror_url` | For a **GitHub mirror repo**, the non-GitHub upstream it syncs from — GitHub's own `mirror_url` field (e.g. `gcc-mirror/gcc` → `git://gcc.gnu.org/git/gcc.git`). Populated by `verify_git_urls` from `data/sources/github/repos.csv`. Sparse: **only** repos GitHub natively imported as mirrors carry it (externally-maintained push-mirrors like `bminor/glibc` do not). Empty for ordinary and non-GitHub rows. Authoritative mirror→upstream link when present. |
| `ecosystems` | Comma-separated list of ecosystems where the repo has packages (e.g. `crates,npm`) |
| `packages` | Total package count in the repo |
| `top_eco` | Ecosystem where the repo is highest-ranked (max PR percentile). `npm` / `pypi` / `crates` / `cpp`. |
| `top_eco_pkg` | Highest-PR package in `top_eco` (e.g. `@babel/helper-plugin-utils` for babel/babel) |
| `top_eco_pct` | PR percentile in `top_eco` (`100 − pr_cum_pct`). 0–100, **higher = better**. babel/babel = 92.25; tail near 0. |
| `class` | Strongest of the per-ecosystem classes (A < B < C) |
| `class_npm`, `class_pypi`, `class_crates`, `class_cpp` | A/B/C from per-ecosystem cumulative PR share; empty if no package in that ecosystem |

### Repo class distribution

After grouping packages by `git_url` / repo (or as orphans), `value.csv` collapses
the package rows into one row per repo plus one per orphan
package (no `repo`, kept under sequential ids so nothing is dropped).
*Strongest* class is the highest class a repo achieves across any of its
ecosystems (the `class` column in `value.csv`).

See [docs/stats.md → Value](stats.md#repo-class-distribution) for the per-class ×
per-ecosystem counts and the GitHub-group / orphan split.

EOL information is intentionally **not** stored here — it feeds the manual
eligibility review, not the value table. The per-ecosystem `check_eol.py`
scripts compute it and write `data/sources/{eco}/eol.csv`; joining that with
the matching `data/sources/{eco}/results.csv` yields per-repo `is_eol` for the
top candidates under manual review.

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

Every downstream consumer (risk, EOL, GitHub-derived contributor metrics,
and the manual eligibility review) keys off GitHub identity — `value.csv`
rows with `platform == github`. Projects that don't live on GitHub carry a
non-GitHub `platform` (gitlab / codeberg / custom) and are excluded from
those analyses.

Examples affected: glibc (sourceware.org), gcc (gcc.gnu.org / Savannah),
libunistring (savannah), glib (gitlab.gnome.org), mpfr (gitlab.inria.fr),
curl (curl.se), ImageMagick (own host), many GNU/Apache/X.Org/KDE
projects, kernel-adjacent code.

**Status update**: `value.csv` now models every repo as a
`(platform, repo, repo_id)` triple alongside `git_url`, so non-GitHub
upstreams carry a first-class identity (host + slug) and are no longer
silently dropped at the value-pipeline level. Per-ecosystem GitHub vs Git
coverage (and the load-bearing class-A subset) is in
[docs/stats.md → Value](stats.md#repo-identity-coverage-valuecsv).

Downstream consumers (risk, EOL, GitHub contributor metrics, the manual
eligibility review) still filter to `platform == github`, so a
non-GitHub-only project (glibc, gcc, etc.) still slips out of those
analyses even though it's now visible in `value.csv` with a populated
`platform` / `repo` / `git_url`. To fully fix: per-host adapters for
license/EOL/contributor checks against GitLab API, savannah, sourceware, etc.

### No package-level quality gate before results.csv

`results.csv` admits everything in the top 95% cumulative download set
plus its transitive deps — no license check, no age cutoff, no popularity
floor, no archive/EOL gate. Quality filtering happens *downstream*: the
per-ecosystem `check_eol.py` / `fetch_licenses.py` signals feed the manual
eligibility review of the top candidates. This is intentional (keeps the
value scoring untouched by signal-quality concerns), but means the raw
value class distribution overstates how many projects we'd actually fund.

### Wayback-derived install stats have gaps

Homebrew and Debian popcon both come via Wayback Machine snapshots, which
sometimes truncate (1 MB cap) or miss a year entirely. `ecosystem_avg_downloads`
in `src/common/params.py` works around this by averaging only over populated years,
so a missing year doesn't deflate the average — but it does mean year-over-year
trend lines are noisy, and a few packages get their `avg_downloads` from
fewer than 5 years of data.
