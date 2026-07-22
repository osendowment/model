# C / C++ (Debian + Homebrew + Repology)

C/C++ has **no single package registry**, so this ecosystem is *assembled*: it
unifies Debian and Homebrew C/C++ packages — joined at the
[Repology](repology.md) canonical-project level — and uses
[OSS-Fuzz](ossfuzz.md) as a security signal. cpp has no registry of its own,
so this page is the pipeline's home and the underlying sources are documented
separately:
[debian](debian.md), [homebrew](homebrew.md),
[repology](repology.md), [ossfuzz](ossfuzz.md).

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
  `boost1.74`/`boost1.81`), then a weighted sum across ecosystems, since Debian
  and Homebrew are disjoint user populations. `downloads_score =
  1.39·debian_avg + 1.0·homebrew_avg` (`downloads_score` in
  `src/settings.json`); the ratio inverts the two ecosystems' annual install
  totals, so a typical package from either contributes equally.
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
├── homebrew_avg_downloads ← Homebrew analytics (Wayback)         [2022–2025]
├── downloads_score        ← derived (1.39·debian + 1.0·homebrew) [2021–2025]
├── dep edges (package→dep)← Debian Packages.xz (Depends/Pre-)    [most recent]
│                            + Homebrew formula.json (runtime)    [most recent]
├── pagerank               ← derived                              [2021–2025]
├── value_class            ← derived                              [2021–2025]
└── package→repo           ← Repology project URLs                [most recent]
```

## Where it's used downstream

- **Value** — `value_class` feeds the `class_cpp` column of
  `data/value/value.csv`.
- **Risk & Eligibility** — both automated stages score `platform in {github,
  gitlab}` (`SUPPORTED_PLATFORMS` in `src/common/params.py`,
  `top_repos.platforms` in `src/settings.json`), so cpp's GitLab-hosted
  upstreams rank in alongside its GitHub ones. Projects on
  gitlab.freedesktop.org, salsa.debian.org, gitlab.gnome.org,
  gitlab.inria.fr, and gitlab.com all enter this way — `gnome/glib` and
  `mpfr/mpfr` are scored class-A cpp repos. A project on a self-hosted git
  server (`platform=custom`) cannot be
  measured, so it enters only through a **verified mirror**: `git_url` points
  at the mirror and `canonical_url` records the upstream. `gnutools/glibc`
  (canonical sourceware.org) and `gcc-mirror/gcc` (canonical gcc.gnu.org)
  reach both stages that way. The eligibility stage also consumes cpp's
  per-ecosystem signals: `fetch_licenses.py` derives licences into the durable
  `raw/licenses.csv` cache (`package, license, fetched_at`) and fills the `license` column of
  `results.csv` (the registry-first input to the stage's license check), and
  `check_eol.py` → `data/sources/cpp/eol.csv` produces advisory package-level
  EOL signals that inform the manual `eol` override in
  `data/eligibility/overrides.csv`.

## Outputs

In `data/sources/cpp/`:

| File | Description |
|---|---|
| `raw/packages.csv` | Per-project join: `project, github_repo, debian_sources, homebrew_formulas, debian_avg_downloads, homebrew_avg_downloads, downloads_score, is_cpp, is_oss_fuzz` |
| `top-packages.csv` | Top C/C++ projects by download mass — `package, debian_avg_downloads, debian_share, homebrew_avg_downloads, homebrew_share`. cpp keeps no per-year download columns |
| `dependency-tree.csv` | Runtime project→project edges — `package, dependency, type`; `type` is always `declared` |
| `github-repos.csv` | Project → GitHub repo mappings — `package, github_repo` |
| `git.csv` | Project → upstream git URL per host; written by the value stage |
| `results.csv` | Every dep-tree project with `pagerank`, `value_class`, `repo_id`, `canonical_url`, `license`. `debian_avg_downloads`, `homebrew_avg_downloads` and the blended `downloads_score` take the place of the other ecosystems' `avg_downloads`, per-year columns and `top` |
| `eol.csv` | `package, is_eol, eol_method, eol_reason, source, eol_checked_at` |

```bash
uv run python -m src.sources.cpp.process_data [--top-share F]
uv run python -m src.sources.cpp.fetch_repology_urls [--classes A,B,C] [--limit N] [--refresh]
uv run python -m src.sources.cpp.fetch_licenses
uv run python -m src.sources.cpp.check_eol [--limit N] [--refresh]
```

### cpp funnel & classes

See the preview pipeline sheet → Value for the C/C++ funnel counts (top packages → dep tree → results → repo coverage) and class distribution.

`Results` is smaller than `After dep tree` because the `is_cpp` filter drops
language-agnostic distro packages that rode in as dependencies.

## Limitations

- **Runtime-only dep tree** — build infrastructure (cmake, pkgconf) is undervalued;
  PageRank reflects runtime coupling, not build coupling.
- **Self-hosted upstreams need a mirror** — Risk and Eligibility score GitHub
  and GitLab. A project living only on sourceware, Savannah, or another
  self-hosted server stays in `value.csv` with `git_valid=True` but is never
  scored, unless a curated `data/value/overrides.csv` row routes it through a
  verified mirror. Scoring such repos directly needs per-host adapters for the
  license, EOL, and contributor checks.
- **`is_cpp` drops** — language-agnostic distro packages are filtered out of
  `results.csv`, so the cpp result set is smaller than its raw dep tree.
- **Wayback-derived installs** — both download proxies have sparse/truncated
  snapshots (see [debian](debian.md) / [homebrew](homebrew.md)).
