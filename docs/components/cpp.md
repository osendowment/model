# C / C++ (Debian + Homebrew + Repology)

C/C++ has **no single package registry**, so this ecosystem is *assembled*: it
unifies Debian and Homebrew C/C++ packages — joined at the
[Repology](../sources/repology.md) canonical-project level — and uses
[OSS-Fuzz](../sources/ossfuzz.md) as a security signal. There is no
`sources/cpp.md`; this component page is the cpp pipeline's home, and the
underlying sources are documented separately:
[debian](../sources/debian.md), [homebrew](../sources/homebrew.md),
[repology](../sources/repology.md), [ossfuzz](../sources/ossfuzz.md).

## Sources & data collected

| Source | Data collected | Raw location |
|---|---|---|
| [Debian popcon](https://popcon.debian.org) (via Wayback) | install-base counts (downloads proxy) | `data/sources/debian/raw/downloads.csv` |
| [Debian `Packages.xz`](https://deb.debian.org) | `Depends`+`Pre-Depends` (runtime) edges, `homepage`, `vcs_browser`, `section` | `data/sources/debian/raw/{dependencies,package-metadata}.csv` |
| [Debian UDD](https://udd-mirror.debian.net) | C/C++ classification via debtags | `data/sources/debian/raw/cpp-packages.csv` |
| [Homebrew formula API](https://formulae.brew.sh) | formula deps (`runtime`+`build`), `homepage`, `source_url`, `license`, `language` | `data/sources/homebrew/raw/{formulas,dependencies}.csv` |
| [Homebrew analytics](https://formulae.brew.sh) (via Wayback) | 365-day install counts (downloads proxy) | `data/sources/homebrew/raw/downloads.csv` |
| [Repology](https://repology.org) | canonical project names (`packages.csv`, via `src/sources/repology/fetch_repology_data.py`) + upstream repo URL candidates (`project-urls.csv`, via `src/sources/cpp/fetch_repology_urls.py`) | `data/sources/repology/{packages,project-urls}.csv` |
| [OSS-Fuzz](https://github.com/google/oss-fuzz) | fuzz-tested project list + `main_repo` | `data/sources/ossfuzz/projects.csv` |

Both download proxies come from sparse Wayback snapshots — see the per-source docs
for the snapshot-coverage caveats. No authentication required.

## Unification

`src/sources/cpp/process_data.py` joins Debian + Homebrew on the Repology canonical
name, then:

- **Downloads** — MAX within each ecosystem (avoid double-counting variants like
  `boost1.74`/`boost1.81`), SUM across ecosystems (Debian and Homebrew are disjoint
  user populations).
- **Dependencies** — union of **runtime-only** project→project edges (see below).
- **is_cpp** — true if any constituent binary/formula is flagged C/C++.
- **Top selection** — within the 95% cumulative download mass of **either** Debian
  or Homebrew.

### Dependency types: runtime only

The cpp dep tree contains runtime project→project edges only. Build-time tooling
(cmake, pkgconf, autoconf, gettext, …) and Debian `Recommends`/`Suggests` do **not**
propagate PageRank. Two filters combine:

| Source | Collected at fetch | Filter applied by cpp |
|---|---|---|
| Debian (`fetch_debian_data.py`) | `Depends` + `Pre-Depends` only — already runtime-only; `Build-Depends`/`Recommends`/`Suggests` not collected | none (all of it is used) |
| Homebrew (`fetch_homebrew_data.py`) | both `runtime` and `build` types stored in raw deps | `cpp/process_data.py` (`build_homebrew_edges`) — rows with `dep_type != "runtime"` are skipped |

The `type` column in `data/sources/cpp/dependency-tree.csv` is uniformly
`"declared"` — the cpp pipeline's own term for "runtime dep declared by either
ecosystem", not a faithful copy of the source-side type. Consequence: PageRank
reflects who *runs* with whom, not who *builds* whom, so build infrastructure
(cmake, pkgconf) is undervalued relative to its real load-bearing role.

## Value pipeline

After unification, cpp uses the shared scoring mechanics (download-weighted
PageRank α = 0.85, then A/B/C cumulative-share cutoffs — see
[`value.md`](../value.md)). Orchestrated by `src.value.cpp_pipeline`, which runs
the Debian and Homebrew sub-pipelines, then the Repology upstream-URL fetch
(`fetch-repology` → `src.sources.cpp.fetch_repology_urls`), then the cpp
aggregation.

```
C / C++ (Debian + Homebrew + Repology)
├── debian_avg_downloads   ← Debian popcon (Wayback snapshots)    [2021–2025]
├── homebrew_avg_downloads ← Homebrew analytics (Wayback)         [2021–2025]
├── downloads_score        ← derived (debian+homebrew composite)  [2021–2025]
├── dep edges (package→dep)← Debian Packages.xz (Depends/Pre-)    [most recent]
│                            + Homebrew formula.json (runtime)    [most recent]
├── pagerank               ← derived                              [2021–2025]
├── value_class            ← derived                              [2021–2025]
└── package→repo           ← Repology project URLs                [most recent]
```

## Where it's used downstream

- **Value** — `value_class` feeds the `class_cpp` column of
  `data/value/value.csv`.
- **Risk & Eligibility** — both automated stages (the risk pipeline and the
  [Eligibility stage](../eligibility.md)) filter to `platform == github`, and cpp
  identity is **GitHub-only**. Many flagship cpp upstreams live off GitHub — glibc
  (sourceware.org), gcc (Savannah), glib (gitlab.gnome.org), mpfr (gitlab.inria.fr),
  curl (curl.se) — so they carry a non-github `platform` + `git_url` in `value.csv`,
  and slip out of both stages. Counting non-GitHub
  Git hosts (sourceware, Savannah, GNOME) lifts coverage well above the
  GitHub-only figure — most non-GitHub upstreams are the load-bearing class-A
  libraries. For the repos that do resolve to GitHub, the eligibility stage
  consumes cpp's per-ecosystem signals: `fetch_licenses.py` fills the `license`
  column of `results.csv` (the registry-first input to the stage's license
  check), and `check_eol.py` → `data/sources/cpp/eol.csv` produces advisory
  package-level EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Outputs

In `data/sources/cpp/`:

- `raw/packages.csv` — per-project join with aggregated signals
- `top-packages.csv` — top C/C++ projects by download mass
- `dependency-tree.csv` — runtime project→project edges (`type` = `"declared"`)
- `github-repos.csv` — project→GitHub-repo mappings
- `results.csv` — all dep-tree projects with `pagerank` + `value_class` (plus
  the repo-identity columns `git`, `eco_guess`, `repo_id`, `mirror_url` and
  `license`)

```bash
uv run python -m src.sources.cpp.process_data [--top-share F]
```

### cpp funnel & classes

See [docs/stats.md → Value](../stats.md#per-ecosystem-value-funnel) for the C/C++ funnel counts (top packages → dep tree → results → repo coverage) and class distribution.

`Results` is smaller than `After dep tree` because the `is_cpp` filter drops
language-agnostic distro packages that rode in as dependencies.

## Limitations

- **Runtime-only dep tree** — build infrastructure (cmake, pkgconf) is undervalued;
  PageRank reflects runtime coupling, not build coupling.
- **GitHub-only identity downstream** — Risk and Eligibility miss non-GitHub
  upstreams even though `value.csv` now exposes their `git_url`. Fully fixing
  this needs per-host adapters (GitLab API, Savannah, sourceware) for
  license/EOL/contributor checks.
- **`is_cpp` drops** — language-agnostic distro packages are filtered out of
  `results.csv`, so the cpp result set is smaller than its raw dep tree.
- **Wayback-derived installs** — both download proxies have sparse/truncated
  snapshots (see [debian](../sources/debian.md) / [homebrew](../sources/homebrew.md)).
