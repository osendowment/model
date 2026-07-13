# Value Pipeline

Identifies the most important open-source packages across ecosystems by combining
download volume with dependency graph analysis (PageRank).

## Pipeline overview

The model runs **only** via `scripts/run-pipeline.sh`, whose stages execute in
order — **value → risk → eligibility → preview → health**. Stage runners are
never invoked by hand (the Preview stage is the one that gets forgotten, which
leaves `data/preview/preview.xlsx` stale; `health` aborts on a red check).

```bash
scripts/run-pipeline.sh                     # every stage
scripts/run-pipeline.sh --stage value       # the value stage alone
scripts/run-pipeline.sh --stage value --only unify   # one step of it
scripts/run-pipeline.sh --from-stage value  # value → … → health
```

The three scoring stages (each feeds the next):

```
1. **Value** (`src.value.run_value_pipeline`) → `data/value/value.csv` — picks the
   most-depended-on packages per ecosystem and ranks them by
   download-weighted PageRank, then unifies per-package classes into one
   row per repo (any host). All classes A/B/C are included. (this doc)
2. **Risk** (`src.risk.run_risk_pipeline`) → `data/risk/risk.csv` — concentration
   + complexity + security + workload scoring for the **top repos** (valid
   class-A on GitHub + GitLab, **archived included**) read directly from
   `data/value/value.csv`. The scope is configured in `src/settings.json`
   under `top_repos` (`platforms` + `classes`; `risk_input.value_classes`
   documents the same class scope, default `["A"]`). See [docs/risk.md](risk.md).
3. **Eligibility** (`src.eligibility.run_eligibility_pipeline`) →
   `data/eligibility/eligibility.csv` — four checks per **top repo** (the
   same valid class-A set, **archived included**): OSI-approved license
   (`oss`), funding intent (`intent`), not company-backed (`nonprofit`),
   not EOL/archived (`active`); `eligible` = AND of the four. See
   [docs/eligibility.md](eligibility.md).
```

### Dataflow at a glance

```
                Value pipeline                    Risk             Eligibility
                ───────────────                   ────             ───────────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C
registries     (95% cum dl)    (BFS)       ↓
                                      value.csv
                                            │
                                            ├─► top repos ─────► contributors + scc
                                            │   (settings.json          │
                                            │    top_repos: gh+gl,      │
                                            │    class A, archived  risk.csv
                                            │    included)
                                            └─► top repos ──────► licenses · funding
                                                (same set)         · EOL/archived
                                                                         │
                                                                  eligibility.csv
                                                                  (oss · intent ·
                                                                   nonprofit · active
                                                                   → eligible)
```

Risk and Eligibility both read the **same scope** directly from `value.csv`:
the top repos — valid class-A rows on the platforms configured in
`settings.json → top_repos` (GitHub + GitLab), **archived included**.
Archived repos surface in eligibility as `active=False` instead of being
silently dropped.

Source-specific details live in [`docs/sources/`](sources/) (one `.md` per source).

## Running it

The value stage runs only through the pipeline script; never invoke the
stage runner or a fetcher by hand.

```bash
scripts/run-pipeline.sh --from-stage value    # value → risk → eligibility → preview → health
scripts/run-pipeline.sh --stage value         # value alone (later stages left stale)
scripts/run-pipeline.sh --stage value --list  # its steps
scripts/run-pipeline.sh --stage value --offline   # pure-cache run, no network
```

Steps, in order (`src/value/run_value_pipeline.py`). Steps sharing a `pgroup`
run **concurrently**:

```
pgroup dumps:      repology · ossfuzz
pgroup eco:        npm · crates · pypi · cpp
sequential:        stats → git-urls
pgroup identity:   eco-fetch · canonical
sequential:        resolve → unify → validation
pgroup crit:       openssf-crit · eco-crit
sequential:        criticality
```

`repology` and `ossfuzz` are 30-day whole-file-TTL dumps the ecosystem
sub-pipelines read (Repology's `packages.csv` is the cpp pipeline's
distro-version input; OSS-Fuzz's `projects.csv` feeds `build_git_urls` and
the risk stage's fuzzed flag). `eco-fetch` and `canonical` each read the
previous run's `value.csv` and write their own file, so they run
concurrently — the former pulls ecosyste.ms repo candidates, the latter
resolves every `github_repo` to its current `nameWithOwner` + numeric id
(90-day per-row TTL). `openssf-crit` and `eco-crit` — the two criticality
sources — run after `unify` decides the class-A scope they fetch and before
`criticality` stamps both onto `value.csv`.

`--rollup` skips straight to the cross-ecosystem tail — see [Unified
output](#unified-output).

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
    ├── repo                      ← per-eco package→repo union (any host) [most recent]
    ├── platform                  ← host class of git_url                 [most recent]
    │                                (github/gitlab/codeberg/bitbucket
    │                                 /sourcehut/custom, empty for orphans)
    ├── repo_id                   ← `gh/<numeric>` (GitHub Repos API) or  [most recent]
    │                                `gl/<nickname>-<id>` (GitLab project API;
    │                                 bare `gl/<id>` for gitlab.com);
    │                                 empty for other platforms
    ├── git_url                   ← per-eco git.csv union                  [most recent]
    │                                (GitLab/Codeberg/Sourcehut/Bitbucket
    │                                 /custom hosts when no GH match);
    │                                 git clone URLs only — never a tarball/hg/svn
    ├── mirror_url                ← two meanings (see the column table):   [most recent]
    │                                (a) upstream a github MIRROR syncs from
    │                                    (GitHub Repos API + repo overrides)
    │                                (b) source location of a project with NO git
    │                                    upstream (mirror_url-only override)
    ├── git_valid                 ← build_validation (strictly True/False) [most recent]
    │                                (rollup of GitHub API + git ls-remote
    │                                 caches → value/validation.csv)
    ├── ecosystems                ← derived (eco set per repo)             [2021–2025]
    ├── packages                  ← derived (package count per repo)       [2021–2025]
    ├── top_eco                   ← derived (best percentile eco)          [2021–2025]
    ├── top_eco_pkg               ← derived (highest-PR pkg in top_eco)    [2021–2025]
    ├── top_eco_pct               ← derived (100 − pr_cum_pct, 0–100)      [2021–2025]
    ├── pr_score                  ← derived (per-eco ln PR-mass min-max    [2021–2025]
    │                                → p2-norm across ecos → max = 100)
    ├── class_{npm,pypi,crates,cpp}
    │                              ← derived (per-eco cum-PR share)        [2021–2025]
    ├── class                     ← derived (strongest across ecos)        [2021–2025]
    ├── openssf_crit              ← openssf/criticality.csv (github-only)  [most recent]
    ├── eco_crit                  ← ecosystems/criticality.csv (gh + gl)   [most recent]
    └── value_score               ← derived 0–100 blend (apply_criticality)
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
> (cpp runtime-only deps, GitHub/GitLab-only project identity, projects with no
> git upstream, etc.).

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

See the preview stats sheet → Value for the
per-ecosystem funnel counts and repo-coverage percentages.

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

Each ecosystem's own pipeline produces four files in `data/sources/{ecosystem}/`
(the cross-ecosystem steps add `git.csv` — the per-platform URL table built by
`build_git_urls` — and `check_eol.py` adds `eol.csv`, an Eligibility input):

### top-packages.csv

Packages covering 95% of ecosystem downloads.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `avg_downloads` | Average annual downloads (2021--2025) |
| `avg_downloads_share` | Fraction of ecosystem-wide total downloads |
| `2021`--`2025` | Downloads per year |

cpp differs: no per-year columns; instead `debian_avg_downloads`,
`debian_share`, `homebrew_avg_downloads`, `homebrew_share` (the two
install-base proxies it is unified from).

### dependency-tree.csv

Transitive dependency edges from top packages.

| Column | Description |
|--------|-------------|
| `package` | Dependent package |
| `dependency` | Dependency package |
| `type` | `declared` (npm/pypi/cpp), `normal`/`build`/`dev` (crates) |

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
| `github_repo` | Bare GitHub `owner/repo` slug (empty if the repo isn't on GitHub) |
| `git` | Canonical git clone URL across hosts (GitHub wins, else GitLab / Codeberg / … / custom) |
| `eco_guess` | Provenance of the repo identity after the `resolve` step: `eco` (ecosyste.ms), `native` (registry metadata), `override`, or empty |
| `avg_downloads` | Average annual downloads |
| `2021`--`2025` | Downloads per year |
| `top` | `True` if package is in the 95% cumulative set |
| `pagerank` | Download-weighted PageRank score |
| `value_class` | A/B/C (see [Value Classes](#value-classes)) |
| `repo_id` | Stable namespaced repo id (`gh/<id>` / `gl/…`), stamped by the `resolve` step |
| `mirror_url` | A GitHub mirror's non-GitHub upstream, when known — or, for a `mirror_url`-only override, the source location of a project with no git upstream at all (see [Manual overrides](#manual-overrides)) |
| `license` | Registry-reported license (stamped by the per-eco `fetch_licenses.py`) |

cpp differs: `debian_avg_downloads`, `homebrew_avg_downloads`, and the blended
`downloads_score` replace `avg_downloads` + the per-year and `top` columns.

## Unified output

`data/value/value.csv` is the canonical per-repo table — one row per repo
(on any host), plus one row per orphan package (no `repo`) so nothing is
dropped. **All classes A/B/C are included** — the complete long-tail table
is kept. Produced by the rollup steps of the value stage (`eco-fetch` →
`canonical` → `resolve` → `unify` → `validation` → `openssf-crit` →
`eco-crit` → `criticality`): `eco-fetch` pulls ecosyste.ms repo candidates
and `canonical` resolves every `github_repo` to its current `nameWithOwner`
+ immutable numeric id (GitHub's own rename authority); the `resolve` step
(`apply_ecosystems_authority`) applies both onto the per-eco `results.csv`,
re-resolving each package's repo identity under `override > github-canonical
> ecosyste.ms > prior registry data`; the `unify` step
(`src/value/unify_value_data.py`) reads each ecosystem's `results.csv` and
`eol.csv`, groups packages by repo, and computes all per-ecosystem and
cross-ecosystem aggregates in one pass (sorted by `top_eco_pct` desc). There
is no separate repo-aggregation step — `unify_value_data.py` produces the
per-repo table directly. `openssf-crit` and `eco-crit` then fetch the two
criticality sources for the class-A scope `unify` just decided, and the
final `criticality` step (`src.value.apply_criticality`) stamps
`openssf_crit` / `eco_crit` / `value_score` and re-sorts the shipped file by
`value_score` desc — unscored rows (below the 2-component floor, mostly
class B/C) sink to the end in `top_eco_pct`-desc order. Manual corrections
come from [`data/value/overrides.csv`](#manual-overrides).

After editing `overrides.csv`, `uv run python -m src.value.run_value_pipeline
--rollup` reruns that same rollup chain (`eco-fetch` → `canonical` →
`resolve` → `unify` → `validation` → `openssf-crit` → `eco-crit` →
`criticality`) from the existing per-eco `results.csv`, without re-running
the ecosystem sub-pipelines or the `stats` / `git-urls` steps — the one
value-stage entry point that `scripts/run-pipeline.sh` does not wrap. It
leaves the later stages stale, so follow it with `scripts/run-pipeline.sh
--from-stage risk`.

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
| `repo` | Lowercase repo slug on its `platform` — GitHub `owner/repo`, GitLab's arbitrarily-nested `owner/…/repo`, Sourcehut `~user/repo`, custom best-effort path. Empty only for orphans (no upstream repo at all). |
| `platform` | Host class of `git_url`, from `classify()` in `src/value/build_git_urls.py`: `github` / `gitlab` / `bitbucket` / `sourcehut` / `codeberg` / `custom`. `gitlab` means **any** GitLab instance — `classify()` derives its host set from `HOST_NICKNAMES` in `src/sources/gitlab/gitlab_client.py` (gitlab.com, salsa.debian.org, gitlab.gnome.org, gitlab.freedesktop.org, invent.kde.org, code.videolan.org, gitlab.inria.fr, …), plus a `gitlab.*` heuristic for self-hosted instances not yet registered. A registered host missing from that set would fall through to `custom`, get no `repo_id`, and silently drop out of the top-repo scope. Empty for orphan rows with no URL. Downstream consumers (risk, eligibility) filter on the platforms configured in `settings.json → top_repos.platforms` (currently `github` + `gitlab`). |
| `repo_id` | Stable repo id namespaced by platform: `gh/<numeric>` (GitHub Repos API id) for a resolved GitHub repo; `gl/<nickname>-<id>` (bare `gl/<id>` for gitlab.com; host nicknames per `HOST_NICKNAMES` in `src/sources/gitlab/gitlab_client.py`) for a project resolved via the GitLab project API on any GitLab host; empty for other platforms (no API id) and unresolved/404 repos. |
| `git_url` | Canonical **git clone URL** — `https://github.com/<repo>.git` for GitHub repos (so a valid repo always carries both `repo` and `git_url`), otherwise the non-GitHub canonical (GitLab / Codeberg / Sourcehut / Bitbucket / custom: sourceware.org, savannah, gitlab.gnome.org, etc.). For non-GitHub repos it's the first non-empty value from per-ecosystem `data/sources/{eco}/git.csv`, canonicalised by the shared git-URL helpers (`src/value/git_urls.py`). A **non-git** source (tarball / hg / svn) never belongs here — it goes in `mirror_url` (see [Manual overrides](#manual-overrides)). Empty for orphan packages and for projects with no git upstream at all. |
| `mirror_url` | **Two distinct meanings**, distinguished by whether the row has a repo. (1) *Mirror upstream* — on a **GitHub mirror repo** row, the non-GitHub upstream it syncs from (`gcc-mirror/gcc` → `https://gcc.gnu.org/git/gcc.git`). Two sources: GitHub's own `mirror_url` field from `data/sources/github/repos.csv` (stamped by the rollup's `resolve` step), and override-declared live upstreams — a repo override carrying a non-GitHub `git_url` (`torvalds/linux` → `https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`) is preserved here. Authoritative mirror→upstream link when present. (2) *No-git source* — on a row with **no `repo`, no `repo_id` and no `git_url`**, the place the code actually lives for a project that has no git upstream at all (tarball / hg / svn: IJG libjpeg, Info-ZIP unzip, GraphicsMagick, Berkeley DB, R). Set by a `mirror_url`-only row in `overrides.csv`. Empty for ordinary rows. |
| `git_valid` | Strictly `True` / `False` — never blank. `True` iff the repo's upstream is reachable. Host-agnostic: GitHub rows are checked via the Repos API cache, non-GitHub rows via `git ls-remote`; a GitLab `gl/` `repo_id` counts as proof on its own (the GitLab project API confirmed the project exists, which is more authoritative than `ls-remote` on hosts whose `git://` endpoint can fail for a live project). `False` means **either** of two legitimate outcomes: *no `git_url` at all* — an orphan package, or a project with no git upstream (nothing to validate) — **or** *a `git_url` that failed validation* (unreachable / 404). Set by `build_validation`; audit trail in [`data/value/validation.csv`](components/validation.md). |
| `ecosystems` | Comma-separated list of ecosystems where the repo has packages (e.g. `crates,npm`) |
| `packages` | Total package count in the repo |
| `top_eco` | Ecosystem where the repo is highest-ranked (max PR percentile). `npm` / `pypi` / `crates` / `cpp`. |
| `top_eco_pkg` | Highest-PR package in `top_eco` (e.g. `@babel/helper-plugin-utils` for babel/babel) |
| `top_eco_pct` | The repo's PageRank **position** inside its strongest ecosystem (`top_eco`). Per registry (npm / PyPI / crates / cpp = Debian + Homebrew): download stats pick the top packages (95% of cumulative downloads), their dependency tree is fetched, and a download-personalized PageRank (α = 0.85) ranks every node; `top_eco_pct = 100 − cumulative-PR percentile`. 0–100, **higher = better**; the tail sits near 0. A `value_score` component (weight `centrality_weight`). |
| `pr_score` | Cross-ecosystem dependency **mass**, complementing `top_eco_pct`'s position. 0–100 (two decimals), **higher = better**. Per ecosystem the repo's summed package PageRank is `ln`-scaled and min-max normalized over that ecosystem's repos; the per-eco values combine as a **p = 2 norm** (`PR_SCORE_P` in `unify_value_data.py`) — a real second-ecosystem footprint adds ~41% (√2), a token listing ~nothing, and an extra ecosystem never lowers the score — then the column is rescaled so the top repo = 100. Blank only for groups with no PageRank signal at all. A `value_score` component (weight `pr_score_weight`). |
| `class` | Strongest of the per-ecosystem classes (A < B < C) |
| `class_npm`, `class_pypi`, `class_crates`, `class_cpp` | A/B/C from per-ecosystem cumulative PR share; empty if no package in that ecosystem |
| `openssf_crit` | OpenSSF criticality score ·100 (0–100, two decimals, higher = more critical; the source CSV keeps the raw 0–1 value), joined from `data/sources/openssf/criticality.csv` by `src.value.apply_criticality` (the last value.csv-writing pipeline step). **Non-empty for every valid class-A GitHub repo, archived included** — that is the fetch scope, and `scripts/pipeline_health.py` gates on it. Empty for non-GitHub rows (the tool is GitHub-only), B/C rows outside the fetch scope, and unresolved/invalid repos. |
| `eco_crit` | ecosyste.ms critical flag: `100` = the repo's canonical package is on ecosyste.ms's critical list, `0` = **explicitly** not on it (`critical=false`), blank = unknown — the fetch didn't resolve, the registry omitted the flag (common for spack/debian cpp packages), or the repo was outside the class-A scope. A checked-but-blank flag is never a real 0. Joined from `data/sources/ecosystems/criticality.csv` by `src.value.apply_criticality`. Unlike `openssf_crit` it covers **GitHub and GitLab**. |
| `value_score` | Value score, a 0–100 **pro-rata blend** of up to four components — `openssf_crit` (weight 0.6), `eco_crit` (0.2), `top_eco_pct` (0.1), and `pr_score` (0.1). Only the components present for a repo are summed and the total is renormalized by their weight sum, so a repo missing some still lands on the same 0–100 scale. Blank unless at least `min_components` (2) are present. Weights in `settings.json → value_score`, criticality-dominant so a foundational-but-quiet micro-dep can't outrank a genuinely critical project; the two PageRank-family factors (position vs mass) split the 0.2 centrality weight. Stamped by `src.value.apply_criticality`. GitLab class-A repos (no `openssf_crit`) score from the remaining components — `top_eco_pct` + `pr_score` alone clear the 2-component floor. |

### Manual overrides

`data/value/overrides.csv` is the single hand-maintained list of corrections for
packages whose **upstream registry metadata is wrong** — a gap-correcting layer
for bad source data, not a patch for a parsing bug. Rows key on
`(package, ecosystem)`; a row with a blank `reason` is rejected.

| Column | Meaning |
|---|---|
| `package`, `ecosystem` | The key. |
| `repo` | Force the correct GitHub `owner/repo`. Sets `platform` = `github` and derives the matching `git_url`. |
| `git_url` | Force a corrected non-GitHub **git clone URL**. Absolute: every eco-/registry-derived host is dropped, and `(platform, repo)` are re-derived from it. |
| `mirror_url` | The project has **no git upstream** (tarball / hg / svn only) — this is where its source actually lives. |
| `valid` | Pin the git target's validity (`True`/`False`); consumed by `build_validation`, not applied at resolve time. |
| `reason` | Required free-text justification. |

Two encodings matter.

**Repo / URL correction** — set `repo` (GitHub) or `git_url` (any other host).
A non-GitHub `git_url` on a `repo` row is the live upstream a GitHub mirror
syncs from, and is preserved as `mirror_url` (`torvalds/linux` →
`git.kernel.org`).

**No git upstream** — `git_url` **blank**, the real source URL in `mirror_url`,
`valid` **blank**. The package resolves to no repo at all, and validity is
*derived*: no `git_url` ⇒ nothing to validate ⇒ `git_valid` = `False`. A non-git
URL must never go in `git_url`. The point is that the alternative — mapping the
package to a fork or a personal mirror — credits the wrong maintainers: IJG's
libjpeg was crediting `mozilla/mozjpeg`, Info-ZIP's unzip was crediting zlib's
author. Info-ZIP unzip, GraphicsMagick (hg), Berkeley DB (Oracle tarball) and R
(svn) are encoded this way.

Overrides are applied by the `resolve` step (`apply_ecosystems_authority` —
rewrites each `results.csv`'s `git` / `github_repo`, stamps `mirror_url`) and by
the `unify` step (forces the group's identity); the `valid` pin is applied by
`build_validation`.

### Repo class distribution

After grouping packages by `repo_id` / `git_url` (or as orphans), `value.csv`
collapses the package rows into one row per repo plus one per orphan
package (no `repo`, kept as its own row so nothing is dropped).
*Strongest* class is the highest class a repo achieves across any of its
ecosystems (the `class` column in `value.csv`).

See the preview stats sheet → Value for the per-class ×
per-ecosystem counts and the GitHub-group / orphan split.

EOL information is intentionally **not** stored here — it feeds the
Eligibility stage, not the value table. The per-ecosystem `check_eol.py`
scripts compute it and write `data/sources/{eco}/eol.csv`; those are
advisory inputs to the manual `eol` override column in
`data/eligibility/overrides.csv`, which `src.eligibility.build_active`
consumes (see [eligibility.md](eligibility.md)).

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

### Project identity is GitHub/GitLab-only downstream

Downstream consumers (risk, eligibility, EOL, contributor metrics) key off
the hosts configured in `settings.json → top_repos.platforms` — currently
**GitHub and GitLab** (`value.csv` rows with `platform` `github` /
`gitlab`). GitLab repos are first-class: they carry a `gl/…` `repo_id`
resolved via the GitLab project API (on any GitLab host — gitlab.com,
gitlab.gnome.org, gitlab.inria.fr, salsa.debian.org, …) and are scored by
risk and eligibility alongside GitHub repos. Formerly-excluded projects now
in scope this way include glib (gitlab.gnome.org), mpfr (gitlab.inria.fr) and
pixman (gitlab.freedesktop.org). gcc is covered via its GitHub mirror
(`gcc-mirror/gcc`) with the live upstream recorded in `mirror_url`. glibc is
not in scope: its upstream is sourceware.org (a `custom` host, so no
`repo_id`), its GitHub mirror `bminor/glibc` is archived, and the Debian salsa
repo carries only packaging files, not glibc's source.

`value.csv` models every repo as a `(platform, repo, repo_id)` triple
alongside `git_url`, so upstreams on the remaining hosts (codeberg /
bitbucket / sourcehut / custom: savannah, sourceware, kernel.org,
project-owned hosts) carry a first-class identity and are not silently
dropped at the value-pipeline level — but they are still excluded from the
downstream analyses (e.g. libunistring on savannah). Per-ecosystem GitHub vs
Git coverage (and the load-bearing class-A subset) is in
the preview stats sheet → Value.

A GitLab host is only recognised as such if it is registered in `HOST_NICKNAMES`
(`src/sources/gitlab/gitlab_client.py`) or matches the `gitlab.*` heuristic —
that registry is what `classify()` derives its GitLab host set from, and it also
assigns the `gl/<nickname>-<id>` prefix. An unregistered host classifies as
`custom`, gets no `repo_id`, and silently drops out of the top-repo scope, so a
new GitLab instance must be added there before its repos can be scored.

To fully fix: per-host adapters for license/EOL/contributor checks against
codeberg, savannah, sourceware, etc.

### Some projects have no git upstream at all

A handful of load-bearing C/C++ projects publish only tarballs, Mercurial, or
Subversion — IJG libjpeg, Info-ZIP unzip, GraphicsMagick, Berkeley DB, R. They
carry no `repo` / `repo_id` / `git_url`, so `git_valid` is `False` and they are
out of the top-repo scope: **unfundable via a repo**, which is the honest
answer. Their source location is recorded in `mirror_url` via a
`mirror_url`-only row in [`overrides.csv`](#manual-overrides), deliberately in
preference to mapping them onto a fork or a personal GitHub mirror, which would
credit the wrong maintainers.

### No package-level quality gate before results.csv

`results.csv` admits everything in the top 95% cumulative download set
plus its transitive deps — no license check, no age cutoff, no popularity
floor, no archive/EOL gate. Quality filtering happens *downstream*: the
per-ecosystem `check_eol.py` / `fetch_licenses.py` signals feed the
Eligibility stage's checks on the top repos. This is intentional (keeps the
value scoring untouched by signal-quality concerns), but means the raw
value class distribution overstates how many projects we'd actually fund.

### Wayback-derived install stats have gaps

Homebrew and Debian popcon both come via Wayback Machine snapshots, which
sometimes truncate (1 MB cap) or miss a year entirely. `ecosystem_avg_downloads`
in `src/common/params.py` works around this by averaging only over populated years,
so a missing year doesn't deflate the average — but it does mean year-over-year
trend lines are noisy, and a few packages get their `avg_downloads` from
fewer than 5 years of data.
