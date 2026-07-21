# Value Pipeline

Ranks open-source packages by download volume and dependency-graph centrality
(PageRank), then rolls packages up into one row per repo.

## Pipeline overview

Run the model only through `scripts/run-pipeline.sh`. Its stages execute in
order — **value → risk → eligibility → preview → health**. Never invoke a stage
runner by hand: a skipped preview stage leaves `data/preview/preview.xlsx`
stale, and `health` aborts the run on a red check.

**Three stages score; two do not.** Value, Risk and Eligibility compute every
number. `preview` (`src.run_preview_pipeline`) rebuilds the deliverables in
`data/preview/`, and `health` (`scripts/pipeline_health.py`) audits each stage
CSV against its builder. Neither changes a score.

The three scoring stages each feed the next:

1. **Value** (`src.value.run_value_pipeline`) → `data/value/value.csv` — ranks
   the most-depended-on packages per ecosystem by download-weighted PageRank,
   then unifies the per-package classes into one row per repo on any host.
   Classes A, B and C are all kept. (this page)
2. **Risk** (`src.risk.run_risk_pipeline`) → `data/risk/risk.csv` —
   concentration + complexity + security + workload scores for the top-repo
   set. See [docs/risk.md](risk.md).
3. **Eligibility** (`src.eligibility.run_eligibility_pipeline`) →
   `data/eligibility/eligibility.csv` — four checks per top-repo row: an
   approved OSS license (`oss`, from OSI + the SPDX License List), funding
   intent (`intent`), not company-backed (`nonprofit`), not EOL/archived
   (`active`). `eligible` is the AND of the four.
   See [docs/eligibility.md](eligibility.md).

**Top-repo scope.** Risk and Eligibility read the same set directly from
`value.csv`: rows with `git_valid=True`, `class=A`, and a `platform` listed in
`settings.json → top_repos.platforms` (`github` + `gitlab`).
`risk_input.value_classes` records the same class scope. Archived repos stay in
and surface in eligibility as `active=False`.

**This class-A set is the "core".** *Core* is the plain-language name for it:
the projects the ecosystem actually runs on. Risk and Eligibility operate only
inside the core. The docs otherwise use the precise terms — **class-A** and
**top-repo set** — because those name the exact filter.

### Dataflow at a glance

```
                Value pipeline                    Risk             Eligibility
                ───────────────                   ────             ───────────
ecosystem ──► top packages ──► dep tree ──► PageRank ──► A/B/C
registries     (95% cum dl)    (BFS)       ↓
                                      value.csv
                                            │
                                            ├─► top-repo set ──► contributors + scc
                                            │   (gh+gl, class-A,        │
                                            │    archived included)  risk.csv
                                            │
                                            └─► top-repo set ───► licenses · funding
                                                (same set)         · EOL/archived
                                                                         │
                                                                  eligibility.csv
                                                                  (oss · intent ·
                                                                   nonprofit · active
                                                                   → eligible)
```

Source-specific details live in [`docs/sources/`](sources/), one page per source.

## Running it

```bash
scripts/run-pipeline.sh                       # every stage
scripts/run-pipeline.sh --from-stage value    # value → … → health
scripts/run-pipeline.sh --stage value         # value alone (later stages left stale)
scripts/run-pipeline.sh --stage value --only unify   # one step of it
scripts/run-pipeline.sh --stage value --list  # its steps
scripts/run-pipeline.sh --stage value --offline   # pure-cache run, no network
```

Steps run in this order (`src/value/run_value_pipeline.py`); steps sharing a
`pgroup` run concurrently.

| Step | pgroup | Work |
|---|---|---|
| `repology` · `ossfuzz` | dumps | 30-day whole-file-TTL dumps the ecosystem sub-pipelines read. Repology's `packages.csv` is the cpp distro-version input; OSS-Fuzz's `projects.csv` feeds `build_git_urls` and the risk stage's fuzzed flag. |
| `npm` · `crates` · `pypi` · `cpp` | eco | The four ecosystem sub-pipelines (see the [component docs](#ecosystems)). |
| `stats` | — | Write the per-ecosystem matrix `data/value/stats.csv`. |
| `git-urls` | — | Classify each package's git URL into `data/sources/{eco}/git.csv`. |
| `eco-fetch` | identity | Pull ecosyste.ms repo candidates. |
| `canonical` | identity | Resolve every `github_repo` to its current `nameWithOwner` + numeric id (90-day per-row TTL). |
| `resolve` | — | Apply both identity sources onto each per-eco `results.csv`. |
| `unify` | — | Group packages by repo into `value.csv`. |
| `validation` | — | Stamp `git_valid`. |
| `openssf-crit` · `eco-crit` | crit | Fetch the two criticality sources for the class-A scope `unify` just decided. |
| `criticality` | — | Stamp `openssf_crit` / `eco_crit` / `value_score`. |

`eco-fetch` and `canonical` run concurrently because each reads the previous
run's `value.csv` and writes its own file.

`--rollup` skips straight to the cross-ecosystem tail — see [Unified
output](#unified-output).

## Metrics Roadmap

Each leaf is one metric, with its source and the period it covers. Per-language
lineage lives in the component docs — [JavaScript/npm](components/javascript.md),
[Python/PyPI](components/python.md), [Rust/crates](components/rust.md),
[C/C++](components/cpp.md). The cross-ecosystem rollup is below.

> `[2021–2025]` is the 5-year window; `[most recent]` is the latest pull of
> that source.

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
    │                                (git clone URLs only — never tarball/hg/svn)
    ├── canonical_url             ← GitHub Repos API + overrides.csv       [most recent]
    │                                (two meanings — see the column table)
    ├── git_valid                 ← build_validation (strictly True/False) [most recent]
    │                                (GitHub API + git ls-remote caches
    │                                 → value/validation.csv)
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

Select the top packages, expand their dependency tree, run download-weighted
PageRank over that graph, then classify each node A/B/C.

Both cuts use **cumulative share**, never a fixed package count. Downloads and
PageRank follow a power law: a small head of packages carries almost all the
mass. A share cutoff tracks that head as the ecosystem grows.

### Top Package Selection

Sort packages by average annual downloads, descending. Accumulate downloads
down the list until the running total reaches 95% of the ecosystem-wide total
(the `downloads_<year>` rows of `data/value/stats.csv`). Every package above
that cutoff is "top".

### PageRank

Nodes are packages; a directed edge A→B means "A depends on B". Rank flows
along that edge from the dependent to its dependency. A package therefore
scores high by being **depended on**; declaring many dependencies never raises
its own score. Personalized PageRank (alpha = 0.85) sets each node's restart
probability to its share of the ecosystem's average annual downloads, so a
package that is both widely depended on **and** widely downloaded ranks
highest. One number therefore carries three signals: **downloads**,
**dependencies** and **dependents**.

### Value Classes

Packages sorted by PageRank descending; cumulative PageRank share sets the class:

| Class | Cumulative Share | Meaning |
|-------|-----------------|---------|
| **A** | 0--75% | Critical infrastructure |
| **B** | 75--95% | Important, widely depended on |
| **C** | 95--100% | Long tail |

> [Current Limitations](#current-limitations) lists the known scope gaps.

### Funnel

The preview pipeline sheet → Value holds the per-ecosystem funnel counts and
repo-coverage percentages. Its columns mean:

| Column | Meaning |
|---|---|
| *Top packages* | Packages covering 95% of cumulative downloads. |
| *After dep tree* | `\|top ∪ transitive deps\|` — the universe analysed for PageRank. |
| *Results* | Every node of that universe; a top package with no edges keeps a row with PageRank 0. cpp is smaller than its dep tree because the `is_cpp` filter drops language-agnostic distro packages. |
| *GH %* | Share with a github.com repo. |
| *Git %* | Share with a repo on any host — also gitlab, bitbucket, sourcehut, codeberg and `custom` (savannah, sourceware, kernel.org, …). |

## Ecosystems

Each language assembles the steps above from its own sources. The component
docs cover what each source supplies and which stage consumes it.

| Language | Registry / sources | Pipeline doc | Raw-fetch reference |
|---|---|---|---|
| JavaScript / TypeScript | npm | [components/javascript.md](components/javascript.md) | [sources/npm.md](sources/npm.md) |
| Python | PyPI | [components/python.md](components/python.md) | [sources/pypi.md](sources/pypi.md) |
| Rust | crates.io | [components/rust.md](components/rust.md) | [sources/crates.md](sources/crates.md) |
| C / C++ | Debian + Homebrew + Repology + OSS-Fuzz | [components/cpp.md](components/cpp.md) | [debian](sources/debian.md) · [homebrew](sources/homebrew.md) · [repology](sources/repology.md) · [ossfuzz](sources/ossfuzz.md) |

C/C++ has no single registry, so it is unified from Debian and Homebrew (joined
via Repology) and has a component doc instead of a `sources/cpp.md`. Its
Wayback-derived install proxies carry snapshot caveats — see the
[debian](sources/debian.md) and [homebrew](sources/homebrew.md) pages.

## Value data sources

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

Every ecosystem pipeline writes four files to `data/sources/{ecosystem}/`. The
cross-ecosystem steps add `git.csv` (the per-platform URL table from
`build_git_urls`), and `check_eol.py` adds `eol.csv`, an Eligibility input.

### top-packages.csv

Packages covering 95% of ecosystem downloads.

| Column | Description |
|--------|-------------|
| `package` | Package name |
| `avg_downloads` | Average annual downloads (2021--2025) |
| `avg_downloads_share` | Fraction of ecosystem-wide total downloads |
| `2021`--`2025` | Downloads per year |

cpp differs: no per-year columns, and `debian_avg_downloads`, `debian_share`,
`homebrew_avg_downloads`, `homebrew_share` replace the two download columns.

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
| `eco_guess` | Provenance of the repo identity after `resolve`: `eco` (ecosyste.ms), `native` (registry metadata), `override`, or empty |
| `avg_downloads` | Average annual downloads |
| `2021`--`2025` | Downloads per year |
| `top` | `True` if the package is in the 95% cumulative set |
| `pagerank` | Download-weighted PageRank score |
| `value_class` | A/B/C (see [Value Classes](#value-classes)) |
| `repo_id` | Namespaced repo id (`gh/<id>` / `gl/…`), stamped by `resolve` |
| `canonical_url` | A GitHub mirror's non-GitHub upstream, or — for a `canonical_url`-only override — the source location of a project with no git upstream (see [Manual overrides](#manual-overrides)) |
| `license` | Registry-reported license (stamped by the per-eco `fetch_licenses.py`) |

cpp differs: `debian_avg_downloads`, `homebrew_avg_downloads` and the blended
`downloads_score` replace `avg_downloads`, the per-year columns and `top`.

## Unified output

`data/value/value.csv` is the canonical per-repo table: one row per repo on any
host, plus one row per orphan package (no `repo`), so nothing is dropped. All
classes A/B/C are kept.

The rollup chain builds it — `eco-fetch` → `canonical` → `resolve` → `unify` →
`validation` → `openssf-crit` → `eco-crit` → `criticality`:

- `resolve` (`apply_ecosystems_authority`) re-resolves each package's repo
  identity under `override > github-canonical > ecosyste.ms > prior registry
  data`, and stamps `canonical_url`.
- `unify` (`src/value/unify_value_data.py`) reads each ecosystem's `results.csv`
  and `eol.csv`, groups packages by repo, and computes every per-ecosystem and
  cross-ecosystem aggregate in one pass, sorted by `top_eco_pct` desc. It
  writes the per-repo table directly — there is no separate aggregation step.
- `criticality` (`src.value.apply_criticality`) stamps `openssf_crit`,
  `eco_crit` and `value_score`, then re-sorts the shipped file by `value_score`
  desc. Unscored rows (below the 2-component floor, mostly class B/C) sink to
  the end in `top_eco_pct`-desc order.

Manual corrections come from [`data/value/overrides.csv`](#manual-overrides).
After editing that file, rerun the same chain from the existing per-eco
`results.csv`:

```bash
uv run python -m src.value.run_value_pipeline --rollup
```

`--rollup` skips the ecosystem sub-pipelines and the `stats` / `git-urls`
steps. It is the one value-stage entry point `scripts/run-pipeline.sh` does not
wrap, and it leaves the later stages stale — follow it with
`scripts/run-pipeline.sh --from-stage risk`.

**Per-ecosystem class.** Sum the group's package PageRank inside the ecosystem,
rank groups by that sum desc, then apply the package-level cumulative-share
cutoffs (≤75% A, ≤95% B, rest C). The strongest class across ecosystems becomes
`class`. Ranking inside an ecosystem avoids comparing PageRank magnitudes
across ecosystems, since each ecosystem's PR mass sums to 1 in its own graph. A
repo-level PageRank was skipped: with no cross-ecosystem deps in our data, the
repo graph stays four disconnected subgraphs.

| Column | Description |
|--------|-------------|
| `repo` | Lowercase repo slug on its `platform` — GitHub `owner/repo`, GitLab's arbitrarily-nested `owner/…/repo`, Sourcehut `~user/repo`, custom best-effort path. Empty only for orphans (no upstream repo at all). |
| `platform` | Host class of `git_url`, from `classify()` in `src/value/build_git_urls.py`: `github` / `gitlab` / `bitbucket` / `sourcehut` / `codeberg` / `custom`. `gitlab` means **any** GitLab instance: `classify()` derives its host set from `HOST_NICKNAMES` in `src/sources/gitlab/gitlab_client.py`, plus a `gitlab.*` heuristic for unregistered self-hosted instances. Empty for orphan rows. Risk and eligibility filter on `settings.json → top_repos.platforms` (`github` + `gitlab`). |
| `repo_id` | Repo id namespaced by platform: `gh/<numeric>` (GitHub Repos API id), or `gl/<nickname>-<id>` from the GitLab project API on any GitLab host (bare `gl/<id>` for gitlab.com; nicknames per `HOST_NICKNAMES`). Empty for other platforms (no API id) and for unresolved/404 repos. |
| `git_url` | Canonical **git clone URL** — `https://github.com/<repo>.git` for GitHub repos, so a valid GitHub repo always carries both `repo` and `git_url`. Otherwise the first non-empty value from the per-ecosystem `data/sources/{eco}/git.csv`, canonicalised by `src/value/git_urls.py`. A **non-git** source (tarball / hg / svn) belongs in `canonical_url`, never here. Empty for orphan packages and for projects with no git upstream. |
| `canonical_url` | The project's **canonical upstream** — the thing being mirrored, not the mirror. Two meanings, told apart by whether the row has a repo. (1) *Mirror upstream*, on a GitHub mirror row: `gnutools/glibc` → `https://sourceware.org/git/glibc.git`, `gcc-mirror/gcc` → `https://gcc.gnu.org/git/gcc.git`. It comes from the `canonical_url` column of `data/sources/github/repos.csv` (GitHub's mirror metadata, stamped by `resolve`) or from a repo override carrying a non-GitHub `git_url` (`torvalds/linux` → `https://git.kernel.org/…/linux.git`). (2) *No-git source*, on a row with no `repo`, `repo_id` or `git_url`: where the code actually lives for a project with no git upstream (IJG libjpeg, Info-ZIP unzip, GraphicsMagick, Berkeley DB, R). Set by a `canonical_url`-only row in `overrides.csv`. Empty otherwise. |
| `git_valid` | Strictly `True` / `False`, never blank. `True` iff the repo's upstream is reachable. Host-agnostic: GitHub rows are checked via the Repos API cache, non-GitHub rows via `git ls-remote`, and a `gl/` `repo_id` is proof on its own (the GitLab project API confirmed the project; `ls-remote` can fail on a live GitLab host). `False` covers both *no `git_url` to check* (orphan, or no git upstream) and *a `git_url` that failed the check* (unreachable / 404). Set by `build_validation`; audit trail in [`data/value/validation.csv`](components/validation.md). |
| `ecosystems` | Comma-separated ecosystems where the repo has packages (e.g. `crates,npm`) |
| `packages` | Total package count in the repo |
| `top_eco` | Ecosystem where the repo ranks highest (max PR percentile): `npm` / `pypi` / `crates` / `cpp` |
| `top_eco_pkg` | Highest-PR package in `top_eco` (e.g. `@babel/helper-plugin-utils` for babel/babel) |
| `top_eco_pct` | The repo's PageRank **position** inside `top_eco`: `100 − cumulative-PR percentile`. 0–100, **higher = better**; the tail sits near 0. A `value_score` component (weight `centrality_weight`). |
| `pr_score` | Cross-ecosystem dependency **mass**, complementing `top_eco_pct`'s position. 0–100 (two decimals), **higher = better**. Per ecosystem the repo's summed package PageRank is `ln`-scaled and min-max normalized over that ecosystem's repos; the per-eco values combine as a **p = 2 norm** (`PR_SCORE_P` in `unify_value_data.py`), so a real second-ecosystem footprint adds ~41% (√2), a token listing adds ~nothing, and an extra ecosystem never lowers the score. The column is then rescaled so the highest-mass repo = 100. Blank only for groups with no PageRank signal. A `value_score` component (weight `pr_score_weight`). |
| `class` | Strongest of the per-ecosystem classes (A < B < C) |
| `class_npm`, `class_pypi`, `class_crates`, `class_cpp` | A/B/C from per-ecosystem cumulative PR share; empty if the repo has no package in that ecosystem |
| `openssf_crit` | OpenSSF criticality score ·100 (0–100, two decimals; the source CSV keeps the raw 0–1 value), joined from `data/sources/openssf/criticality.csv` by `src.value.apply_criticality`. **Non-empty for every valid class-A GitHub repo, archived included** — that is the fetch scope, and `scripts/pipeline_health.py` gates on it. Empty for non-GitHub rows (the tool is GitHub-only), for B/C rows outside the fetch scope, and for unresolved/invalid repos. |
| `eco_crit` | ecosyste.ms critical flag: `100` = the repo's canonical package is on the critical list, `0` = **explicitly** not on it (`critical=false`), blank = unknown (the fetch did not resolve, the registry omitted the flag — common for spack/debian cpp packages — or the repo sat outside the class-A scope). A blank flag is never a real 0. Joined from `data/sources/ecosystems/criticality.csv` by `src.value.apply_criticality`. Covers **GitHub and GitLab**. |
| `value_score` | 0–100 **pro-rata blend** of up to four components: `openssf_crit` (weight 0.6), `eco_crit` (0.2), `top_eco_pct` (0.1), `pr_score` (0.1). Only the components present are summed, then renormalized by their weight sum, so a repo missing some still lands on the same 0–100 scale. Blank unless at least `min_components` (2) are present. Weights sit in `settings.json → value_score` and are criticality-dominant, so a foundational-but-quiet micro-dep cannot outrank a genuinely critical project; the two PageRank-family factors split the 0.2 centrality weight. GitLab class-A repos carry no `openssf_crit`, so `top_eco_pct` + `pr_score` alone clear the floor. Stamped by `src.value.apply_criticality`. |

### Manual overrides

`data/value/overrides.csv` is the single hand-maintained list of corrections for
packages whose **upstream registry metadata is wrong**. It corrects bad source
data; it never patches a parsing bug. Rows key on `(package, ecosystem)`, and a
row with a blank `reason` is rejected.

| Column | Meaning |
|---|---|
| `package`, `ecosystem` | The key. |
| `repo` | Force the correct GitHub `owner/repo`. Sets `platform` = `github` and derives the matching `git_url`. |
| `git_url` | Force a corrected non-GitHub **git clone URL**. Absolute: every eco-/registry-derived host is dropped, and `(platform, repo)` are re-derived from it. |
| `canonical_url` | The project's canonical upstream. With a `git_url` it marks that repo as a **mirror**; alone it means the project has **no git upstream** (tarball / hg / svn). |
| `valid` | Pin the git target's validity (`True`/`False`); consumed by `build_validation`, not applied at resolve time. |
| `reason` | Required free-text justification. |

Three encodings matter.

**Repo / URL correction** — set `repo` (GitHub) or `git_url` (any other host).
A non-GitHub `git_url` on a `repo` row is the live upstream a GitHub mirror
syncs from, and is preserved as `canonical_url` (`torvalds/linux` →
`git.kernel.org`; `gcc-mirror/gcc` → `gcc.gnu.org`).

**Self-hosted project, reached through its mirror** — `git_url` = the
GitHub/GitLab **mirror**, `canonical_url` = the upstream it copies, `valid`
blank. This is how a project on a self-hosted git server enters the risk scope
at all (`gnutools/glibc` ← `sourceware.org`, `qt/qt5` ← `code.qt.io`,
`1g4-mirror/libunistring` ← `git.savannah.gnu.org`), because the [risk
stage](risk.md#scope-github-and-gitlab-only) scores only GitHub and GitLab. The
mirror must be *verified* — identical HEAD sha, full ref set, in sync now — and
the `reason` field records that evidence. A row carrying a `canonical_url`
scores no issue backlog, because the mirror's tracker is not the project's (see
[components/workload.md](components/workload.md)).

**No git upstream** — `git_url` **blank**, the real source URL in
`canonical_url`, `valid` **blank**. The package resolves to no repo, so
validity is derived: no `git_url` ⇒ nothing to validate ⇒ `git_valid` =
`False`. A non-git URL must never go in `git_url`. The alternative — mapping
the package to a fork or a personal mirror — credits the wrong maintainers:
IJG's libjpeg was crediting `mozilla/mozjpeg`, Info-ZIP's unzip was crediting
zlib's author. Info-ZIP unzip, GraphicsMagick (hg), Berkeley DB (Oracle
tarball) and R (svn) use this encoding.

The `resolve` step applies overrides to each `results.csv`
(`apply_ecosystems_authority` rewrites `git` / `github_repo` and stamps
`canonical_url`), `unify` forces the group's identity, and `build_validation`
applies the `valid` pin.

### What value.csv does not hold

`unify` groups package rows by `repo_id` / `git_url`, so the package-level rows
stay in `data/sources/{eco}/results.csv`. Per-class × per-ecosystem counts and
the GitHub-group / orphan split are on the preview pipeline sheet → Value.

EOL is deliberately absent: it feeds Eligibility, not the value table. The
per-ecosystem `check_eol.py` writes `data/sources/{eco}/eol.csv`, an advisory
input to the manual `eol` column of `data/eligibility/overrides.csv`, which
`src.eligibility.build_active` consumes (see [eligibility.md](eligibility.md)).

## Current Limitations

Known scope choices and gaps. Add new entries here, so caveats stay out of code
comments and source-doc footnotes.

### cpp dependency tree is runtime-only

Build-time tooling (cmake, pkgconf, autoconf, gettext, …), Debian
`Recommends`/`Suggests`, and Homebrew `build` deps do **not** propagate
PageRank. The cpp pipeline drops them at two points:

- Debian: `fetch_debian_data.py` stores only `Depends` + `Pre-Depends`.
- Homebrew: raw deps carry `runtime` and `build`;
  `src/sources/cpp/process_data.py` keeps `runtime` only.

PageRank therefore reflects who *runs* with whom, not who *builds* whom, and
undervalues build infrastructure such as cmake and pkgconf. To fix later: keep
the source-side type in the cpp edge schema and add a build-aware PR overlay.

### Project identity is GitHub/GitLab-only downstream

Risk, eligibility, EOL and contributor metrics key off the hosts in
`settings.json → top_repos.platforms` — currently **GitHub and GitLab**. GitLab
repos are first-class: they carry a `gl/…` `repo_id` from the GitLab project
API on any GitLab host and are scored alongside GitHub repos (glib on
gitlab.gnome.org, mpfr on gitlab.inria.fr, pixman on gitlab.freedesktop.org).

A project hosted on its own git server enters the scope through a **verified
mirror** declared in [`overrides.csv`](#manual-overrides), with the upstream
recorded in `canonical_url`. gcc (`gcc-mirror/gcc` ← gcc.gnu.org), glibc
(`gnutools/glibc` ← sourceware.org), qt (`qt/qt5` ← code.qt.io) and
libunistring (`1g4-mirror/libunistring` ← git.savannah.gnu.org) all reach risk
and eligibility this way.

Every other host keeps a full `(platform, repo, repo_id, git_url)` identity in
`value.csv` — codeberg, bitbucket, sourcehut and `custom` (savannah,
sourceware, kernel.org, project-owned servers) — but those rows stay out of the
top-repo scope, and a `custom` row carries no `repo_id` at all. A class-A
project on such a host needs a verified-mirror override before risk or
eligibility can score it.

A GitLab host counts as GitLab only when `HOST_NICKNAMES`
(`src/sources/gitlab/gitlab_client.py`) registers it or it matches the
`gitlab.*` heuristic; that registry also assigns the `gl/<nickname>-<id>`
prefix. Add a new GitLab instance there before expecting its repos to be
scored.

To fully fix: per-host adapters for license/EOL/contributor checks against
codeberg, savannah, sourceware, and the rest.

### Some projects have no git upstream at all

IJG libjpeg, Info-ZIP unzip, GraphicsMagick, Berkeley DB and R publish only
tarballs, Mercurial or Subversion. They carry no `repo` / `repo_id` /
`git_url`, so `git_valid` is `False` and they sit outside the top-repo scope:
**unfundable via a repo**, which is the honest answer. A `canonical_url`-only
row in [`overrides.csv`](#manual-overrides) records where their source lives.

### No package-level quality gate before results.csv

`results.csv` admits the whole top-95%-cumulative-download set plus its
transitive deps — no license check, no age cutoff, no popularity floor, no
archive/EOL gate. Quality filtering happens downstream: the per-ecosystem
`check_eol.py` / `fetch_licenses.py` signals feed the Eligibility checks on the
top-repo set. This keeps value scoring independent of signal quality, but it means
the raw value class distribution overstates how many projects we would fund.

### Wayback-derived install stats have gaps

Homebrew and Debian popcon both arrive via Wayback Machine snapshots, which
sometimes truncate (1 MB cap) or miss a year. `ecosystem_avg_downloads` in
`src/common/params.py` averages only over populated years, so a missing year
does not deflate the average. Year-over-year trend lines stay noisy, and a few
packages get their `avg_downloads` from fewer than 5 years of data.
